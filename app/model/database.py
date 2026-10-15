"""Database helpers for the Entity Registry plugin.

Cluster notes (core API ≥ 1.7): N core instances share one MariaDB. Bootstrap
DDL runs under ``db_instance.advisory_lock("lyndrix_bootstrap")`` and treats
"already exists" errors as "a peer did it". MariaDB ≥ 11.6 runs
``innodb_snapshot_isolation=ON``: a REPEATABLE READ transaction that updates a
row another transaction changed since its first read fails with error 1020
instead of waiting — so sessions run READ COMMITTED and the short write
transactions are retried on 1020/1205/1213 (see ``retry_write``).
"""
from __future__ import annotations

import re
import time
from contextlib import contextmanager, nullcontext
from typing import Callable, Generator, TypeVar

from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from core.api import db_instance, get_logger

from .models import _Base

log = get_logger("Plugin:EntityRegistry:DB")

T = TypeVar("T")

# MariaDB error codes.
RETRYABLE_CODES = frozenset({1020, 1205, 1213})  # snapshot conflict, lock wait timeout, deadlock
_EXISTS_CODES = frozenset({1050, 1060, 1061})    # table / column / index already exists
_EXISTS_TEXT = ("already exists", "duplicate column")

# Columns added after a table's first release, keyed by table:
# {"registry_entities": {"new_col": "VARCHAR(64)"}}. create_all() never alters
# existing tables, so this is the (hand-rolled, idempotent) migration path.
_NEW_COLUMNS: dict[str, dict[str, str]] = {
    # 0.2.0 — unique "open" key so two instances cannot file one conflict twice.
    "registry_conflicts": {"open_fingerprint": "VARCHAR(64)"},
}
# Unique indexes on migrated columns: (index name, table, column).
_NEW_UNIQUE_INDEXES: list[tuple[str, str, str]] = [
    ("uq_registry_conflict_open", "registry_conflicts", "open_fingerprint"),
]


# ── Error classification ──────────────────────────────────────────────────────

def db_error_code(exc: BaseException) -> int | None:
    """The MySQL/MariaDB error number behind a (SQLAlchemy-wrapped) DB error."""
    orig = getattr(exc, "orig", None) or exc
    errno = getattr(orig, "errno", None)
    if isinstance(errno, int):
        return errno
    args = getattr(orig, "args", ())
    if args and isinstance(args[0], int):
        return args[0]
    match = re.match(r"\(?(\d{4})", str(orig))
    return int(match.group(1)) if match else None


def is_retryable(exc: BaseException) -> bool:
    return db_error_code(exc) in RETRYABLE_CODES


def is_already_exists(exc: BaseException) -> bool:
    if db_error_code(exc) in _EXISTS_CODES:
        return True
    msg = str(getattr(exc, "orig", None) or exc).lower()
    return any(t in msg for t in _EXISTS_TEXT)


def retry_write(fn: Callable[[], T], *, attempts: int = 3, pause: float = 0.05) -> T:
    """Run ``fn()`` — one complete short transaction — again on 1020/1205/1213."""
    for attempt in range(attempts):
        try:
            return fn()
        except Exception as exc:
            if attempt + 1 < attempts and is_retryable(exc):
                log.debug(f"DB: retrying write after {db_error_code(exc)} (attempt {attempt + 1})")
                time.sleep(pause * (attempt + 1))
                continue
            raise
    raise AssertionError("unreachable")  # pragma: no cover


# ── Bootstrap ─────────────────────────────────────────────────────────────────

def _bootstrap_lock():
    lock = getattr(db_instance, "advisory_lock", None)  # core API ≥ 1.7
    return lock("lyndrix_bootstrap") if callable(lock) else nullcontext()


def ensure_tables() -> None:
    """Create registry tables and apply column migrations.

    Idempotent and safe when several instances boot at once: serialized by the
    core bootstrap lock, and a lost race ("table/column/index already exists")
    counts as success.
    """
    if not db_instance.is_connected or not db_instance.engine:
        raise RuntimeError("Database engine not ready — call after db:connected event")
    with _bootstrap_lock():
        try:
            _Base.metadata.create_all(db_instance.engine, checkfirst=True)
        except Exception as exc:
            if not is_already_exists(exc):
                raise
            log.info("DB: a peer created registry tables concurrently — re-checking")
            _Base.metadata.create_all(db_instance.engine, checkfirst=True)
        _migrate_columns()
    log.info("DB: registry tables ready")


def _migrate_columns() -> None:
    engine = db_instance.engine
    mariadb = engine.dialect.name in ("mysql", "mariadb")
    insp = inspect(engine)
    for table, new_cols in _NEW_COLUMNS.items():
        existing = {col["name"] for col in insp.get_columns(table)}
        for col, col_type in new_cols.items():
            if col in existing:
                continue
            nullability = "" if "NOT NULL" in col_type else " NULL"
            if_not = " IF NOT EXISTS" if mariadb else ""
            try:
                with engine.begin() as conn:
                    conn.execute(text(
                        f"ALTER TABLE {table} ADD COLUMN{if_not} {col} {col_type}{nullability}"
                    ))
                log.info(f"DB: migrated — added column '{col}' to {table}")
            except Exception as exc:
                if not is_already_exists(exc):
                    raise

    _backfill_open_fingerprints()

    for name, table, col in _NEW_UNIQUE_INDEXES:
        try:
            with engine.begin() as conn:
                conn.execute(text(f"CREATE UNIQUE INDEX IF NOT EXISTS {name} ON {table} ({col})"))
        except Exception as exc:
            if not is_already_exists(exc):
                raise


def _backfill_open_fingerprints() -> None:
    """Give each pre-0.2.0 open conflict its unique open key (oldest row wins
    when the same conflict was already filed twice)."""
    with db_instance.engine.begin() as conn:
        rows = conn.execute(text(
            "SELECT id, fingerprint FROM registry_conflicts "
            "WHERE status = 'open' AND open_fingerprint IS NULL ORDER BY id"
        )).all()
        taken = {
            r[0] for r in conn.execute(text(
                "SELECT open_fingerprint FROM registry_conflicts WHERE open_fingerprint IS NOT NULL"
            ))
        }
        for row_id, fingerprint in rows:
            if fingerprint in taken:
                continue
            conn.execute(
                text("UPDATE registry_conflicts SET open_fingerprint = :f WHERE id = :i"),
                {"f": fingerprint, "i": row_id},
            )
            taken.add(fingerprint)


# ── Sessions ──────────────────────────────────────────────────────────────────

@contextmanager
def get_session() -> Generator[Session, None, None]:
    """Yield a READ COMMITTED session (no-op on SQLite), rolling back on exception."""
    if not db_instance.is_connected or not db_instance.SessionLocal:
        raise RuntimeError("Database not connected")
    session: Session = db_instance.SessionLocal()
    try:
        bind = session.get_bind()
        if bind is not None and getattr(bind.dialect, "name", "") in ("mysql", "mariadb"):
            session.connection(execution_options={"isolation_level": "READ COMMITTED"})
    except Exception as exc:  # isolation is an optimisation, never a reason to fail
        log.debug(f"DB: READ COMMITTED not applied: {exc}")
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
