"""ER-06: API timestamps are timezone-aware (``Z``); both forms are accepted on input."""
from __future__ import annotations

from datetime import datetime, timezone

from app.logic import maintenance as mwx
from app.model.models import iso_z


def test_iso_z_marks_naive_utc_and_normalises_offsets():
    assert iso_z(datetime(2026, 11, 14, 10, 0, 5, 123)) == "2026-11-14T10:00:05.000123Z"
    aware = datetime.fromisoformat("2026-11-14T11:00:00+01:00")
    assert iso_z(aware) == "2026-11-14T10:00:00Z"
    assert iso_z(None) is None


def test_entity_output_carries_z(svc):
    uuid = svc.identify("host", {"iac.host": "a"}, source="iac-orchestrator")["uuid"]
    entity = svc.get_entity(uuid)
    for key in ("created_at", "updated_at", "last_seen_at"):
        assert entity[key].endswith("Z"), (key, entity[key])
    assert datetime.fromisoformat(entity["created_at"].replace("Z", "+00:00")).tzinfo is not None
    # created_at comes from Python UTC, not the DB server's time zone.
    delta = datetime.now(timezone.utc) - datetime.fromisoformat(entity["created_at"].replace("Z", "+00:00"))
    assert abs(delta.total_seconds()) < 60


def test_input_accepts_naive_and_aware_timestamps():
    naive = mwx.parse_ts("2026-11-14T10:00:00", field="x")
    zulu = mwx.parse_ts("2026-11-14T10:00:00Z", field="x")
    offset = mwx.parse_ts("2026-11-14T11:00:00+01:00", field="x")
    assert naive == zulu == offset
