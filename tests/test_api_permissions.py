"""ER-01 — irreversible registry operations need ``registry:admin``; an
ordinary writer (``api:write`` is a baseline grant of every INT_USER) cannot
merge/retire/register or pose as another ingest source."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import PERM_ADMIN, PERM_READ, PERM_WRITE, build_plugin_router
from core.api import ApiIdentity


def _identity(name: str, *perms: str) -> ApiIdentity:
    return ApiIdentity(method="session", username=name, permissions=frozenset(perms))


USER = _identity("bob", PERM_READ, PERM_WRITE)
ADMIN = _identity("root", PERM_READ, PERM_WRITE, PERM_ADMIN)


@pytest.fixture()
def client(svc, monkeypatch):
    """The real router; only authentication is replaced (who is calling)."""
    from core.api import security

    current = {"identity": USER}
    monkeypatch.setattr(security, "require_api_auth", lambda request: current["identity"])
    app = FastAPI()
    app.include_router(build_plugin_router(svc))
    http = TestClient(app)
    http.as_ = lambda identity: current.__setitem__("identity", identity) or http  # type: ignore[attr-defined]
    return http


def _host(svc, name: str) -> str:
    return svc.identify("host", {"iac.host": name}, source="iac-orchestrator")["uuid"]


def test_user_cannot_merge_retire_register_dismiss_or_reload(client, svc):
    a, b = _host(svc, "a"), _host(svc, "b")
    calls = [
        ("post", f"/entities/{a}/merge", {"drop_uuid": b}),
        ("post", f"/entities/{a}/retire", None),
        ("post", "/register", {"uuid": "0" * 36, "entity_type": "host", "display_name": "x",
                               "source": "bob"}),
        ("post", "/conflicts/1/dismiss", {}),
        ("post", "/catalog/reload", None),
    ]
    for method, path, body in calls:
        res = getattr(client, method)(path, json=body)
        assert res.status_code == 403, (path, res.text)
        assert "registry:admin" in res.json()["detail"]
    assert svc.get_entity(b, follow_merge=False)["status"] == "active"
    assert svc.get_entity(a)["status"] == "active"


def test_admin_can_merge_and_retire(client, svc):
    a, b = _host(svc, "a"), _host(svc, "b")
    client.as_(ADMIN)
    assert client.post(f"/entities/{a}/merge", json={"drop_uuid": b}).status_code == 200
    assert svc.get_entity(b, follow_merge=False)["merged_into"] == a
    assert client.post(f"/entities/{a}/retire").json()["entity"]["status"] == "retired"
    res = client.post("/register", json={"uuid": "1" * 36, "entity_type": "host",
                                         "display_name": "byo", "source": "itsm"})
    assert res.status_code == 200 and res.json()["created"] is True


def test_identify_source_is_derived_for_ordinary_writers(client, svc):
    body = {"entity_type": "host", "identifiers": {"iac.host": "new-1"}}
    res = client.post("/identify", json=body)
    assert res.status_code == 200, res.text
    assert res.json()["entity"]["owner_source"] == "user:bob"
    # Naming yourself is fine, posing as another system is not.
    assert client.post("/identify", json={**body, "source": "user:bob"}).status_code == 200
    spoof = client.post("/identify", json={
        "entity_type": "host", "identifiers": {"iac.host": "new-2"}, "source": "iac-orchestrator"})
    assert spoof.status_code == 403
    assert svc.lookup("iac.host", "new-2") is None


def test_identify_batch_rejects_a_spoofed_source_before_writing(client, svc):
    items = [
        {"entity_type": "host", "identifiers": {"iac.host": "b-1"}},
        {"entity_type": "host", "identifiers": {"iac.host": "b-2"}, "source": "monitoring"},
    ]
    assert client.post("/identify/batch", json={"items": items}).status_code == 403
    assert svc.lookup("iac.host", "b-1") is None
    ok = client.post("/identify/batch", json={"items": items[:1]})
    assert ok.status_code == 200 and ok.json()["created"] == 1
    assert svc.lookup("iac.host", "b-1")["sources"] == ["user:bob"]


def test_admin_may_choose_the_source(client, svc):
    client.as_(ADMIN)
    res = client.post("/identify", json={
        "entity_type": "host", "identifiers": {"iac.host": "c-1"}, "source": "connector-x"})
    assert res.status_code == 200 and res.json()["entity"]["owner_source"] == "connector-x"


def test_access_reports_capabilities_and_resolve_stays_read_only(client, svc):
    _host(svc, "a")
    assert client.get("/access").json() == {"write": True, "admin": False, "source": "user:bob"}
    assert client.as_(ADMIN).get("/access").json()["admin"] is True
    client.as_(_identity("eve", PERM_READ))
    assert client.get("/resolve", params={"ref": "a"}).json()["match"] == "identifier"
    assert client.post("/identify", json={"entity_type": "host",
                                          "identifiers": {"iac.host": "z"}}).status_code == 403


def test_manifest_maps_the_admin_permission_to_int_admin_only(entrypoint):
    m = entrypoint.manifest
    assert [p.id for p in m.custom_permissions] == ["registry:admin"]
    holders = [r for r in m.roles if any(p.endswith("registry:admin") for p in r.permissions)]
    assert [r.id for r in holders] == ["admin"] and holders[0].auto_map_groups == ["INT_ADMIN"]


def test_bus_identify_keeps_the_caller_named_source(bus, svc):
    import asyncio

    asyncio.run(bus.handlers["entity_registry:identify"]({
        "entity_type": "host", "identifiers": {"iac.host": "bus-1"},
        "source": "iac-orchestrator", "request_id": "r1"}))
    topic, reply = bus.events[-1]
    assert topic == "entity_registry:identified"
    assert reply["ok"] and reply["request_id"] == "r1" and set(reply) == {
        "request_id", "ok", "uuid", "created", "conflict"}
    assert svc.lookup("iac.host", "bus-1")["owner_source"] == "iac-orchestrator"


def test_merging_an_already_merged_entity_is_409(client, svc):
    """ER-05: the loser of a merge race gets 409, not a generic 400."""
    a, b = _host(svc, "a"), _host(svc, "b")
    client.as_(ADMIN)
    assert client.post(f"/entities/{a}/merge", json={"drop_uuid": b}).status_code == 200
    assert client.post(f"/entities/{b}/merge", json={"drop_uuid": a}).status_code == 409
