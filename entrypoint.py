"""lyndrix-plugin-entity-registry — Plugin entrypoint.

The Entity Registry gives every configuration item (host, hypervisor,
service, container, ITSM record, ...) one stable UUID that all other plugins
can store, and reconciles identities so a renamed host keeps its id.

Architecture
------------
  catalog/entity_types.yml   — entity types, identifier schemes, relations
  app/model/models.py        — Entity / Identifier / Relation / Conflict
  app/model/database.py      — session helpers + table bootstrap
  app/logic/catalog.py       — validated catalog loader
  app/logic/service.py       — identify / register / merge / relations
  app/api/__init__.py        — REST router (both UIs use only this)
  app/ui/react/              — React UI (bundle → app/ui/static/ui_bundle.js)

Events consumed
---------------
  entity_registry:identify   — {entity_type, identifiers, display_name?,
                                attributes?, source, request_id?}
                               or {items: [...], request_id?} for a batch.
                               Reply on entity_registry:identified echoing
                               request_id: {ok, uuid, created, conflict} or
                               {ok, results: [...]}. Use ctx.request() for the
                               round-trip.
  entity_registry:register   — {uuid, entity_type, display_name, identifiers?,
                                attributes?, source}  (bring-your-own UUID)
  entity_registry:lookup     — {ref, entity_type?, request_id} or
                               {scheme, value, request_id}. Read-only resolve of
                               a UUID / scheme:value / identifier value / display
                               name. Reply on entity_registry:lookup_result:
                               {request_id, ok, entity|null, match, candidates}.
  entity_registry:relate     — {from_uuid, to_uuid, relation, source?}
  entity_registry:unrelate   — {from_uuid, to_uuid, relation}
  entity_registry:retire     — {uuid, source?}

Events emitted
--------------
  entity_registry:identified     — reply to identify (see above)
  entity_registry:lookup_result  — reply to lookup (see above)
  entity_registry:entity_created — {entity}
  entity_registry:entity_updated — {entity, changes}
  entity_registry:entity_merged  — {keep, drop_uuid}
  entity_registry:entity_retired — {entity}
  entity_registry:conflict       — {conflict}
  entity_registry:ingest_failed  — {topic, error, payload_keys}
"""
import asyncio
import time

from core.api import ModuleManifest, PluginHealthStatus, db_instance

from .app.api import IdentifyPayload, RegisterPayload, build_plugin_router
from .app.logic.service import NotFound, RegistryError
from .app.logic.service import entity_registry_service as svc

PLUGIN_ID = "lyndrix.plugin.entity_registry"

# ── Manifest ──────────────────────────────────────────────────────────────────

manifest = ModuleManifest(
    id=PLUGIN_ID,
    name="Entity Registry",
    version="0.1.0",
    description=(
        "Stable UUIDs for every configuration item. Reconciles identities across "
        "plugins (IaC, Server Manager, Monitoring, ITSM) so a renamed host keeps its id."
    ),
    author="Lyndrix",
    icon="fingerprint",
    type="PLUGIN",
    min_core_version="1.4.0",
    auto_enable_on_install=True,
    repo_url="https://github.com/lyndrix-platform/lyndrix-plugin-entity-registry",
    ui_route="/registry",
    react_ui=True,
    i18n_namespace="entity_registry",
    react_routes=[
        {"path": "/registry", "label": "Entity Registry", "icon": "fingerprint",
         "sidebar_visible": True},
    ],
    permissions={
        "subscribe": [
            "db:connected",
            "entity_registry:identify",
            "entity_registry:register",
            "entity_registry:lookup",
            "entity_registry:relate",
            "entity_registry:unrelate",
            "entity_registry:retire",
        ],
        "emit": [
            "entity_registry:identified",
            "entity_registry:lookup_result",
            "entity_registry:entity_created",
            "entity_registry:entity_updated",
            "entity_registry:entity_merged",
            "entity_registry:entity_retired",
            "entity_registry:conflict",
            "entity_registry:ingest_failed",
        ],
    },
    roles=[
        {"id": "admin", "label": "Entity Registry Administrator",
         "permissions": [f"plugin:{PLUGIN_ID}:api:read", f"plugin:{PLUGIN_ID}:api:write"],
         "auto_map_groups": ["INT_ADMIN"],
         "description": "Identify/register entities, merge conflicts, manage relations."},
        {"id": "viewer", "label": "Entity Registry Viewer",
         "permissions": [f"plugin:{PLUGIN_ID}:api:read"],
         "auto_map_groups": [],
         "description": "Read-only access to entities, identifiers and relations."},
    ],
)


