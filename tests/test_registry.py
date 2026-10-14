"""Entity Registry service tests — real service against in-memory SQLite."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


class _FakeCtx:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def emit(self, topic, payload):
        self.events.append((topic, payload))

    def topics(self) -> list[str]:
        return [t for t, _ in self.events]


@pytest.fixture()
def svc(monkeypatch):
    from app.logic.service import EntityRegistryService
    from app.model import database

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    # Enforce FKs like MariaDB does, so flush-order bugs fail here too.
    event.listen(engine, "connect", lambda conn, _rec: conn.execute("PRAGMA foreign_keys=ON"))
    monkeypatch.setattr(
        database, "db_instance",
        SimpleNamespace(engine=engine, SessionLocal=sessionmaker(bind=engine), is_connected=True),
    )
    service = EntityRegistryService()
    service.set_context(_FakeCtx())
    service.catalog.load()
    service.on_db_connected()
    return service


SRC = "iac-orchestrator"


def _host(svc, key="docker-atlas", vm="hydra/8003", **extra):
    return svc.identify(
        "host", {"iac.host": key, "pve.vm": vm}, display_name=key, source=SRC, **extra
    )


# ── identify ───────────────────────────────────────────────────────────────────

def test_identify_creates_then_recognises(svc):
    first = _host(svc)
    assert first["created"] is True and first["conflict"] is False
    assert len(first["uuid"]) == 36
    assert {i["scheme"] for i in first["entity"]["identifiers"]} == {"iac.host", "pve.vm"}

    again = _host(svc)
    assert again["created"] is False
    assert again["uuid"] == first["uuid"]
    assert svc.list_entities()["total"] == 1
    assert svc._ctx.topics() == ["entity_registry:entity_created"]  # no-op update emits nothing


def test_rename_keeps_uuid_via_stronger_identifier(svc):
    """Host key changes, Proxmox vmid stays → same UUID, new alias learned."""
    first = _host(svc, key="docker-atlas")
    renamed = _host(svc, key="docker-atlas-new")
    assert renamed["created"] is False
    assert renamed["uuid"] == first["uuid"]
    assert renamed["entity"]["display_name"] == "docker-atlas-new"
    values = {(i["scheme"], i["value"]) for i in renamed["entity"]["identifiers"]}
    assert ("iac.host", "docker-atlas") in values and ("iac.host", "docker-atlas-new") in values
    # The old key still resolves.
    assert svc.lookup("iac.host", "docker-atlas")["uuid"] == first["uuid"]
    topic, payload = svc._ctx.events[-1]
    assert topic == "entity_registry:entity_updated"
    assert "identifier:iac.host" in payload["changes"] and "display_name" in payload["changes"]


def test_attributes_merge_and_sources_accumulate(svc):
    _host(svc, attributes={"ip": "10.1.130.61", "site": "onprem"})
    res = svc.identify("host", {"iac.host": "docker-atlas"}, source="monitoring",
                       attributes={"ip": "10.1.130.62"})
    assert res["entity"]["attributes"] == {"ip": "10.1.130.62", "site": "onprem"}
    assert res["entity"]["sources"] == [SRC, "monitoring"]
    assert res["entity"]["owner_source"] == SRC


def test_conflict_is_filed_not_merged(svc):
    a = svc.identify("host", {"iac.host": "alpha"}, source=SRC)
    b = svc.identify("host", {"pve.vm": "hydra/100"}, source=SRC)
    # One observation claims both → ambiguous.
    res = svc.identify("host", {"iac.host": "alpha", "pve.vm": "hydra/100"}, source=SRC)
    assert res["conflict"] is True and res["created"] is False
    assert res["uuid"] == b["uuid"]  # pve.vm (priority 10) beats iac.host (20)
    assert svc.list_entities()["total"] == 2  # nothing merged
    conflicts = svc.list_conflicts()
    assert len(conflicts) == 1
    assert {c["uuid"] for c in conflicts[0]["candidates"]} == {a["uuid"], b["uuid"]}
    assert svc._ctx.topics()[-1] == "entity_registry:conflict"

    # Same conflict again → not filed twice.
    svc.identify("host", {"iac.host": "alpha", "pve.vm": "hydra/100"}, source=SRC)
    assert len(svc.list_conflicts()) == 1


def test_identify_validation(svc):
    from app.logic.service import RegistryError

    with pytest.raises(RegistryError, match="entity_type"):
        svc.identify("spaceship", {"iac.host": "x"}, source=SRC)
    with pytest.raises(RegistryError, match="scheme"):
        svc.identify("host", {"ip": "10.0.0.1"}, source=SRC)  # ip is an attribute, not a key
    with pytest.raises(RegistryError, match="identifier"):
        svc.identify("host", {"iac.host": "  "}, source=SRC)


# ── merge ──────────────────────────────────────────────────────────────────────

def test_merge_repoints_everything_and_leaves_tombstone(svc):
    a = svc.identify("host", {"iac.host": "alpha"}, source=SRC, attributes={"ip": "1"})
    b = svc.identify("host", {"pve.vm": "hydra/100"}, source="other", attributes={"ip": "2", "x": 1})
    hv = svc.identify("hypervisor", {"iac.host": "hydra"}, source=SRC)
    svc.relate(b["uuid"], hv["uuid"], "runs_on")
    svc.relate(a["uuid"], hv["uuid"], "runs_on")  # duplicate after merge → dropped
    svc.identify("host", {"iac.host": "alpha", "pve.vm": "hydra/100"}, source=SRC)  # conflict
    assert len(svc.list_conflicts()) == 1

    kept = svc.merge(a["uuid"], b["uuid"], source="admin")
    assert kept["uuid"] == a["uuid"]
    assert {i["value"] for i in kept["identifiers"]} == {"alpha", "hydra/100"}
    assert kept["attributes"] == {"ip": "1", "x": 1}  # keep wins, missing filled from drop
    assert kept["sources"] == [SRC, "other"]
    assert [r["other_uuid"] for r in kept["relations"]] == [hv["uuid"]]  # one runs_on, not two

    # Old uuid resolves to the survivor; tombstone visible when not following.
    assert svc.get_entity(b["uuid"])["uuid"] == a["uuid"]
    raw = svc.get_entity(b["uuid"], follow_merge=False)
    assert raw["status"] == "merged" and raw["merged_into"] == a["uuid"]
    assert svc.list_conflicts() == [] and svc.list_conflicts(status="resolved")
    # Identify via the dropped entity's key lands on the survivor.
    assert svc.identify("host", {"pve.vm": "hydra/100"}, source=SRC)["uuid"] == a["uuid"]
    assert svc._ctx.topics()[-1] == "entity_registry:entity_merged"


def test_merge_rejects_bad_input(svc):
    from app.logic.service import NotFound, RegistryError

    a = svc.identify("host", {"iac.host": "alpha"}, source=SRC)
    with pytest.raises(RegistryError):
        svc.merge(a["uuid"], a["uuid"])
    with pytest.raises(NotFound):
        svc.merge(a["uuid"], "00000000-0000-0000-0000-000000000000")


# ── register (bring your own uuid) ─────────────────────────────────────────────

def test_register_is_idempotent_and_guards_identifiers(svc):
    from app.logic.service import RegistryError

    uid = "11111111-2222-3333-4444-555555555555"
    rec, created = svc.register(uid, "itsm_record", "INC0000001",
                                identifiers={"itsm.number": "INC0000001"}, source="itsm")
    assert created and rec["uuid"] == uid
    rec2, created2 = svc.register(uid, "itsm_record", "INC0000001 — renamed", source="itsm")
    assert not created2 and rec2["display_name"] == "INC0000001 — renamed"
    assert svc.lookup("itsm.number", "INC0000001")["uuid"] == uid

    other = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    with pytest.raises(RegistryError, match="already owned"):
        svc.register(other, "itsm_record", "dup", identifiers={"itsm.number": "INC0000001"},
                     source="itsm")
    with pytest.raises(RegistryError, match="36-char"):
        svc.register("short", "itsm_record", "x", source="itsm")


# ── relations / retire ─────────────────────────────────────────────────────────

def test_relations_roundtrip(svc):
    from app.logic.service import RegistryError

    h = svc.identify("host", {"iac.host": "alpha"}, source=SRC)
    hv = svc.identify("hypervisor", {"iac.host": "hydra"}, source=SRC)
    rel = svc.relate(h["uuid"], hv["uuid"], "runs_on", source=SRC)
    assert rel["relation"] == "runs_on"
    assert svc.relate(h["uuid"], hv["uuid"], "runs_on")["id"] == rel["id"]  # idempotent
    seen_from_hv = svc.get_entity(hv["uuid"])["relations"]
    assert seen_from_hv[0]["direction"] == "in" and seen_from_hv[0]["other_uuid"] == h["uuid"]
    with pytest.raises(RegistryError):
        svc.relate(h["uuid"], hv["uuid"], "teleports_to")
    with pytest.raises(RegistryError):
        svc.relate(h["uuid"], h["uuid"], "runs_on")
    assert svc.unrelate(h["uuid"], hv["uuid"], "runs_on") is True
    assert svc.unrelate(h["uuid"], hv["uuid"], "runs_on") is False


def test_retire_and_resurrect(svc):
    h = svc.identify("host", {"iac.host": "alpha"}, source=SRC)
    assert svc.retire(h["uuid"], source="admin")["status"] == "retired"
    assert svc.list_entities()["total"] == 0
    assert svc.list_entities(status="retired")["total"] == 1
    # Seen again by a source → active again.
    assert svc.identify("host", {"iac.host": "alpha"}, source=SRC)["entity"]["status"] == "active"


def test_list_search_and_stats(svc):
    _host(svc, key="docker-atlas", vm="hydra/8003")
    _host(svc, key="docker-gpu", vm="hydra/8010")
    svc.identify("hypervisor", {"iac.host": "hydra"}, source=SRC)
    assert svc.list_entities(q="gpu")["total"] == 1
    assert svc.list_entities(q="8003")["total"] == 1  # identifier values are searchable
    assert svc.list_entities(entity_type="hypervisor")["total"] == 1
    stats = svc.get_stats()
    assert stats["by_type"] == {"host": 2, "hypervisor": 1}
    assert stats["identifiers"] == 5 and stats["open_conflicts"] == 0


def test_catalog_rejects_duplicates(tmp_path):
    from app.logic.catalog import CatalogLoader

    (tmp_path / "entity_types.yml").write_text(
        "entity_types: [{id: a, label: A}, {id: a, label: B}]\n"
        "identifier_schemes: [{id: x, label: X}]\n"
    )
    with pytest.raises(Exception, match="duplicate"):
        CatalogLoader(tmp_path).load()


# ── resolve (read-only, for CI links) ──────────────────────────────────────────

def test_resolve_by_uuid_identifier_scheme_and_name(svc):
    uuid = _host(svc, key="docker-hydra", vm="hydra/8003")["uuid"]
    assert svc.resolve(uuid)["match"] == "uuid"
    assert svc.resolve(uuid.upper())["entity"]["uuid"] == uuid  # case-insensitive
    by_value = svc.resolve("docker-hydra")
    assert by_value["match"] == "identifier" and by_value["entity"]["uuid"] == uuid
    by_scheme = svc.resolve("pve.vm:hydra/8003")  # scheme prefix, value keeps its own ':'-free form
    assert by_scheme["match"] == "identifier" and by_scheme["entity"]["uuid"] == uuid
    svc.update_entity(uuid, display_name="Hydra Docker Host")
    by_name = svc.resolve("hydra docker host")
    assert by_name["match"] == "display_name" and by_name["entity"]["uuid"] == uuid
    assert svc.resolve("docker-hydra", entity_type="hypervisor")["entity"] is None


def test_resolve_unknown_lists_candidates_and_never_creates(svc):
    _host(svc, key="docker-hydra", vm="hydra/8003")
    _host(svc, key="docker-atlas", vm="hydra/8004")
    total_before = svc.get_stats()["total"]
    res = svc.resolve("docker-")
    assert res["entity"] is None and res["match"] is None
    assert {c["display_name"] for c in res["candidates"]} == {"docker-hydra", "docker-atlas"}
    assert svc.resolve("00000000-0000-0000-0000-000000000000") == {"entity": None, "match": None, "candidates": []}
    assert svc.get_stats()["total"] == total_before  # resolve never creates
    with pytest.raises(Exception):
        svc.resolve("   ")


def test_resolve_ambiguous_value_returns_candidates_not_a_guess(svc):
    a = svc.identify("host", {"iac.host": "shared"}, display_name="A", source=SRC)["uuid"]
    b = svc.identify("container", {"docker.container": "shared"}, display_name="B", source=SRC)["uuid"]
    res = svc.resolve("shared")
    assert res["entity"] is None and res["match"] == "ambiguous"
    assert {c["uuid"] for c in res["candidates"]} == {a, b}


def test_resolve_follows_merge_tombstone(svc):
    keep = _host(svc, key="docker-hydra", vm="hydra/8003")["uuid"]
    drop = svc.identify("host", {"fqdn": "docker-hydra.int.example"}, display_name="dup", source=SRC)["uuid"]
    svc.merge(keep, drop)
    assert svc.resolve(drop)["entity"]["uuid"] == keep
    assert svc.resolve("docker-hydra.int.example")["entity"]["uuid"] == keep
