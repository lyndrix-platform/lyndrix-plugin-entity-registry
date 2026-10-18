"""Entity Registry — business logic.

The registry hands out UUIDs and reconciles identities, nothing more. The
central operation is ``identify()``: given an entity type and a set of
``{scheme: value}`` identifiers it finds the one entity they belong to —
creating it if none exists, learning new aliases when exactly one matches,
and filing a conflict (never auto-merging) when several match.

Events emitted
--------------
entity_registry:entity_created  — {entity}
entity_registry:entity_updated  — {entity, changes: [...]}
entity_registry:entity_merged   — {keep: entity, drop_uuid}
entity_registry:entity_retired  — {entity}
entity_registry:conflict        — {conflict}
entity_registry:maintenance_changed — {source, kind, count, ...} once per
                                  processed maintenance:* event (0.3.0)
"""
from __future__ import annotations

import asyncio
import functools
import hashlib
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from sqlalchemy import delete, func, or_, select, text, tuple_
from sqlalchemy.exc import IntegrityError

from core.api import get_logger

from ..model.database import ensure_tables, get_session, retry_write
from ..model.models import Conflict, Entity, Identifier, MaintenanceEntry, Relation
from . import maintenance as mwx
from .catalog import CatalogLoader

log = get_logger("Plugin:EntityRegistry:Service")

LIVE_STATUSES = ("active", "retired")
# identify() refreshes last_seen_at at most this often for an otherwise
# unchanged entity — every sync run of every source would otherwise write the
# same hot rows from N instances (and collide on MariaDB snapshot isolation).
LAST_SEEN_RESOLUTION = timedelta(seconds=30)
CATALOG_RELOADED_TOPIC = "entity_registry:catalog_reloaded"
MAINTENANCE_CHANGED_TOPIC = "entity_registry:maintenance_changed"
# Expired overrides are purged this long after their effective end (no timer:
# the sweep piggybacks on the next override event).
PURGE_GRACE = timedelta(days=1)
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


class RegistryError(ValueError):
    """Invalid input (unknown type/scheme/relation, missing identifiers, ...)."""


class NotFound(LookupError):
    pass


