"""SQLAlchemy models for the Entity Registry plugin.

Entity      — one row per configuration item (CI); the UUID is the id other
              plugins store. Merged entities stay as tombstones
              (status="merged", merged_into=<uuid>) so stale references
              still resolve.
Identifier  — (scheme, value) → entity. Exactly one owner per pair, enforced
              by the unique constraint; this is what makes identify() safe.
Relation    — directed, labelled edge between two entities.
Conflict    — an identify() call whose identifiers pointed at several
              entities; parked for an admin to merge or dismiss.
"""
from __future__ import annotations

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase


class _Base(DeclarativeBase):
    pass


class Entity(_Base):
    __tablename__ = "registry_entities"

    uuid = Column(String(36), primary_key=True)
    entity_type = Column(String(64), nullable=False, index=True)
    display_name = Column(String(255), nullable=False)
    # active | retired | merged
    status = Column(String(16), nullable=False, default="active", index=True)
    # Plugin/system that first registered the entity.
    owner_source = Column(String(64), nullable=True)
    # Every source that has reported this entity ("seen by").
    sources = Column(JSON, nullable=False, default=list)
    # Weak / descriptive facts (ip, site, stage, ...). Never used for matching.
    attributes = Column(JSON, nullable=False, default=dict)
    # Set on tombstones: the surviving entity of a merge.
    merged_into = Column(String(36), nullable=True)
    created_at = Column(DateTime, nullable=False, default=func.now())
    updated_at = Column(DateTime, nullable=False, default=func.now(), onupdate=func.now())
    last_seen_at = Column(DateTime, nullable=True)

    def to_dict(self) -> dict:
        return {
            "uuid": self.uuid,
            "entity_type": self.entity_type,
            "display_name": self.display_name,
            "status": self.status,
            "owner_source": self.owner_source,
            "sources": list(self.sources or []),
            "attributes": dict(self.attributes or {}),
            "merged_into": self.merged_into,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "last_seen_at": self.last_seen_at.isoformat() if self.last_seen_at else None,
        }


class Identifier(_Base):
    __tablename__ = "registry_identifiers"
    __table_args__ = (
        UniqueConstraint("scheme", "value", name="uq_registry_identifier"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    entity_uuid = Column(
        String(36), ForeignKey("registry_entities.uuid"), nullable=False, index=True
    )
    scheme = Column(String(64), nullable=False)
    value = Column(String(255), nullable=False)
    source = Column(String(64), nullable=True)
    created_at = Column(DateTime, nullable=False, default=func.now())

    def to_dict(self) -> dict:
        return {
            "scheme": self.scheme,
            "value": self.value,
            "source": self.source,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class Relation(_Base):
    __tablename__ = "registry_relations"
    __table_args__ = (
        UniqueConstraint("from_uuid", "to_uuid", "relation", name="uq_registry_relation"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    from_uuid = Column(
        String(36), ForeignKey("registry_entities.uuid"), nullable=False, index=True
    )
    to_uuid = Column(
        String(36), ForeignKey("registry_entities.uuid"), nullable=False, index=True
    )
    relation = Column(String(64), nullable=False)
    source = Column(String(64), nullable=True)
    created_at = Column(DateTime, nullable=False, default=func.now())

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "from_uuid": self.from_uuid,
            "to_uuid": self.to_uuid,
            "relation": self.relation,
            "source": self.source,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class Conflict(_Base):
    __tablename__ = "registry_conflicts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    entity_type = Column(String(64), nullable=False)
    # Stable key over (entity_type, sorted candidate uuids) so the same
    # unresolved conflict is filed once, not on every sync run.
    fingerprint = Column(String(64), nullable=False, index=True)
    # [{uuid, display_name, matched: [scheme, ...]}, ...]
    candidates = Column(JSON, nullable=False, default=list)
    # The identifiers of the identify() call that exposed the conflict.
    identifiers = Column(JSON, nullable=False, default=dict)
    source = Column(String(64), nullable=True)
    # open | resolved | dismissed
    status = Column(String(16), nullable=False, default="open", index=True)
    resolution = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, default=func.now())
    resolved_at = Column(DateTime, nullable=True)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "entity_type": self.entity_type,
            "candidates": list(self.candidates or []),
            "identifiers": dict(self.identifiers or {}),
            "source": self.source,
            "status": self.status,
            "resolution": self.resolution,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
        }