# ── Health ────────────────────────────────────────────────────────────────────

async def health(ctx) -> PluginHealthStatus:
    start = time.perf_counter()
    if not db_instance.is_connected:
        return PluginHealthStatus(status="error", details={"reason": "db_unavailable"})
    if not svc.is_ready:
        return PluginHealthStatus(status="error", details={"reason": "tables_not_initialized"})
    try:
        stats = await asyncio.to_thread(svc.get_stats)
    except Exception as exc:
        return PluginHealthStatus(
            status="error", details={"reason": "db_query_failed", "error": str(exc)},
            latency_ms=round((time.perf_counter() - start) * 1000, 1),
        )
    details = {"db_connected": True, "ready": True, **stats}
    status = "degraded" if stats.get("open_conflicts") else "ok"
    if status == "degraded":
        details["reason"] = "open_conflicts"
    return PluginHealthStatus(
        status=status, details=details, latency_ms=round((time.perf_counter() - start) * 1000, 1)
    )


# ── Setup ─────────────────────────────────────────────────────────────────────

def setup(ctx):
    ctx.log.info("Entity Registry: setup started")
    svc.set_context(ctx)
    svc.catalog.load()  # fail loudly on a broken catalog
    ctx.register_routes(build_plugin_router(svc))

    def _db_init(reason: str) -> None:
        try:
            svc.on_db_connected()
            ctx.log.info(f"Entity Registry: tables ready ({reason})")
        except Exception as exc:
            ctx.log.error(f"Entity Registry: DB init failed — {exc}")

    # db:connected fires before plugin activation; bootstrap now if possible and
    # keep listening for reconnects.
    if db_instance.is_connected:
        _db_init("DB already connected")

    @ctx.subscribe("db:connected")
    async def _on_db_connected(payload):
        _db_init("db:connected")

    # ── Inbound bus API ──────────────────────────────────────────────────────
    def _fail(topic: str, payload, error: str) -> None:
        keys = sorted(payload.keys()) if isinstance(payload, dict) else []
        ctx.log.warning(f"Entity Registry: {topic} rejected — {error}")
        ctx.emit("entity_registry:ingest_failed", {"topic": topic, "error": error,
                                                   "payload_keys": keys})

    def _identify_one(data: dict) -> dict:
        item = IdentifyPayload.model_validate(data)
        res = svc.identify(
            item.entity_type, item.identifiers,
            display_name=item.display_name, attributes=item.attributes, source=item.source,
        )
        return {"ok": True, "uuid": res["uuid"], "created": res["created"],
                "conflict": res["conflict"]}

    @ctx.subscribe("entity_registry:identify")
    async def _on_identify(payload):
        topic = "entity_registry:identify"
        if not isinstance(payload, dict):
            _fail(topic, payload, "payload must be an object")
            return
        request_id = payload.get("request_id")
        reply: dict = {"request_id": request_id}
        if not svc.is_ready:
            reply.update(ok=False, error="database not ready")
        elif isinstance(payload.get("items"), list):
            results = []
            for item in payload["items"]:
                try:
                    results.append(await asyncio.to_thread(_identify_one, item))
                except Exception as exc:  # pydantic / RegistryError / DB
                    results.append({"ok": False, "error": str(exc),
                                    "identifiers": (item or {}).get("identifiers")
                                    if isinstance(item, dict) else None})
            reply.update(ok=True, results=results)
        else:
            try:
                data = {k: v for k, v in payload.items() if k != "request_id"}
                reply.update(await asyncio.to_thread(_identify_one, data))
            except Exception as exc:
                reply.update(ok=False, error=str(exc))
        if request_id is not None:
            ctx.emit("entity_registry:identified", reply)
        elif not reply.get("ok"):
            _fail(topic, payload, str(reply.get("error")))

    @ctx.subscribe("entity_registry:register")
    async def _on_register(payload):
        topic = "entity_registry:register"
        if not isinstance(payload, dict):
            _fail(topic, payload, "payload must be an object")
            return
        if not svc.is_ready:
            _fail(topic, payload, "database not ready")
            return
        try:
            item = RegisterPayload.model_validate(payload)
            await asyncio.to_thread(
                svc.register, item.uuid, item.entity_type, item.display_name,
                identifiers=item.identifiers, attributes=item.attributes, source=item.source,
            )
        except Exception as exc:
            _fail(topic, payload, str(exc))

    @ctx.subscribe("entity_registry:lookup")
    async def _on_lookup(payload):
        """Read-only resolve for other plugins (ITSM CI links, event correlation)."""
        topic = "entity_registry:lookup"
        if not isinstance(payload, dict):
            _fail(topic, payload, "payload must be an object")
            return
        request_id = payload.get("request_id")
        reply: dict = {"request_id": request_id}
        ref = payload.get("ref") or payload.get("uuid")
        if payload.get("scheme") and payload.get("value"):
            ref = f"{payload['scheme']}:{payload['value']}"
        if not svc.is_ready:
            reply.update(ok=False, error="database not ready")
        elif not ref:
            reply.update(ok=False, error="ref (uuid, scheme:value, identifier value or name) required")
        else:
            try:
                res = await asyncio.to_thread(
                    svc.resolve, str(ref), entity_type=payload.get("entity_type")
                )
                reply.update(ok=True, **res)
            except Exception as exc:
                reply.update(ok=False, error=str(exc))
        if request_id is not None:
            ctx.emit("entity_registry:lookup_result", reply)
        elif not reply.get("ok"):
            _fail(topic, payload, str(reply.get("error")))

    @ctx.subscribe("entity_registry:relate")
    async def _on_relate(payload):
        topic = "entity_registry:relate"
        data = payload if isinstance(payload, dict) else {}
        if not all(data.get(k) for k in ("from_uuid", "to_uuid", "relation")):
            _fail(topic, payload, "from_uuid, to_uuid and relation are required")
            return
        if not svc.is_ready:
            _fail(topic, payload, "database not ready")
            return
        try:
            await asyncio.to_thread(
                svc.relate, str(data["from_uuid"]), str(data["to_uuid"]),
                str(data["relation"]), source=data.get("source"),
            )
        except (RegistryError, NotFound) as exc:
            _fail(topic, payload, str(exc))
        except Exception as exc:
            ctx.log.error(f"Entity Registry: relate failed — {exc}")

    @ctx.subscribe("entity_registry:unrelate")
    async def _on_unrelate(payload):
        data = payload if isinstance(payload, dict) else {}
        if not all(data.get(k) for k in ("from_uuid", "to_uuid", "relation")) or not svc.is_ready:
            _fail("entity_registry:unrelate", payload, "from_uuid, to_uuid, relation required / DB ready")
            return
        try:
            await asyncio.to_thread(
                svc.unrelate, str(data["from_uuid"]), str(data["to_uuid"]), str(data["relation"])
            )
        except Exception as exc:
            ctx.log.error(f"Entity Registry: unrelate failed — {exc}")

    @ctx.subscribe("entity_registry:retire")
    async def _on_retire(payload):
        data = payload if isinstance(payload, dict) else {}
        if not data.get("uuid") or not svc.is_ready:
            _fail("entity_registry:retire", payload, "uuid required / DB ready")
            return
        try:
            await asyncio.to_thread(svc.retire, str(data["uuid"]), source=data.get("source"))
        except Exception as exc:
            ctx.log.error(f"Entity Registry: retire failed — {exc}")

    ctx.log.info("Entity Registry: setup complete")
