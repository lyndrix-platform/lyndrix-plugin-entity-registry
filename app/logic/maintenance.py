"""Maintenance read-model helpers — payload parsing and read-time evaluation.

The registry stores what ``maintenance:windows_published`` and
``maintenance:override_started/ended`` deliver (``MaintenanceEntry`` rows) and
evaluates the effective state **when it reads**, through the core library
``core.api.maintenance`` (API 1.13). The library is optional: on an older
core (``min_core_version`` stays 1.4.0) the rows are stored and served as raw
data with ``active: null`` — a UI then shows "evaluation unavailable" instead
of guessing. Overrides need no library (plain arithmetic), so an active
override is reported as ``active: true`` either way.

Nothing in here touches the database; the service calls these helpers inside
its own sessions.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from core.api import get_logger

try:  # core API >= 1.13 (core >= 1.11)
    from core.api import maintenance as mw
except ImportError:  # pragma: no cover - exercised via monkeypatch in tests
    mw = None  # type: ignore[assignment]

log = get_logger("Plugin:EntityRegistry:Maintenance")

UTC = timezone.utc
DEFAULT_LINGER_MINUTES = 15
MAX_SOURCE_LEN = 64
MAX_OVERRIDE_ID_LEN = 64
MAX_REASON_LEN = 2000


class MaintenancePayloadError(ValueError):
    """A ``maintenance:*`` payload is malformed (raised before any DB work)."""


# ── Time helpers ──────────────────────────────────────────────────────────────


def utcnow() -> datetime:
    """Aware UTC now, whole seconds."""
    return datetime.now(UTC).replace(microsecond=0)


def parse_ts(value: Any, *, field: str) -> datetime:
    """ISO-8601 (``Z`` accepted, naive = UTC) → aware UTC datetime."""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str) and value.strip():
        if mw is not None:
            try:
                return mw.parse_iso(value)
            except ValueError as exc:
                raise MaintenancePayloadError(f"{field}: {exc}") from exc
        text = value.strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError as exc:
            raise MaintenancePayloadError(
                f"{field}: invalid ISO-8601 timestamp {value!r}"
            ) from exc
    else:
        raise MaintenancePayloadError(f"{field}: invalid ISO-8601 timestamp {value!r}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def to_db(dt: datetime | None) -> datetime | None:
    """Aware → naive UTC for a MariaDB ``DateTime`` column."""
    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC)
    return dt.replace(tzinfo=None, microsecond=0)


def from_db(dt: datetime | None) -> datetime | None:
    """Naive UTC column value → aware UTC."""
    if dt is None:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def iso(dt: datetime | None) -> str | None:
    """Aware/naive-UTC datetime → ``…Z`` string (second precision)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _as_int(value: Any, field: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        if isinstance(value, str) and value.strip().lstrip("-").isdigit():
            value = int(value.strip())
        else:
            raise MaintenancePayloadError(f"{field} must be an integer, got {value!r}")
    if value < minimum:
        raise MaintenancePayloadError(f"{field} must be >= {minimum}, got {value}")
    return value


# ── Payload normalisation (windows_published items, override payloads) ───────


def normalize_item(item: Any) -> dict | None:
    """One ``windows_published`` item → ``{ci_id, windows, inherited_from,
    deploy_policy, incidents}`` or ``None`` when it cannot be stored.

    The item's ``kind``/``name``/``host_ci_id`` are informational (the
    entity's own type and relations stay authoritative) and are dropped.
    Unknown keys are ignored. With the core library present the windows are
    validated — a broken item is skipped rather than poisoning every read.
    """
    if not isinstance(item, dict):
        return None
    ci_id = item.get("ci_id")
    if not isinstance(ci_id, str) or not ci_id.strip():
        return None
    windows = item.get("windows")
    if windows is None:
        windows = []
    if not isinstance(windows, list) or not all(isinstance(w, dict) for w in windows):
        return None
    if mw is not None:
        try:
            windows = mw.to_dicts(mw.parse_windows(windows))
        except ValueError as exc:  # ScheduleError
            log.warning(f"maintenance: skipping windows of {ci_id} — {exc}")
            return None
    inherited = item.get("inherited_from")
    policy = item.get("deploy_policy")
    incidents = item.get("incidents")
    return {
        "ci_id": ci_id.strip(),
        "windows": windows,
        "inherited_from": (
            str(inherited)[:32] if isinstance(inherited, str) and inherited else None
        ),
        "deploy_policy": (
            str(policy)[:16] if isinstance(policy, str) and policy else None
        ),
        "incidents": dict(incidents) if isinstance(incidents, dict) else None,
    }


def parse_override_started(payload: Any) -> dict:
    """``maintenance:override_started`` → ``{override_id, ci_ids, until,
    started_at, linger_minutes, reason, source}`` (aware UTC datetimes)."""
    if not isinstance(payload, dict):
        raise MaintenancePayloadError("payload must be an object")
    if mw is not None:
        try:
            o = mw.override_from_payload(payload)
        except ValueError as exc:  # MaintenanceError
            raise MaintenancePayloadError(str(exc)) from exc
        override_id, ci_ids, until, started = (
            o.override_id,
            list(o.ci_ids),
            o.until,
            o.started_at,
        )
        linger, reason, source = o.linger_minutes, o.reason, o.source
    else:
        override_id = payload.get("override_id")
        if not isinstance(override_id, str) or not override_id:
            raise MaintenancePayloadError("override_started: override_id is required")
        raw_ids = payload.get("ci_ids") or []
        if not isinstance(raw_ids, (list, tuple)):
            raise MaintenancePayloadError("override_started: ci_ids must be a list")
        ci_ids = [str(c) for c in raw_ids]
        if payload.get("until") is None:
            raise MaintenancePayloadError("override_started: until is required")
        until = parse_ts(payload["until"], field="until")
        started_raw = payload.get("started_at")
        started = (
            parse_ts(started_raw, field="started_at")
            if started_raw is not None
            else utcnow()
        )
        linger_raw = payload.get("linger_minutes", DEFAULT_LINGER_MINUTES)
        linger = (
            _as_int(linger_raw, "linger_minutes")
            if linger_raw is not None
            else DEFAULT_LINGER_MINUTES
        )
        reason = str(payload.get("reason") or "")
        source = str(payload.get("source") or "")
    if len(override_id) > MAX_OVERRIDE_ID_LEN:
        raise MaintenancePayloadError(
            f"override_id longer than {MAX_OVERRIDE_ID_LEN} chars"
        )
    return {
        "override_id": override_id,
        "ci_ids": [c.strip() for c in ci_ids if isinstance(c, str) and c.strip()],
        "until": until,
        "started_at": started,
        "linger_minutes": linger,
        "reason": reason[:MAX_REASON_LEN],
        "source": source.strip()[:MAX_SOURCE_LEN],
    }


def parse_override_ended(payload: Any) -> dict:
    """``maintenance:override_ended`` → ``{override_id, source, ended_at,
    linger_minutes}`` (``linger_minutes`` None = keep the stored value)."""
    if not isinstance(payload, dict):
        raise MaintenancePayloadError("payload must be an object")
    override_id = payload.get("override_id")
    if not isinstance(override_id, str) or not override_id:
        raise MaintenancePayloadError("override_ended: override_id is required")
    ended_raw = payload.get("ended_at")
    ended = parse_ts(ended_raw, field="ended_at") if ended_raw is not None else utcnow()
    linger_raw = payload.get("linger_minutes")
    linger = _as_int(linger_raw, "linger_minutes") if linger_raw is not None else None
    source = payload.get("source")
    return {
        "override_id": override_id[:MAX_OVERRIDE_ID_LEN],
        "source": (
            str(source).strip()[:MAX_SOURCE_LEN]
            if isinstance(source, str) and source.strip()
            else None
        ),
        "ended_at": ended,
        "linger_minutes": linger,
    }


# ── Read-time evaluation ──────────────────────────────────────────────────────


def override_effective_until(row) -> datetime | None:
    """``ended_at + linger`` once ended, otherwise ``until`` (aware UTC)."""
    ended = from_db(row.ended_at)
    if ended is not None:
        return ended + timedelta(minutes=max(0, row.linger_minutes or 0))
    return from_db(row.until)


def override_is_active(row, now: datetime) -> bool:
    started = from_db(row.started_at)
    end = override_effective_until(row)
    if end is None:
        return False
    return (started is None or started <= now) and now < end


def empty_state() -> dict:
    return {
        "active": False,
        "until": None,
        "source": None,
        "next_start": None,
        "note": None,
        "windows": [],
        "overrides": [],
        "incidents": None,
        "deploy_policy": None,
    }


def compact(state: dict) -> dict:
    """The list-row form: just enough for a badge."""
    return {
        "active": state["active"],
        "until": state["until"],
        "source": state["source"],
    }


def _window_view(row) -> dict:
    return {
        "source": row.source,
        "windows": list(row.windows or []),
        "inherited_from": row.inherited_from,
        "deploy_policy": row.deploy_policy,
        "incidents": dict(row.incidents) if row.incidents else None,
        "updated_at": iso(row.updated_at),
    }


def _override_view(row) -> dict:
    return {
        "id": row.override_id,
        "source": row.source,
        "until": iso(override_effective_until(row)),
        "started_at": iso(row.started_at),
        "ended_at": iso(row.ended_at),
        "linger_minutes": row.linger_minutes,
        "reason": row.reason or "",
    }


def evaluate(window_rows: list, override_rows: list, now: datetime) -> dict:
    """Combine one entity's rows into the read-model state.

    With the core library: ``effective_state`` over all sources' windows and
    the stored overrides (an active override wins, ``until`` is the later end).
    Without it: overrides are still evaluated (arithmetic), windows are passed
    through raw and ``active`` is ``None`` whenever windows exist that cannot
    be evaluated.
    """
    state = empty_state()
    state["windows"] = [_window_view(r) for r in window_rows]
    active_overrides = [r for r in override_rows if override_is_active(r, now)]
    state["overrides"] = [_override_view(r) for r in active_overrides]
    for r in window_rows:  # first source that says something wins (sources are sorted)
        if state["incidents"] is None and r.incidents:
            state["incidents"] = dict(r.incidents)
        if state["deploy_policy"] is None and r.deploy_policy:
            state["deploy_policy"] = r.deploy_policy

    if mw is None:
        if active_overrides:
            strongest = max(active_overrides, key=override_effective_until)
            state.update(
                active=True,
                until=iso(override_effective_until(strongest)),
                source="override",
                note=strongest.reason or None,
            )
        elif any(r.windows for r in window_rows):
            state["active"] = None  # windows exist but cannot be evaluated here
        return state

    schedules: list = []
    for r in window_rows:
        try:
            schedules.extend(mw.parse_windows(r.windows or []))
        except ValueError as exc:  # stored before the library existed, or a newer form
            log.debug(
                f"maintenance: ignoring unreadable windows of {r.entity_uuid}/{r.source} — {exc}"
            )
    overrides = [
        mw.Override(
            override_id=r.override_id,
            ci_ids=(r.entity_uuid,),
            until=from_db(r.until) or now,
            started_at=from_db(r.started_at) or now,
            ended_at=from_db(r.ended_at),
            linger_minutes=(
                r.linger_minutes
                if r.linger_minutes is not None
                else DEFAULT_LINGER_MINUTES
            ),
            reason=r.reason or "",
            source=r.source,
        )
        for r in override_rows
    ]
    result = mw.effective_state(schedules, overrides, now)
    state.update(
        active=result.active,
        until=iso(result.until),
        source=result.source,
        next_start=iso(result.next_start),
        note=result.note,
    )
    return state
