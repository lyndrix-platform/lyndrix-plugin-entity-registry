"""Maintenance read-model (0.3.0): windows, overrides, evaluation, routes' logic,
bus handlers and the ``iac.service`` scheme — against in-memory SQLite."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from app.logic import maintenance as mwx

SRC = "iac-orchestrator"
MON = "monitoring"


def _iso(dt) -> str:
    return mwx.iso(dt)


def _host(svc, key="docker-hydra", vm="hydra/8003"):
    return svc.identify(
        "host", {"iac.host": key, "pve.vm": vm}, display_name=key, source=SRC
    )["uuid"]


def _once(start, end, note=None) -> dict:
    w = {"type": "once", "start": _iso(start), "end": _iso(end)}
    if note:
        w["note"] = note
    return w


def _item(ci_id, *windows, **extra) -> dict:
    return {
        "ci_id": ci_id,
        "kind": "host",
        "name": "x",
        "windows": list(windows),
        "inherited_from": None,
        **extra,
    }


# ── apply_windows ──────────────────────────────────────────────────────────────


def test_apply_windows_replaces_per_source_and_keeps_other_sources(svc):
    now = mwx.utcnow()
    a, b = _host(svc, "a", "hydra/1"), _host(svc, "b", "hydra/2")
    active = _once(now - timedelta(hours=1), now + timedelta(hours=1))
    later = _once(now + timedelta(days=1), now + timedelta(days=1, hours=2))

    res = svc.apply_windows(SRC, [_item(a, active), _item(b, later)], replaces=True)
    assert res == {
        "source": SRC,
        "count": 2,
        "skipped_unknown": 0,
        "skipped_invalid": 0,
    }
    svc.apply_windows(MON, [_item(a, later)], replaces=True)

    # Orchestrator re-publishes only b → its a-row is gone, monitoring's a-row stays.
    svc.apply_windows(SRC, [_item(b, later)], replaces=True)
    m = svc.get_entity(a)["maintenance"]
    assert [w["source"] for w in m["windows"]] == [MON]
    assert m["active"] is False and m["next_start"] == later["start"]
    mb = svc.get_entity(b)["maintenance"]
    assert [w["source"] for w in mb["windows"]] == [SRC]

    # An empty set withdraws the source completely.
    svc.apply_windows(SRC, [], replaces=True)
    assert svc.get_entity(b)["maintenance"]["windows"] == []
    assert svc.get_entity(b)["maintenance"] == mwx.empty_state()


def test_apply_windows_skips_unknown_and_invalid_follows_tombstones(svc):
    now = mwx.utcnow()
    keep = _host(svc, "keep", "hydra/1")
    drop = svc.identify(
        "host", {"fqdn": "dup.example"}, display_name="dup", source=SRC
    )["uuid"]
    svc.merge(keep, drop)
    win = _once(
        now - timedelta(minutes=5), now + timedelta(minutes=55), note="Storage move"
    )

    res = svc.apply_windows(
        SRC,
        [
            _item(drop, win),  # tombstone → keep
            _item("00000000-0000-0000-0000-000000000000", win),  # unknown → skipped
            "not-a-dict",  # invalid → skipped
            {"ci_id": keep, "windows": "nope"},  # invalid → skipped
            {
                "ci_id": keep,
                "windows": [
                    {"type": "weekly", "days": [], "start": "02:00", "end": "04:00"}
                ],
            },
        ],
    )
    assert (
        res["count"] == 1
        and res["skipped_unknown"] == 1
        and res["skipped_invalid"] == 3
    )
    m = svc.get_entity(keep)["maintenance"]
    assert (
        m["active"] is True and m["source"] == "window" and m["note"] == "Storage move"
    )
    assert m["until"] == win["end"]
    # Same payload again → still exactly one row (replace, not append).
    svc.apply_windows(SRC, [_item(drop, win)])
    assert len(svc.get_entity(keep)["maintenance"]["windows"]) == 1


def test_apply_windows_without_replaces_upserts(svc):
    now = mwx.utcnow()
    a, b = _host(svc, "a", "hydra/1"), _host(svc, "b", "hydra/2")
    w1 = _once(now + timedelta(hours=1), now + timedelta(hours=2))
    w2 = _once(now + timedelta(hours=3), now + timedelta(hours=4))
    svc.apply_windows(SRC, [_item(a, w1)])
    svc.apply_windows(SRC, [_item(b, w1)], replaces=False)  # a untouched
    svc.apply_windows(SRC, [_item(b, w2)], replaces=False)  # b updated in place
    assert svc.get_entity(a)["maintenance"]["windows"][0]["windows"] == [w1]
    mb = svc.get_entity(b)["maintenance"]
    assert len(mb["windows"]) == 1 and mb["windows"][0]["windows"] == [w2]


def test_apply_windows_emits_one_collective_event_and_keeps_rule_fields(svc):
    now = mwx.utcnow()
    a = _host(svc, "a", "hydra/1")
    svc._ctx.events.clear()
    rule = {
        "enabled": True,
        "offline_minutes": 10,
        "severity": "high",
        "auto_resolve": True,
    }
    svc.apply_windows(
        SRC,
        [
            _item(
                a,
                _once(now + timedelta(hours=1), now + timedelta(hours=2)),
                inherited_from="stage_default",
                deploy_policy="window",
                incidents=rule,
            ),
            _item(_host(svc, "b", "hydra/2")),
        ],
    )
    topics = [t for t, _ in svc._ctx.events]
    assert topics.count("entity_registry:maintenance_changed") == 1
    assert "entity_registry:entity_updated" not in topics  # no per-entity noise
    _, payload = [
        e for e in svc._ctx.events if e[0] == "entity_registry:maintenance_changed"
    ][0]
    assert (
        payload["source"] == SRC
        and payload["kind"] == "window"
        and payload["count"] == 2
    )
    m = svc.get_entity(a)["maintenance"]
    assert m["incidents"] == rule and m["deploy_policy"] == "window"
    assert m["windows"][0]["inherited_from"] == "stage_default"


def test_apply_windows_validation(svc):
    from app.logic.service import RegistryError

    with pytest.raises(RegistryError, match="source"):
        svc.apply_windows("", [])
    with pytest.raises(RegistryError, match="list"):
        svc.apply_windows(SRC, {"ci_id": "x"})  # type: ignore[arg-type]


# ── overrides ──────────────────────────────────────────────────────────────────


def _started(
    ov_id, ci_ids, now, *, minutes=240, source=SRC, reason="deploy aac-grafana"
):
    return {
        "override_id": ov_id,
        "ci_ids": ci_ids,
        "until": _iso(now + timedelta(minutes=minutes)),
        "reason": reason,
        "source": source,
        "started_at": _iso(now),
        "linger_minutes": 15,
    }


def test_override_start_end_linger_and_expiry(svc):
    now = mwx.utcnow()
    a, b = _host(svc, "a", "hydra/1"), _host(svc, "b", "hydra/2")
    res = svc.start_override(_started("job-1", [a, b, "unknown-id"], now))
    assert res["count"] == 2 and res["skipped_unknown"] == 1

    m = svc.get_entity(a)["maintenance"]
    assert (
        m["active"] is True
        and m["source"] == "override"
        and m["note"] == "deploy aac-grafana"
    )
    assert m["until"] == _iso(now + timedelta(minutes=240))
    assert m["overrides"][0]["id"] == "job-1" and m["overrides"][0]["source"] == SRC

    # Re-delivery of "started" is an upsert, not a second row.
    svc.start_override(_started("job-1", [a, b], now))
    assert len(svc.get_entity(a)["maintenance"]["overrides"]) == 1

    # Ended 5 min ago, linger 15 → still active for 10 more minutes, then gone.
    ended = now - timedelta(minutes=5)
    assert (
        svc.end_override(
            {
                "override_id": "job-1",
                "source": SRC,
                "ended_at": _iso(ended),
                "linger_minutes": 15,
            }
        )["count"]
        == 2
    )
    from app.model.database import get_session

    with get_session() as s:
        st = svc.maintenance_for(s, [a, b], now)
        assert st[a]["active"] is True and st[a]["until"] == _iso(
            ended + timedelta(minutes=15)
        )
        assert st[b]["active"] is True
        later = svc.maintenance_for(s, [a], now + timedelta(minutes=11))[a]
        assert later["active"] is False and later["overrides"] == []
    # A second "ended" keeps the first end time (idempotent).
    svc.end_override({"override_id": "job-1", "source": SRC, "ended_at": _iso(now)})
    with get_session() as s:
        assert svc.maintenance_for(s, [a], now)[a]["until"] == _iso(
            ended + timedelta(minutes=15)
        )


def test_end_override_respects_source_and_defaults(svc):
    now = mwx.utcnow()
    a = _host(svc, "a", "hydra/1")
    svc.start_override(_started("job-2", [a], now, source=SRC))
    # Another emitter cannot end it.
    assert svc.end_override({"override_id": "job-2", "source": MON})["count"] == 0
    assert svc.get_entity(a)["maintenance"]["active"] is True
    # Without source → matched by id; ended_at defaults to now.
    assert svc.end_override({"override_id": "job-2"})["count"] == 1
    from app.model.database import get_session

    with get_session() as s:
        st = svc.maintenance_for(s, [a], now + timedelta(minutes=16))[a]
    assert st["active"] is False


def test_override_payload_validation(svc):
    from app.logic.service import RegistryError

    with pytest.raises(RegistryError, match="override_id"):
        svc.start_override({"ci_ids": [], "until": "2026-01-01T00:00:00Z"})
    with pytest.raises(RegistryError, match="until"):
        svc.start_override({"override_id": "x", "ci_ids": []})
    with pytest.raises(RegistryError):
        svc.start_override({"override_id": "x", "ci_ids": [], "until": "not-a-date"})
    with pytest.raises(RegistryError, match="override_id"):
        svc.end_override({})


def test_purge_expired_removes_only_long_dead_overrides(svc, monkeypatch):
    now = mwx.utcnow()
    a = _host(svc, "a", "hydra/1")
    old = now - timedelta(days=3)
    # start/end_override piggyback the sweep; hold it back so the rows exist.
    real_purge, hold = svc._purge_quietly, {"on": True}
    monkeypatch.setattr(
        svc, "_purge_quietly", lambda: None if hold["on"] else real_purge()
    )
    svc.start_override(_started("old-open", [a], old, minutes=60))  # until 3d ago
    svc.start_override(_started("old-ended", [a], old, minutes=600))  # until ~2.6d ago
    svc.end_override(
        {
            "override_id": "old-ended",
            "source": SRC,
            "ended_at": _iso(old + timedelta(minutes=30)),
        }
    )
    svc.start_override(
        _started("recent-ended", [a], now - timedelta(hours=30), minutes=600)
    )
    svc.end_override(
        {
            "override_id": "recent-ended",
            "source": SRC,
            "ended_at": _iso(now - timedelta(hours=29)),
        }
    )
    svc.start_override(_started("live", [a], now))

    assert svc.purge_expired(now) == 2
    hold["on"] = False
    # The piggyback does the same on the next override event.
    svc.start_override(_started("old-open-2", [a], old, minutes=60))
    svc.start_override(_started("live-2", [a], now))
    assert svc.purge_expired(now) == 0  # already swept by start_override
    from app.model.database import get_session
    from sqlalchemy import select
    from app.model.models import MaintenanceEntry

    with get_session() as s:
        left = {r.override_id for r in s.execute(select(MaintenanceEntry)).scalars()}
    assert left == {"recent-ended", "live", "live-2"}


# ── evaluation ─────────────────────────────────────────────────────────────────


def test_effective_state_window_and_override_together(svc):
    now = mwx.utcnow()
    a = _host(svc, "a", "hydra/1")
    svc.apply_windows(
        SRC, [_item(a, _once(now - timedelta(hours=1), now + timedelta(hours=1)))]
    )
    svc.start_override(_started("job-3", [a], now, minutes=180))
    m = svc.get_entity(a)["maintenance"]
    assert m["active"] is True and m["source"] == "override"  # override wins
    assert m["until"] == _iso(now + timedelta(hours=3))  # later of the two ends
    assert len(m["windows"]) == 1 and len(m["overrides"]) == 1


def test_maintenance_for_without_core_library(svc, monkeypatch):
    now = mwx.utcnow()
    a, b, c = (
        _host(svc, "a", "hydra/1"),
        _host(svc, "b", "hydra/2"),
        _host(svc, "c", "hydra/3"),
    )
    svc.apply_windows(
        SRC, [_item(a, _once(now - timedelta(hours=1), now + timedelta(hours=1)))]
    )
    svc.start_override(_started("job-4", [b], now))

    monkeypatch.setattr(mwx, "mw", None)
    from app.model.database import get_session

    with get_session() as s:
        st = svc.maintenance_for(s, [a, b, c], now)
    assert (
        st[a]["active"] is None and len(st[a]["windows"]) == 1
    )  # raw data, no verdict
    assert st[a]["next_start"] is None
    assert (
        st[b]["active"] is True and st[b]["source"] == "override"
    )  # arithmetic needs no library
    assert st[c] == mwx.empty_state()
    # Writes still work without the library (raw windows stored, payloads parsed natively).
    svc.apply_windows(
        MON,
        [
            _item(
                c, {"type": "weekly", "days": ["sun"], "start": "02:00", "end": "04:00"}
            )
        ],
    )
    svc.start_override(
        {
            "override_id": "job-5",
            "ci_ids": [c],
            "until": _iso(now + timedelta(hours=1)),
            "source": MON,
            "started_at": _iso(now),
            "linger_minutes": 0,
        }
    )
    svc.end_override({"override_id": "job-5", "source": MON})
    with get_session() as s:
        st = svc.maintenance_for(s, [c], now + timedelta(minutes=1))[c]
    assert (
        st["active"] is None
        and st["windows"][0]["source"] == MON
        and st["overrides"] == []
    )


# ── read paths: detail, list, batch, host ──────────────────────────────────────


def test_detail_and_list_carry_maintenance_and_filter(svc):
    now = mwx.utcnow()
    a, b = _host(svc, "a", "hydra/1"), _host(svc, "b", "hydra/2")
    _host(svc, "c", "hydra/3")
    svc.apply_windows(
        SRC,
        [
            _item(a, _once(now - timedelta(hours=1), now + timedelta(hours=1))),
            _item(b, _once(now + timedelta(hours=1), now + timedelta(hours=2))),
        ],
    )
    rows = {e["uuid"]: e["maintenance"] for e in svc.list_entities()["entities"]}
    assert rows[a] == {
        "active": True,
        "until": _iso(now + timedelta(hours=1)),
        "source": "window",
    }
    assert rows[b]["active"] is False and set(rows[b]) == {"active", "until", "source"}
    assert svc.get_entity(a)["maintenance"]["windows"]  # full form on the detail

    filtered = svc.list_entities(in_maintenance=True)
    assert [e["uuid"] for e in filtered["entities"]] == [a] and filtered["total"] == 1
    assert svc.list_entities(in_maintenance=False)["total"] == 3
    # resolve() summaries carry it too
    assert svc.resolve("a")["entity"]["maintenance"]["active"] is True


def test_get_maintenance_batch_resolves_tombstones_and_unknown(svc):
    now = mwx.utcnow()
    keep, drop = _host(svc, "keep", "hydra/1"), _host(svc, "drop", "hydra/2")
    svc.merge(keep, drop)
    svc.start_override(_started("job-6", [keep], now))
    res = svc.get_maintenance([drop, keep, "ghost", " ", drop])
    assert set(res["maintenance"]) == {drop, keep, "ghost"}
    assert res["maintenance"]["ghost"] is None
    assert (
        res["maintenance"][drop]["uuid"] == keep
        and res["maintenance"][drop]["active"] is True
    )
    assert res["now"].endswith("Z")
    assert svc.get_maintenance([])["maintenance"] == {}


def test_host_of_via_runs_on(svc):
    from app.logic.service import NotFound

    host = _host(svc, "docker-hydra", "hydra/8003")
    service = svc.identify(
        "service",
        {"iac.service": "docker-hydra/aac-grafana"},
        display_name="aac-grafana @ docker-hydra",
        source=SRC,
    )["uuid"]
    assert svc.host_of(service) is None  # no relation yet
    svc.relate(service, host, "runs_on", source=SRC)
    res = svc.host_of(service)
    assert res["host"]["uuid"] == host and res["relation"]["relation"] == "runs_on"
    assert svc.host_of(host) is None  # a host runs on nothing (yet)
    with pytest.raises(NotFound):
        svc.host_of("00000000-0000-0000-0000-000000000000")
    # Tombstones are followed on the service side.
    dup = svc.identify(
        "service", {"fqdn": "grafana.example"}, display_name="dup", source=SRC
    )["uuid"]
    svc.merge(service, dup)
    assert svc.host_of(dup)["host"]["uuid"] == host


# ── catalog ────────────────────────────────────────────────────────────────────


def test_catalog_has_iac_service_scheme(svc):
    assert "iac.service" in svc.catalog.scheme_ids()
    assert svc.catalog.scheme_priority("iac.service") == 20
    first = svc.identify(
        "service", {"iac.service": "docker-hydra/aac-grafana"}, source=SRC
    )
    again = svc.identify(
        "service", {"iac.service": "docker-hydra/aac-grafana"}, source=SRC
    )
    assert first["created"] and again["uuid"] == first["uuid"]
    assert (
        svc.resolve("iac.service:docker-hydra/aac-grafana")["entity"]["uuid"]
        == first["uuid"]
    )


# ── bus handlers (entrypoint.py) ───────────────────────────────────────────────


def _run(coro):
    return asyncio.run(coro)


def _failed(ctx) -> list[dict]:
    return [p for t, p in ctx.events if t == "entity_registry:ingest_failed"]


def test_manifest_declares_maintenance_topics(entrypoint):
    perms = entrypoint.manifest.permissions
    assert {
        "maintenance:windows_published",
        "maintenance:override_started",
        "maintenance:override_ended",
    } <= set(perms.subscribe)
    assert "entity_registry:maintenance_changed" in perms.emit
    assert entrypoint.manifest.version == "0.3.0"


def test_bus_windows_published_rejects_bad_payloads(bus, svc):
    h = bus.handlers["maintenance:windows_published"]
    _run(h("nope"))
    _run(h({"items": []}))  # no source
    _run(h({"source": SRC, "items": "x"}))  # items not a list
    assert len(_failed(bus)) == 3
    assert _failed(bus)[0]["topic"] == "maintenance:windows_published"
    svc._ready = False
    _run(h({"source": SRC, "items": []}))
    assert _failed(bus)[-1]["error"] == "database not ready"


def test_bus_windows_and_overrides_end_to_end(bus, svc):
    now = mwx.utcnow()
    a = _host(svc, "a", "hydra/1")
    _run(
        bus.handlers["maintenance:windows_published"](
            {
                "source": SRC,
                "published_at": _iso(now),
                "replaces": True,
                "items": [
                    _item(a, _once(now + timedelta(hours=1), now + timedelta(hours=2)))
                ],
            }
        )
    )
    m = svc.get_entity(a)["maintenance"]
    assert m["active"] is False and m["next_start"] == _iso(now + timedelta(hours=1))

    _run(bus.handlers["maintenance:override_started"](_started("job-7", [a], now)))
    assert svc.get_entity(a)["maintenance"]["active"] is True
    _run(
        bus.handlers["maintenance:override_ended"](
            {
                "override_id": "job-7",
                "source": SRC,
                "ended_at": _iso(now - timedelta(minutes=30)),
                "linger_minutes": 15,
            }
        )
    )
    assert svc.get_entity(a)["maintenance"]["active"] is False
    assert not _failed(bus)
    changed = [p for t, p in bus.events if t == "entity_registry:maintenance_changed"]
    assert [c["kind"] for c in changed] == ["window", "override", "override"]


def test_bus_override_handlers_reject_bad_payloads(bus):
    started, ended = (
        bus.handlers["maintenance:override_started"],
        bus.handlers["maintenance:override_ended"],
    )
    _run(started({"override_id": "x"}))  # no ci_ids / until
    _run(started({"override_id": "", "ci_ids": [], "until": "2026-01-01T00:00:00Z"}))
    _run(
        started({"override_id": "x", "ci_ids": [], "until": "garbage"})
    )  # parse error → RegistryError
    _run(ended({"source": SRC}))
    _run(ended([]))
    assert len(_failed(bus)) == 5