def _now() -> datetime:
    # MariaDB DateTime is tz-naive; store UTC consistently.
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _norm_identifiers(identifiers: dict | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for scheme, value in (identifiers or {}).items():
        if value is None:
            continue
        s, v = str(scheme).strip(), str(value).strip()
        if s and v:
            out[s] = v
    return out


class EntityRegistryService:
    def __init__(self) -> None:
        self._ctx = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self.catalog = CatalogLoader()
        self._ready = False

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def set_context(self, ctx) -> None:
        self._ctx = ctx
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None

    def on_db_connected(self) -> None:
        """Bootstrap tables (bootstrap-locked, peer-race tolerant), then go ready."""
        ensure_tables()
        self._ready = True
        log.info("EntityRegistry: DB tables ready")

    async def init_db(self, reason: str, *, attempts: int = 5, pause: float = 5.0) -> bool:
        """Run :meth:`on_db_connected` off-loop; retry a failed bootstrap a few
        times instead of staying not-ready until the next restart."""
        for attempt in range(1, attempts + 1):
            try:
                await asyncio.to_thread(self.on_db_connected)
                log.info(f"EntityRegistry: tables ready ({reason})")
                return True
            except Exception as exc:
                log.error(f"EntityRegistry: DB init failed ({reason}, attempt {attempt}/{attempts}) — {exc}")
                if attempt < attempts:
                    await asyncio.sleep(pause * attempt)
        return False

    @property
    def is_ready(self) -> bool:
        return self._ready

    def _require_ready(self) -> None:
        if not self._ready:
            raise RuntimeError("Entity Registry database tables not ready")

    def set_catalog_dir(self, path: str | Path) -> None:
        self.catalog.catalog_dir = Path(path)

    def reload_catalog(self, *, announce: bool = False) -> None:
        """Re-read the catalog YAML. ``announce=True`` (operator-triggered reload)
        tells the other cluster instances to reload theirs as well."""
        self.catalog.reload()
        if announce:
            self._emit(CATALOG_RELOADED_TOPIC, {"reloaded_at": _now().isoformat()}, cluster=True)

    # ── Validation ───────────────────────────────────────────────────────────

    def _check_type(self, entity_type: str) -> str:
        if entity_type not in self.catalog.entity_type_ids():
            raise RegistryError(f"Unknown entity_type '{entity_type}'")
        return entity_type

    def _check_schemes(self, identifiers: dict[str, str]) -> None:
        known = set(self.catalog.scheme_ids())
        unknown = sorted(set(identifiers) - known)
        if unknown:
            raise RegistryError(f"Unknown identifier scheme(s): {', '.join(unknown)}")

    def _check_relation(self, relation: str) -> str:
        if relation not in self.catalog.relation_ids():
            raise RegistryError(f"Unknown relation '{relation}'")
        return relation

    # ── Queries ──────────────────────────────────────────────────────────────

    def _resolve_live(self, s, uuid: str) -> Entity | None:
        """Follow merge tombstones to the surviving entity (bounded)."""
        seen: set[str] = set()
        current = s.get(Entity, uuid)
        while current is not None and current.status == "merged" and current.merged_into:
            if current.uuid in seen:  # defensive: broken chain
                return None
            seen.add(current.uuid)
            current = s.get(Entity, current.merged_into)
        return current

    def _entity_detail(self, s, entity: Entity) -> dict:
        data = entity.to_dict()
        data["identifiers"] = [
            i.to_dict()
            for i in s.execute(
                select(Identifier)
                .where(Identifier.entity_uuid == entity.uuid)
                .order_by(Identifier.scheme, Identifier.value)
            ).scalars()
        ]
        rels = s.execute(
            select(Relation).where(
                or_(Relation.from_uuid == entity.uuid, Relation.to_uuid == entity.uuid)
            )
        ).scalars().all()
        other_ids = {r.to_uuid if r.from_uuid == entity.uuid else r.from_uuid for r in rels}
        names = {}
        if other_ids:
            names = {
                e.uuid: (e.display_name, e.entity_type)
                for e in s.execute(select(Entity).where(Entity.uuid.in_(other_ids))).scalars()
            }
        data["relations"] = []
        for r in rels:
            outgoing = r.from_uuid == entity.uuid
            other = r.to_uuid if outgoing else r.from_uuid
            name, etype = names.get(other, (None, None))
            data["relations"].append({
                **r.to_dict(),
                "direction": "out" if outgoing else "in",
                "other_uuid": other,
                "other_display_name": name,
                "other_entity_type": etype,
            })
        data["maintenance"] = self.maintenance_for(s, [entity.uuid])[entity.uuid]
        return data

    def get_entity(self, uuid: str, *, follow_merge: bool = True) -> dict | None:
        self._require_ready()
        with get_session() as s:
            entity = self._resolve_live(s, uuid) if follow_merge else s.get(Entity, uuid)
            return self._entity_detail(s, entity) if entity else None

    def list_entities(
        self,
        *,
        entity_type: str | None = None,
        q: str | None = None,
        status: str | None = "active",
        limit: int = 200,
        offset: int = 0,
        in_maintenance: bool | None = None,
    ) -> dict:
        """List entities with identifiers and a compact ``maintenance`` state.

        ``in_maintenance=True`` narrows the SQL to entities that have maintenance
        rows at all, evaluates the page and keeps only the active ones — so
        ``total`` then counts the matches of this page, not the whole table.
        """
        self._require_ready()
        with get_session() as s:
            stmt = select(Entity)
            if status and status != "all":
                stmt = stmt.where(Entity.status == status)
            else:
                stmt = stmt.where(Entity.status != "merged")
            if entity_type:
                stmt = stmt.where(Entity.entity_type == entity_type)
            if q:
                like = f"%{q.strip()}%"
                matched = select(Identifier.entity_uuid).where(Identifier.value.like(like))
                stmt = stmt.where(
                    or_(Entity.display_name.like(like), Entity.uuid.in_(matched))
                )
            if in_maintenance:
                stmt = stmt.where(Entity.uuid.in_(select(MaintenanceEntry.entity_uuid)))
            total = s.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
            rows = s.execute(
                stmt.order_by(Entity.entity_type, Entity.display_name).limit(limit).offset(offset)
            ).scalars().all()
            entities = self._summaries(s, rows)
            if in_maintenance:
                entities = [e for e in entities if e["maintenance"]["active"]]
                total = len(entities)
            return {"total": total, "entities": entities}

    def lookup(self, scheme: str, value: str) -> dict | None:
        self._require_ready()
        with get_session() as s:
            ident = s.execute(
                select(Identifier).where(
                    Identifier.scheme == scheme.strip(), Identifier.value == str(value).strip()
                )
            ).scalar_one_or_none()
            if ident is None:
                return None
            entity = self._resolve_live(s, ident.entity_uuid)
            return self._entity_detail(s, entity) if entity else None

    def resolve(self, ref: str, *, entity_type: str | None = None, limit: int = 5) -> dict:
        """Find the one entity a human or another plugin means by ``ref``.

        ``ref`` may be a UUID (merge tombstones are followed), ``scheme:value``
        for a catalog scheme, a bare identifier value (any scheme, e.g.
        ``docker-hydra``) or a display name. Returns::

            {"entity": detail | None, "match": "uuid" | "identifier" |
             "display_name" | "ambiguous" | None, "candidates": [summary, ...]}

        Several different entities for one ``ref`` are *never* guessed between:
        ``entity`` stays None and ``candidates`` lists them so a UI can offer a
        choice. Nothing is created here — that is ``identify()``'s job.
        """
        self._require_ready()
        ref = (ref or "").strip()
        if not ref:
            raise RegistryError("ref is required")
        with get_session() as s:
            if _UUID_RE.match(ref):
                entity = self._resolve_live(s, ref.lower())
                if entity is not None and (entity_type is None or entity.entity_type == entity_type):
                    return {"entity": self._entity_detail(s, entity), "match": "uuid", "candidates": []}
                return {"entity": None, "match": None, "candidates": []}

            # "scheme:value" — only when the prefix is a catalog scheme, so values
            # such as FQDNs or 'hydra/8003' are never misread.
            scheme, sep, value = ref.partition(":")
            if sep and value and scheme in self.catalog.scheme_ids():
                where = (Identifier.scheme == scheme, Identifier.value == value.strip())
            else:
                where = (Identifier.value == ref,)
            hits: dict[str, Entity] = {}
            for ident in s.execute(select(Identifier).where(*where)).scalars():
                live = self._resolve_live(s, ident.entity_uuid)
                if live is None or live.status != "active":
                    continue
                if entity_type and live.entity_type != entity_type:
                    continue
                hits[live.uuid] = live
            if len(hits) == 1:
                return {"entity": self._entity_detail(s, next(iter(hits.values()))),
                        "match": "identifier", "candidates": []}
            if len(hits) > 1:
                return {"entity": None, "match": "ambiguous",
                        "candidates": self._summaries(s, list(hits.values())[:limit])}

            stmt = select(Entity).where(
                Entity.status == "active", func.lower(Entity.display_name) == ref.lower()
            )
            if entity_type:
                stmt = stmt.where(Entity.entity_type == entity_type)
            named = s.execute(stmt).scalars().all()
            if len(named) == 1:
                return {"entity": self._entity_detail(s, named[0]), "match": "display_name",
                        "candidates": []}
            if len(named) > 1:
                return {"entity": None, "match": "ambiguous",
                        "candidates": self._summaries(s, named[:limit])}

            near = self.list_entities(entity_type=entity_type, q=ref, limit=limit)["entities"]
            return {"entity": None, "match": None, "candidates": near}

    def _summaries(self, s, entities: list[Entity]) -> list[dict]:
        """List rows: ``to_dict()`` + identifiers + compact maintenance (two IN queries)."""
        uuids = [e.uuid for e in entities]
        idents: dict[str, list[dict]] = {u: [] for u in uuids}
        if uuids:
            for i in s.execute(select(Identifier).where(Identifier.entity_uuid.in_(uuids))).scalars():
                idents[i.entity_uuid].append(i.to_dict())
        states = self.maintenance_for(s, uuids)
        return [
            {**e.to_dict(), "identifiers": idents[e.uuid], "maintenance": mwx.compact(states[e.uuid])}
            for e in entities
        ]

    def get_stats(self) -> dict:
        self._require_ready()
        with get_session() as s:
            by_type = dict(
                s.execute(
                    select(Entity.entity_type, func.count())
                    .where(Entity.status == "active")
                    .group_by(Entity.entity_type)
                ).all()
            )
            open_conflicts = s.execute(
                select(func.count()).select_from(Conflict).where(Conflict.status == "open")
            ).scalar_one()
            identifiers = s.execute(select(func.count()).select_from(Identifier)).scalar_one()
            relations = s.execute(select(func.count()).select_from(Relation)).scalar_one()
        return {
            "total": sum(by_type.values()),
            "by_type": by_type,
            "identifiers": identifiers,
            "relations": relations,
            "open_conflicts": open_conflicts,
        }

    def health_probe(self) -> dict:
        """Cheap probe for ``health()`` (polled by every dashboard): one round
        trip plus the open-conflict count."""
        self._require_ready()
        with get_session() as s:
            s.execute(text("SELECT 1"))
            open_conflicts = s.execute(
                select(func.count()).select_from(Conflict).where(Conflict.status == "open")
            ).scalar_one()
        return {"open_conflicts": open_conflicts}

    # ── Reconciliation ───────────────────────────────────────────────────────

    def _write(self, tx, *, what: str):
        """Run a write transaction with the cluster retry policy.

        ``tx()`` must be one complete transaction (own session). It is re-run on
        MariaDB 1020/1205/1213 (``retry_write``) and once more after an
        IntegrityError — a concurrent writer on another instance inserted the
        same unique key first, so the re-run sees its row and continues from it.
        """
        try:
            return retry_write(tx)
        except IntegrityError:
            log.info(f"EntityRegistry: {what} lost a concurrent insert race — re-reading")
        try:
            return retry_write(tx)
        except IntegrityError as exc:
            raise RegistryError(
                f"Concurrent registration for the same identifier — retry ({what})"
            ) from exc

    def identify(
        self,
        entity_type: str,
        identifiers: dict,
        *,
        display_name: str | None = None,
        attributes: dict | None = None,
        source: str | None = None,
    ) -> dict:
        """Find-or-create the entity the identifiers belong to.

        Returns ``{uuid, created, conflict, entity}``. On a conflict the entity
        matched by the strongest (lowest-priority-number) scheme is returned and
        nothing is written except the conflict record — merging is a human
        decision.
        """
        self._require_ready()
        self._check_type(entity_type)
        idents = _norm_identifiers(identifiers)
        if not idents:
            raise RegistryError("At least one identifier is required")
        self._check_schemes(idents)
        source = (source or "").strip() or None

        result, events = self._write(
            lambda: self._identify_tx(entity_type, idents, display_name, attributes, source),
            what="identify",
        )
        for topic, payload in events:
            self._emit(topic, payload)
        return result

    def _identify_tx(self, entity_type, idents, display_name, attributes, source):
        """One identify() transaction → ``(result, events to emit after commit)``."""
        with get_session() as s:
            matches = s.execute(
                select(Identifier).where(
                    tuple_(Identifier.scheme, Identifier.value).in_(list(idents.items()))
                )
            ).scalars().all()

            # Collapse matches onto live entities (follow merge tombstones).
            matched: dict[str, list[str]] = {}
            for m in matches:
                live = self._resolve_live(s, m.entity_uuid)
                if live is None:
                    continue
                matched.setdefault(live.uuid, []).append(m.scheme)

            if not matched:
                entity = self._create(
                    s, entity_type, idents,
                    display_name=display_name, attributes=attributes, source=source,
                )
                result = {"uuid": entity.uuid, "created": True, "conflict": False,
                          "entity": self._entity_detail(s, entity)}
                return result, [("entity_registry:entity_created", {"entity": result["entity"]})]

            if len(matched) == 1:
                entity_uuid, schemes = next(iter(matched.items()))
                entity = s.get(Entity, entity_uuid)
                changes = self._absorb(
                    s, entity, idents,
                    display_name=display_name, attributes=attributes, source=source,
                )
                if entity.entity_type != entity_type:
                    log.warning(
                        f"identify: type mismatch for {entity.uuid} — stored "
                        f"'{entity.entity_type}', reported '{entity_type}' by {source}"
                    )
                    changes.append("type_mismatch")
                s.commit()
                s.refresh(entity)
                detail = self._entity_detail(s, entity)
                events = [("entity_registry:entity_updated",
                           {"entity": detail, "changes": changes})] if changes else []
                return {"uuid": entity.uuid, "created": False, "conflict": False,
                        "entity": detail}, events

            # Several entities claim these identifiers → conflict.
            winner_uuid = min(
                matched,
                key=lambda u: min(self.catalog.scheme_priority(sc) for sc in matched[u]),
            )
            conflict = self._file_conflict(s, entity_type, matched, idents, source)
            winner = s.get(Entity, winner_uuid)
            detail = self._entity_detail(s, winner)
            events = [("entity_registry:conflict", {"conflict": conflict})] if conflict else []
            return {"uuid": winner_uuid, "created": False, "conflict": True,
                    "entity": detail}, events

    def _create(self, s, entity_type, idents, *, display_name, attributes, source) -> Entity:
        entity = Entity(
            uuid=str(uuid4()),
            entity_type=entity_type,
            display_name=(display_name or "").strip() or next(iter(idents.values())),
            status="active",
            owner_source=source,
            sources=[source] if source else [],
            attributes=dict(attributes or {}),
            last_seen_at=_now(),
        )
        s.add(entity)
        s.flush()  # parent row first — Identifier FKs reference it
        for scheme, value in idents.items():
            s.add(Identifier(entity_uuid=entity.uuid, scheme=scheme, value=value, source=source))
        # An IntegrityError here means a concurrent identify() (possibly on
        # another instance) created the same key first; identify() re-runs the
        # lookup and absorbs into the winner (see _write).
        s.commit()
        s.refresh(entity)
        log.info(f"EntityRegistry: created {entity_type} '{entity.display_name}' ({entity.uuid})")
        return entity

    def _absorb(self, s, entity: Entity, idents, *, display_name, attributes, source) -> list[str]:
        """Apply a fresh observation to an existing entity. Returns change tags."""
        changes: list[str] = []
        existing = {
            (i.scheme, i.value)
            for i in s.execute(
                select(Identifier).where(Identifier.entity_uuid == entity.uuid)
            ).scalars()
        }
        for scheme, value in idents.items():
            if (scheme, value) not in existing:
                s.add(Identifier(entity_uuid=entity.uuid, scheme=scheme, value=value, source=source))
                changes.append(f"identifier:{scheme}")
        if display_name and display_name.strip() and display_name.strip() != entity.display_name:
            entity.display_name = display_name.strip()
            changes.append("display_name")
        if attributes:
            merged = {**(entity.attributes or {}), **attributes}
            if merged != (entity.attributes or {}):
                entity.attributes = merged
                changes.append("attributes")
        if source and source not in (entity.sources or []):
            entity.sources = [*(entity.sources or []), source]
            changes.append("sources")
        if entity.status == "retired":
            # Seen again by a source → it is back.
            entity.status = "active"
            changes.append("status")
        now = _now()
        if changes or entity.last_seen_at is None or now - entity.last_seen_at >= LAST_SEEN_RESOLUTION:
            entity.last_seen_at = now
        return changes

    def _file_conflict(self, s, entity_type, matched, idents, source) -> dict | None:
        """Record an open conflict once per candidate set; returns it if new."""
        candidate_uuids = sorted(matched)
        fingerprint = hashlib.sha256(
            f"{entity_type}|{'|'.join(candidate_uuids)}".encode()
        ).hexdigest()[:64]
        existing = s.execute(
            select(Conflict).where(Conflict.fingerprint == fingerprint, Conflict.status == "open")
        ).scalar_one_or_none()
        if existing is not None:
            return None
        names = {
            e.uuid: e.display_name
            for e in s.execute(select(Entity).where(Entity.uuid.in_(candidate_uuids))).scalars()
        }
        conflict = Conflict(
            entity_type=entity_type,
            fingerprint=fingerprint,
            candidates=[
                {"uuid": u, "display_name": names.get(u), "matched": sorted(matched[u])}
                for u in candidate_uuids
            ],
            identifiers=idents,
            source=source,
            status="open",
            open_fingerprint=fingerprint,
        )
        s.add(conflict)
        try:
            s.commit()
        except IntegrityError:
            # Another instance filed the same open conflict a moment ago.
            s.rollback()
            return None
        s.refresh(conflict)
        log.warning(
            f"EntityRegistry: conflict — {len(candidate_uuids)} {entity_type} entities match "
            f"{idents} (reported by {source})"
        )
        return conflict.to_dict()

    def register(
        self,
        uuid: str,
        entity_type: str,
        display_name: str,
        *,
        identifiers: dict | None = None,
        attributes: dict | None = None,
        source: str | None = None,
    ) -> tuple[dict, bool]:
        """Register an entity that brings its own UUID (e.g. an ITSM record).

        Idempotent on the UUID: a second call updates name/attributes and adds
        identifiers. Identifiers already owned by another entity are rejected.
        """
        self._require_ready()
        self._check_type(entity_type)
        uuid = (uuid or "").strip()
        if len(uuid) != 36:
            raise RegistryError("uuid must be a 36-char UUID string")
        idents = _norm_identifiers(identifiers)
        self._check_schemes(idents)
        source = (source or "").strip() or None

        detail, created, changes = self._write(
            lambda: self._register_tx(uuid, entity_type, display_name, idents, attributes, source),
            what="register",
        )
        if created:
            log.info(f"EntityRegistry: registered {entity_type} '{detail['display_name']}' ({uuid})")
            self._emit("entity_registry:entity_created", {"entity": detail})
        elif changes:
            self._emit("entity_registry:entity_updated", {"entity": detail, "changes": changes})
        return detail, created

    def _register_tx(self, uuid, entity_type, display_name, idents, attributes, source):
        with get_session() as s:
            if idents:
                owned = s.execute(
                    select(Identifier).where(
                        tuple_(Identifier.scheme, Identifier.value).in_(list(idents.items())),
                        Identifier.entity_uuid != uuid,
                    )
                ).scalars().all()
                if owned:
                    taken = ", ".join(f"{o.scheme}={o.value}" for o in owned)
                    raise RegistryError(f"Identifier(s) already owned by another entity: {taken}")

            entity = s.get(Entity, uuid)
            created = entity is None
            if created:
                entity = Entity(
                    uuid=uuid, entity_type=entity_type,
                    display_name=(display_name or "").strip() or uuid,
                    status="active", owner_source=source,
                    sources=[source] if source else [],
                    attributes=dict(attributes or {}), last_seen_at=_now(),
                )
                s.add(entity)
                s.flush()  # parent row first — Identifier FKs reference it
                for scheme, value in idents.items():
                    s.add(Identifier(entity_uuid=uuid, scheme=scheme, value=value, source=source))
                changes: list[str] = []
            else:
                if entity.status == "merged":
                    raise RegistryError(f"Entity {uuid} was merged into {entity.merged_into}")
                changes = self._absorb(
                    s, entity, idents,
                    display_name=display_name, attributes=attributes, source=source,
                )
            s.commit()
            s.refresh(entity)
            return self._entity_detail(s, entity), created, changes

    def update_entity(
        self,
        uuid: str,
        *,
        display_name: str | None = None,
        attributes: dict | None = None,
        replace_attributes: bool = False,
    ) -> dict | None:
        self._require_ready()
        res = self._write(
            lambda: self._update_tx(uuid, display_name, attributes, replace_attributes),
            what="update_entity",
        )
        if res is None:
            return None
        detail, changes = res
        if changes:
            self._emit("entity_registry:entity_updated", {"entity": detail, "changes": changes})
        return detail

    def _update_tx(self, uuid, display_name, attributes, replace_attributes):
        with get_session() as s:
            entity = s.get(Entity, uuid)
            if entity is None or entity.status == "merged":
                return None
            changes: list[str] = []
            if display_name is not None and display_name.strip() != entity.display_name:
                entity.display_name = display_name.strip() or entity.display_name
                changes.append("display_name")
            if attributes is not None:
                new = dict(attributes) if replace_attributes else {**(entity.attributes or {}), **attributes}
                if new != (entity.attributes or {}):
                    entity.attributes = new
                    changes.append("attributes")
            s.commit()
            s.refresh(entity)
            return self._entity_detail(s, entity), changes

    def retire(self, uuid: str, *, source: str | None = None) -> dict | None:
        self._require_ready()
        with get_session() as s:
            entity = s.get(Entity, uuid)
            if entity is None or entity.status == "merged":
                return None
            if entity.status != "retired":
                entity.status = "retired"
                s.commit()
                s.refresh(entity)
            detail = self._entity_detail(s, entity)
        log.info(f"EntityRegistry: retired '{detail['display_name']}' ({uuid}) by {source}")
        self._emit("entity_registry:entity_retired", {"entity": detail})
        return detail

    def merge(self, keep_uuid: str, drop_uuid: str, *, source: str | None = None) -> dict:
        """Fold ``drop`` into ``keep``: re-point identifiers and relations,
        union attributes/sources, leave a tombstone so old references resolve."""
        self._require_ready()
        if keep_uuid == drop_uuid:
            raise RegistryError("keep and drop must differ")
        detail = self._write(lambda: self._merge_tx(keep_uuid, drop_uuid), what="merge")
        log.info(f"EntityRegistry: merged {drop_uuid} into {keep_uuid} by {source}")
        self._emit("entity_registry:entity_merged", {"keep": detail, "drop_uuid": drop_uuid})
        return detail

    def _merge_tx(self, keep_uuid: str, drop_uuid: str) -> dict:
        with get_session() as s:
            keep, drop = s.get(Entity, keep_uuid), s.get(Entity, drop_uuid)
            if keep is None or drop is None:
                raise NotFound("Unknown entity uuid")
            if keep.status == "merged" or drop.status == "merged":
                raise RegistryError("Cannot merge an already merged entity")

            for ident in s.execute(
                select(Identifier).where(Identifier.entity_uuid == drop_uuid)
            ).scalars():
                ident.entity_uuid = keep_uuid

            existing_rel = {
                (r.from_uuid, r.to_uuid, r.relation)
                for r in s.execute(
                    select(Relation).where(
                        or_(Relation.from_uuid == keep_uuid, Relation.to_uuid == keep_uuid)
                    )
                ).scalars()
            }
            for rel in s.execute(
                select(Relation).where(
                    or_(Relation.from_uuid == drop_uuid, Relation.to_uuid == drop_uuid)
                )
            ).scalars().all():
                new_from = keep_uuid if rel.from_uuid == drop_uuid else rel.from_uuid
                new_to = keep_uuid if rel.to_uuid == drop_uuid else rel.to_uuid
                key = (new_from, new_to, rel.relation)
                if new_from == new_to or key in existing_rel:
                    s.delete(rel)  # self-loop or duplicate after re-pointing
                    continue
                rel.from_uuid, rel.to_uuid = new_from, new_to
                existing_rel.add(key)

            keep.attributes = {**(drop.attributes or {}), **(keep.attributes or {})}
            keep.sources = list(dict.fromkeys([*(keep.sources or []), *(drop.sources or [])]))
            if drop.status == "active" and keep.status == "retired":
                keep.status = "active"
            keep.last_seen_at = max(filter(None, [keep.last_seen_at, drop.last_seen_at, _now()]))

            drop.status = "merged"
            drop.merged_into = keep_uuid

            # Any open conflict that involved the dropped entity is settled.
            for c in s.execute(select(Conflict).where(Conflict.status == "open")).scalars().all():
                if any(cand.get("uuid") == drop_uuid for cand in (c.candidates or [])):
                    c.status = "resolved"
                    c.open_fingerprint = None
                    c.resolution = f"merged {drop_uuid} into {keep_uuid}"
                    c.resolved_at = _now()

            s.commit()
            s.refresh(keep)
            return self._entity_detail(s, keep)

    # ── Relations ────────────────────────────────────────────────────────────

    def relate(self, from_uuid: str, to_uuid: str, relation: str, *, source: str | None = None) -> dict:
        self._require_ready()
        self._check_relation(relation)
        if from_uuid == to_uuid:
            raise RegistryError("An entity cannot relate to itself")
        with get_session() as s:
            a, b = self._resolve_live(s, from_uuid), self._resolve_live(s, to_uuid)
            if a is None or b is None:
                raise NotFound("Unknown entity uuid")
            existing = s.execute(
                select(Relation).where(
                    Relation.from_uuid == a.uuid, Relation.to_uuid == b.uuid,
                    Relation.relation == relation,
                )
            ).scalar_one_or_none()
            if existing is not None:
                return existing.to_dict()
            rel = Relation(from_uuid=a.uuid, to_uuid=b.uuid, relation=relation, source=source)
            s.add(rel)
            s.commit()
            s.refresh(rel)
            data = rel.to_dict()
            a_detail = self._entity_detail(s, a)
        self._emit("entity_registry:entity_updated", {"entity": a_detail, "changes": ["relations"]})
        return data

    def unrelate(self, from_uuid: str, to_uuid: str, relation: str) -> bool:
        self._require_ready()
        with get_session() as s:
            rel = s.execute(
                select(Relation).where(
                    Relation.from_uuid == from_uuid, Relation.to_uuid == to_uuid,
                    Relation.relation == relation,
                )
            ).scalar_one_or_none()
            if rel is None:
                return False
            s.delete(rel)
            s.commit()
        return True

    def host_of(self, uuid: str) -> dict | None:
        """The host a service/container runs on: the target of its outgoing
        ``runs_on`` relation (merge tombstones followed on both ends).
        ``None`` when no such relation exists; ``NotFound`` for an unknown uuid."""
        self._require_ready()
        with get_session() as s:
            entity = self._resolve_live(s, uuid)
            if entity is None:
                raise NotFound("Unknown entity uuid")
            rels = s.execute(
                select(Relation)
                .where(Relation.from_uuid == entity.uuid, Relation.relation == "runs_on")
                .order_by(Relation.id)
            ).scalars().all()
            for rel in rels:
                host = self._resolve_live(s, rel.to_uuid)
                if host is not None and host.status != "merged":
                    return {"host": self._entity_detail(s, host), "relation": rel.to_dict()}
            return None

    # ── Maintenance read-model (0.3.0) ───────────────────────────────────────
    # Fed by maintenance:windows_published / override_started / override_ended.
    # State lives in registry_maintenance; current/next window and override
    # expiry are evaluated when read (app/logic/maintenance.py), never stored.

    def apply_windows(self, source: str, items: list, *, replaces: bool = True) -> dict:
        """Store the window set one emitter published (``windows_published``).

        ``replaces=True`` drops every earlier window row of ``source`` in the
        same transaction, so a duplicate delivery (or one on another instance)
        ends in the same state. ``ci_id``s follow merge tombstones; unknown ones
        are skipped and counted — the registry never creates entities here. The
        item's ``kind`` is informational only (the entity's type is
        authoritative). One ``entity_registry:maintenance_changed`` is emitted
        per call instead of an ``entity_updated`` per entity.
        """
        self._require_ready()
        source = (source or "").strip()
        if not source or len(source) > mwx.MAX_SOURCE_LEN:
            raise RegistryError("source is required (1-64 chars)")
        if not isinstance(items, list):
            raise RegistryError("items must be a list")
        normalized: list[dict] = []
        invalid = 0
        for item in items:
            norm = mwx.normalize_item(item)
            if norm is None:
                invalid += 1
            else:
                normalized.append(norm)
        stored, unknown = self._write(
            lambda: self._apply_windows_tx(source, normalized, replaces), what="apply_windows"
        )
        if unknown or invalid:
            log.info(
                f"EntityRegistry: maintenance windows from '{source}' — {stored} stored, "
                f"{unknown} unknown ci_id(s) skipped, {invalid} invalid item(s) skipped"
            )
        self._emit(MAINTENANCE_CHANGED_TOPIC, {
            "source": source, "kind": "window", "count": stored,
            "skipped_unknown": unknown, "skipped_invalid": invalid,
        }, cluster=True)
        return {"source": source, "count": stored, "skipped_unknown": unknown,
                "skipped_invalid": invalid}

    def _apply_windows_tx(self, source: str, items: list[dict], replaces: bool) -> tuple[int, int]:
        with get_session() as s:
            if replaces:
                s.execute(
                    delete(MaintenanceEntry).where(
                        MaintenanceEntry.source == source, MaintenanceEntry.kind == "window"
                    )
                )
            by_uuid: dict[str, dict] = {}
            unknown = 0
            for item in items:
                entity = self._resolve_live(s, item["ci_id"])
                if entity is None:
                    unknown += 1
                    continue
                by_uuid[entity.uuid] = item  # two tombstones of one entity: last wins
            existing: dict[str, MaintenanceEntry] = {}
            if by_uuid and not replaces:
                existing = {
                    row.entity_uuid: row
                    for row in s.execute(
                        select(MaintenanceEntry).where(
                            MaintenanceEntry.kind == "window",
                            MaintenanceEntry.source == source,
                            MaintenanceEntry.entity_uuid.in_(list(by_uuid)),
                        )
                    ).scalars()
                }
            now = _now()
            for uuid, item in by_uuid.items():
                row = existing.get(uuid)
                if row is None:
                    row = MaintenanceEntry(entity_uuid=uuid, kind="window", source=source, override_id="")
                    s.add(row)
                row.windows = item["windows"]
                row.inherited_from = item["inherited_from"]
                row.deploy_policy = item["deploy_policy"]
                row.incidents = item["incidents"]
                row.updated_at = now
            s.commit()
            return len(by_uuid), unknown

    def start_override(self, payload: dict) -> dict:
        """Upsert one override row per ``(entity, source, override_id)`` for
        every known ``ci_id`` (``maintenance:override_started``)."""
        self._require_ready()
        try:
            ov = mwx.parse_override_started(payload)
        except mwx.MaintenancePayloadError as exc:
            raise RegistryError(str(exc)) from exc
        stored, unknown = self._write(lambda: self._start_override_tx(ov), what="start_override")
        self._purge_quietly()
        self._emit(MAINTENANCE_CHANGED_TOPIC, {
            "source": ov["source"], "kind": "override", "override_id": ov["override_id"],
            "count": stored, "skipped_unknown": unknown,
        }, cluster=True)
        return {"override_id": ov["override_id"], "source": ov["source"], "count": stored,
                "skipped_unknown": unknown}

    def _start_override_tx(self, ov: dict) -> tuple[int, int]:
        with get_session() as s:
            targets: dict[str, None] = {}
            unknown = 0
            for ci_id in ov["ci_ids"]:
                entity = self._resolve_live(s, ci_id)
                if entity is None:
                    unknown += 1
                else:
                    targets[entity.uuid] = None
            rows: dict[str, MaintenanceEntry] = {}
            if targets:
                rows = {
                    row.entity_uuid: row
                    for row in s.execute(
                        select(MaintenanceEntry).where(
                            MaintenanceEntry.kind == "override",
                            MaintenanceEntry.source == ov["source"],
                            MaintenanceEntry.override_id == ov["override_id"],
                            MaintenanceEntry.entity_uuid.in_(list(targets)),
                        )
                    ).scalars()
                }
            now = _now()
            for uuid in targets:
                row = rows.get(uuid)
                if row is None:
                    row = MaintenanceEntry(
                        entity_uuid=uuid, kind="override", source=ov["source"],
                        override_id=ov["override_id"],
                    )
                    s.add(row)
                row.until = mwx.to_db(ov["until"])
                row.started_at = mwx.to_db(ov["started_at"])
                row.linger_minutes = ov["linger_minutes"]
                row.reason = ov["reason"] or None
                # ended_at is left alone: a re-delivered "started" after the
                # "ended" was processed must not revive the override.
                row.updated_at = now
            s.commit()
            return len(targets), unknown

    def end_override(self, payload: dict) -> dict:
        """Mark every row of an override as ended (``maintenance:override_ended``);
        it keeps counting as active for ``linger_minutes`` and expires by itself."""
        self._require_ready()
        try:
            ov = mwx.parse_override_ended(payload)
        except mwx.MaintenancePayloadError as exc:
            raise RegistryError(str(exc)) from exc
        count = self._write(lambda: self._end_override_tx(ov), what="end_override")
        self._purge_quietly()
        self._emit(MAINTENANCE_CHANGED_TOPIC, {
            "source": ov["source"], "kind": "override", "override_id": ov["override_id"],
            "count": count, "ended": True,
        }, cluster=True)
        return {"override_id": ov["override_id"], "count": count}

    def _end_override_tx(self, ov: dict) -> int:
        with get_session() as s:
            stmt = select(MaintenanceEntry).where(
                MaintenanceEntry.kind == "override",
                MaintenanceEntry.override_id == ov["override_id"],
            )
            if ov["source"]:  # never end another emitter's override
                stmt = stmt.where(MaintenanceEntry.source == ov["source"])
            rows = s.execute(stmt).scalars().all()
            now = _now()
            for row in rows:
                if row.ended_at is None:  # first "ended" wins — idempotent on re-delivery
                    row.ended_at = mwx.to_db(ov["ended_at"])
                if ov["linger_minutes"] is not None:
                    row.linger_minutes = ov["linger_minutes"]
                elif row.linger_minutes is None:
                    row.linger_minutes = mwx.DEFAULT_LINGER_MINUTES
                row.updated_at = now
            s.commit()
            return len(rows)

    def purge_expired(self, now: datetime | None = None) -> int:
        """Delete override rows whose effective end lies more than ``PURGE_GRACE``
        in the past. ``ended_at + linger`` is not expressible portably in SQL, so
        ended rows get a second day of grace (linger is minutes, never days)."""
        self._require_ready()
        now = now or mwx.utcnow()
        open_cutoff = mwx.to_db(now - PURGE_GRACE)
        ended_cutoff = mwx.to_db(now - 2 * PURGE_GRACE)

        def _tx() -> int:
            with get_session() as s:
                res = s.execute(
                    delete(MaintenanceEntry).where(
                        MaintenanceEntry.kind == "override",
                        or_(
                            (MaintenanceEntry.ended_at.is_(None)) & (MaintenanceEntry.until < open_cutoff),
                            (MaintenanceEntry.ended_at.is_not(None)) & (MaintenanceEntry.ended_at < ended_cutoff),
                        ),
                    )
                )
                s.commit()
                return int(res.rowcount or 0)

        removed = self._write(_tx, what="purge_expired")
        if removed:
            log.info(f"EntityRegistry: purged {removed} expired maintenance override(s)")
        return removed

    def _purge_quietly(self) -> None:
        """Housekeeping must never fail the event that triggered it."""
        try:
            self.purge_expired()
        except Exception as exc:  # pragma: no cover - defensive
            log.debug(f"EntityRegistry: purge_expired skipped — {exc}")

    def maintenance_for(self, s, uuids: list[str], now: datetime | None = None) -> dict[str, dict]:
        """Read-model state for several entities — one ``IN`` query, evaluated now.

        Every requested uuid is a key; entities without rows get the empty
        state (``active: False``). See ``maintenance.evaluate`` for the shape.
        """
        now = now or mwx.utcnow()
        out = {u: mwx.empty_state() for u in uuids}
        if not uuids:
            return out
        rows = s.execute(
            select(MaintenanceEntry)
            .where(MaintenanceEntry.entity_uuid.in_(uuids))
            .order_by(MaintenanceEntry.source, MaintenanceEntry.id)
        ).scalars().all()
        grouped: dict[str, tuple[list, list]] = {}
        for row in rows:
            wins, ovs = grouped.setdefault(row.entity_uuid, ([], []))
            (wins if row.kind == "window" else ovs).append(row)
        for uuid, (wins, ovs) in grouped.items():
            out[uuid] = mwx.evaluate(wins, ovs, now)
        return out

    def get_maintenance(self, ci_ids: list[str], now: datetime | None = None) -> dict:
        """Batch read-model for consumers (``GET /maintenance?ci_ids=``).

        Keys are the ids as requested; merge tombstones resolve to the survivor's
        state, unknown ids map to ``None``.
        """
        self._require_ready()
        now = now or mwx.utcnow()
        ids = list(dict.fromkeys(c.strip() for c in ci_ids if isinstance(c, str) and c.strip()))
        with get_session() as s:
            found = {
                e.uuid: e
                for e in s.execute(select(Entity).where(Entity.uuid.in_(ids))).scalars()
            } if ids else {}
            live_of: dict[str, str | None] = {}
            for ci_id in ids:
                entity = found.get(ci_id)
                if entity is not None and entity.status == "merged":
                    entity = self._resolve_live(s, ci_id)
                live_of[ci_id] = entity.uuid if entity is not None else None
            states = self.maintenance_for(s, sorted({u for u in live_of.values() if u}), now)
        return {
            "now": mwx.iso(now),
            "maintenance": {
                ci_id: ({**states[live], "uuid": live} if live else None)
                for ci_id, live in live_of.items()
            },
        }

    # ── Conflicts ────────────────────────────────────────────────────────────

    def list_conflicts(self, *, status: str | None = "open") -> list[dict]:
        self._require_ready()
        with get_session() as s:
            stmt = select(Conflict).order_by(Conflict.created_at.desc())
            if status and status != "all":
                stmt = stmt.where(Conflict.status == status)
            return [c.to_dict() for c in s.execute(stmt).scalars()]

    def dismiss_conflict(self, conflict_id: int, *, reason: str | None = None) -> dict | None:
        self._require_ready()
        with get_session() as s:
            c = s.get(Conflict, conflict_id)
            if c is None:
                return None
            if c.status == "open":
                c.status = "dismissed"
                c.open_fingerprint = None
                c.resolution = (reason or "dismissed")[:2000]
                c.resolved_at = _now()
                s.commit()
                s.refresh(c)
            return c.to_dict()

    # ── Private ──────────────────────────────────────────────────────────────

    def _emit(self, topic: str, payload: dict, *, cluster: bool = False) -> None:
        """Emit on the bus; ``cluster=True`` also reaches the other core instances."""
        if not self._ctx:
            return
        if cluster:
            emit = functools.partial(self._emit_cluster, topic, payload)
        else:
            emit = functools.partial(self._ctx.emit, topic, payload)
        # Service methods run in to_thread workers; async bus subscribers need
        # the loop thread, so marshal emits back onto it.
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        loop = self._loop
        if running is None and loop is not None and loop.is_running():
            loop.call_soon_threadsafe(emit)
            return
        emit()

    def _emit_cluster(self, topic: str, payload: dict) -> None:
        try:
            self._ctx.emit(topic, payload, cluster=True)
        except TypeError:  # core without cluster bus (API < 1.7): local only
            self._ctx.emit(topic, payload)


entity_registry_service = EntityRegistryService()
