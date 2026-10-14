"""Database helpers for the Entity Registry plugin."""
from __future__ import annotations

from contextlib import contextmanager
from typing import Generator

from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from core.api import db_instance, get_logger

from .models import _Base

log = get_logger("Plugin:EntityRegistry:DB")

# Columns added after a table's first release go here, keyed by table:
# {"registry_entities": {"new_col": "VARCHAR(64)"}}. create_all() never alters
# existing tables, so this is the (hand-rolled, idempotent) migration path.
_NEW_COLUMNS: dict[str, dict[str, str]] = {}


def ensure_tables() -> None:
    """Create registry tables and apply column migrations (idempotent)."""
    if not db_instance.is_connected or not db_instance.engine:
        raise RuntimeError("Database engine not ready — call after db:connected event")
    _Base.metadata.create_all(db_instance.engine, checkfirst=True)
    _migrate_columns()
    log.info("DB: registry tables ready")


def _migrate_columns() -> None:
    insp = inspect(db_instance.engine)
    for table, new_cols in _NEW_COLUMNS.items():
        existing = {col["name"] for col in insp.get_columns(table)}
        with db_instance.engine.begin() as conn:
            for col, col_type in new_cols.items():
                if col in existing:
                    continue
                nullability = "" if "NOT NULL" in col_type else " NULL"
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}{nullability}"))
                log.info(f"DB: migrated — added column '{col}' to {table}")


@contextmanager
def get_session() -> Generator[Session, None, None]:
    """Yield a SQLAlchemy session, rolling back on exception."""
    if not db_instance.is_connected or not db_instance.SessionLocal:
        raise RuntimeError("Database not connected")
    session: Session = db_instance.SessionLocal()
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
