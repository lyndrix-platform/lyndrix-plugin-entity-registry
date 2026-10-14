"""REST API for the Entity Registry.

Mounted by core at ``/api/plugins/lyndrix.plugin.entity_registry/`` via
``ctx.register_routes()``. The registry enforces authentication; routes add
the plugin-scoped ``api:read`` / ``api:write`` permissions. Every handler
does its (sync SQLAlchemy) work off the event loop via ``asyncio.to_thread``.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from core.api import ApiIdentity, require_permission

from ..logic.service import EntityRegistryService, NotFound, RegistryError

PLUGIN_ID = "lyndrix.plugin.entity_registry"
PERM_READ = f"plugin:{PLUGIN_ID}:api:read"
PERM_WRITE = f"plugin:{PLUGIN_ID}:api:write"


# ── Payloads ───────────────────────────────────────────────────────────────────

class IdentifyPayload(BaseModel):
    entity_type: str
    identifiers: Dict[str, str] = Field(min_length=1)
    display_name: Optional[str] = None
    attributes: Dict[str, Any] = Field(default_factory=dict)
    source: str = Field(min_length=1, max_length=64)


class IdentifyBatchPayload(BaseModel):
    items: List[IdentifyPayload] = Field(min_length=1, max_length=1000)


class RegisterPayload(BaseModel):
    uuid: str = Field(min_length=36, max_length=36)
    entity_type: str
    display_name: str
    identifiers: Dict[str, str] = Field(default_factory=dict)
    attributes: Dict[str, Any] = Field(default_factory=dict)
    source: str = Field(min_length=1, max_length=64)


class EntityUpdatePayload(BaseModel):
    display_name: Optional[str] = None
    attributes: Optional[Dict[str, Any]] = None
    replace_attributes: bool = False


class MergePayload(BaseModel):
    drop_uuid: str = Field(min_length=36, max_length=36)


class RelationPayload(BaseModel):
    to_uuid: str = Field(min_length=36, max_length=36)
    relation: str


class DismissPayload(BaseModel):
    reason: Optional[str] = None


# ── Router ─────────────────────────────────────────────────────────────────────

def build_plugin_router(service: EntityRegistryService) -> APIRouter:
    router = APIRouter(tags=["Entity Registry"])

    def _ensure_ready() -> None:
        if not service.is_ready:
            raise HTTPException(
                status_code=503, detail="Entity Registry database not ready — retry shortly."
            )

    async def _call(fn, *args, **kwargs):
        """Run a service call off-loop and map domain errors to HTTP codes."""
        try:
            return await asyncio.to_thread(fn, *args, **kwargs)
        except RegistryError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except NotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    # ── Entities ───────────────────────────────────────────────────────────────
    @router.get("/entities")
    async def list_entities(
        type: Optional[str] = None,  # noqa: A002 — query param name
        q: Optional[str] = None,
        status: str = "active",
        limit: int = Query(200, ge=1, le=1000),
        offset: int = Query(0, ge=0),
        identity: ApiIdentity = Depends(require_permission(PERM_READ)),
    ):
        _ensure_ready()
        return await _call(
            service.list_entities, entity_type=type, q=q, status=status, limit=limit, offset=offset
        )

    @router.get("/entities/{uuid}")
    async def get_entity(uuid: str, identity: ApiIdentity = Depends(require_permission(PERM_READ))):
        _ensure_ready()
        entity = await _call(service.get_entity, uuid)
        if entity is None:
            raise HTTPException(status_code=404, detail=f"Unknown entity: {uuid}")
        return {"entity": entity}

    @router.patch("/entities/{uuid}")
    async def update_entity(
        uuid: str,
        payload: EntityUpdatePayload,
        identity: ApiIdentity = Depends(require_permission(PERM_WRITE)),
    ):
        _ensure_ready()
        entity = await _call(
            service.update_entity, uuid,
            display_name=payload.display_name, attributes=payload.attributes,
            replace_attributes=payload.replace_attributes,
        )
        if entity is None:
            raise HTTPException(status_code=404, detail=f"Unknown entity: {uuid}")
        return {"entity": entity}

    @router.post("/entities/{uuid}/retire")
    async def retire_entity(
        uuid: str, identity: ApiIdentity = Depends(require_permission(PERM_WRITE))
    ):
        _ensure_ready()
        entity = await _call(service.retire, uuid, source=identity.username)
        if entity is None:
            raise HTTPException(status_code=404, detail=f"Unknown entity: {uuid}")
        return {"entity": entity}

    @router.post("/entities/{uuid}/merge")
    async def merge_entity(
        uuid: str,
        payload: MergePayload,
        identity: ApiIdentity = Depends(require_permission(PERM_WRITE)),
    ):
        _ensure_ready()
        entity = await _call(service.merge, uuid, payload.drop_uuid, source=identity.username)
        return {"entity": entity}

    # ── Identify / register ────────────────────────────────────────────────────
    @router.post("/identify")
    async def identify(
        payload: IdentifyPayload, identity: ApiIdentity = Depends(require_permission(PERM_WRITE))
    ):
        _ensure_ready()
        return await _call(
            service.identify, payload.entity_type, payload.identifiers,
            display_name=payload.display_name, attributes=payload.attributes, source=payload.source,
        )

    @router.post("/identify/batch")
    async def identify_batch(
        payload: IdentifyBatchPayload,
        identity: ApiIdentity = Depends(require_permission(PERM_WRITE)),
    ):
        """Each item is applied independently; failures are reported per item."""
        _ensure_ready()
        results: List[Dict[str, Any]] = []
        for item in payload.items:
            try:
                res = await asyncio.to_thread(
                    service.identify, item.entity_type, item.identifiers,
                    display_name=item.display_name, attributes=item.attributes, source=item.source,
                )
                results.append({"ok": True, **{k: res[k] for k in ("uuid", "created", "conflict")}})
            except (RegistryError, NotFound) as exc:
                results.append({"ok": False, "error": str(exc), "identifiers": item.identifiers})
        return {
            "results": results,
            "created": sum(1 for r in results if r.get("created")),
            "conflicts": sum(1 for r in results if r.get("conflict")),
            "failed": sum(1 for r in results if not r["ok"]),
        }

    @router.post("/register")
    async def register(
        payload: RegisterPayload, identity: ApiIdentity = Depends(require_permission(PERM_WRITE))
    ):
        _ensure_ready()
        entity, created = await _call(
            service.register, payload.uuid, payload.entity_type, payload.display_name,
            identifiers=payload.identifiers, attributes=payload.attributes, source=payload.source,
        )
        return {"entity": entity, "created": created}

    @router.get("/lookup")
    async def lookup(
        scheme: str, value: str, identity: ApiIdentity = Depends(require_permission(PERM_READ))
    ):
        _ensure_ready()
        entity = await _call(service.lookup, scheme, value)
        if entity is None:
            raise HTTPException(status_code=404, detail=f"No entity for {scheme}={value}")
        return {"entity": entity}

    # ── Relations ──────────────────────────────────────────────────────────────
    @router.post("/entities/{uuid}/relations")
    async def add_relation(
        uuid: str,
        payload: RelationPayload,
        identity: ApiIdentity = Depends(require_permission(PERM_WRITE)),
    ):
        _ensure_ready()
        rel = await _call(
            service.relate, uuid, payload.to_uuid, payload.relation, source=identity.username
        )
        return {"relation": rel}

    @router.delete("/entities/{uuid}/relations/{relation}/{to_uuid}")
    async def remove_relation(
        uuid: str,
        relation: str,
        to_uuid: str,
        identity: ApiIdentity = Depends(require_permission(PERM_WRITE)),
    ):
        _ensure_ready()
        if not await _call(service.unrelate, uuid, to_uuid, relation):
            raise HTTPException(status_code=404, detail="Unknown relation")
        return {"ok": True}

    # ── Conflicts ──────────────────────────────────────────────────────────────
    @router.get("/conflicts")
    async def list_conflicts(
        status: str = "open", identity: ApiIdentity = Depends(require_permission(PERM_READ))
    ):
        _ensure_ready()
        return {"conflicts": await _call(service.list_conflicts, status=status)}

    @router.post("/conflicts/{conflict_id}/dismiss")
    async def dismiss_conflict(
        conflict_id: int,
        payload: DismissPayload,
        identity: ApiIdentity = Depends(require_permission(PERM_WRITE)),
    ):
        _ensure_ready()
        conflict = await _call(service.dismiss_conflict, conflict_id, reason=payload.reason)
        if conflict is None:
            raise HTTPException(status_code=404, detail=f"Unknown conflict: {conflict_id}")
        return {"conflict": conflict}

    # ── Catalog / stats ────────────────────────────────────────────────────────
    @router.get("/catalog")
    async def get_catalog(identity: ApiIdentity = Depends(require_permission(PERM_READ))):
        return await asyncio.to_thread(lambda: service.catalog.catalog.to_payload())

    @router.post("/catalog/reload")
    async def reload_catalog(identity: ApiIdentity = Depends(require_permission(PERM_WRITE))):
        def _run() -> dict:
            service.reload_catalog()
            return service.catalog.catalog.to_payload()

        try:
            return await asyncio.to_thread(_run)
        except Exception as exc:  # invalid YAML must not take the plugin down
            raise HTTPException(status_code=400, detail=f"Catalog reload failed: {exc}") from exc

    @router.get("/stats")
    async def stats(identity: ApiIdentity = Depends(require_permission(PERM_READ))):
        _ensure_ready()
        return await _call(service.get_stats)

    return router
