"""Catalog loader — entity types, identifier schemes and relations from YAML.

The catalog is the registry's vocabulary. It is validated with pydantic on
load so a typo in the YAML fails loudly at boot instead of silently accepting
unknown types at identify() time.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field, field_validator

from core.api import get_logger

log = get_logger("Plugin:EntityRegistry:Catalog")

# <plugin root>/catalog — app/logic/catalog.py → parents[2] is the plugin root.
DEFAULT_CATALOG_DIR = Path(__file__).resolve().parents[2] / "catalog"
CATALOG_FILE = "entity_types.yml"

# Priority used for a scheme that is not in the catalog (should not happen —
# identify() rejects unknown schemes — but keeps sorting total).
UNKNOWN_PRIORITY = 1000


class EntityTypeDef(BaseModel):
    id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    label: str
    icon: str = "category"
    description: str = ""


class IdentifierSchemeDef(BaseModel):
    id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_.]*$")
    label: str
    description: str = ""
    priority: int = Field(default=100, ge=0)


class RelationDef(BaseModel):
    id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    label: str
    inverse: Optional[str] = None


class Catalog(BaseModel):
    entity_types: list[EntityTypeDef] = Field(min_length=1)
    identifier_schemes: list[IdentifierSchemeDef] = Field(min_length=1)
    relations: list[RelationDef] = Field(default_factory=list)

    @field_validator("entity_types", "identifier_schemes", "relations")
    @classmethod
    def _unique_ids(cls, items):
        seen: set[str] = set()
        for item in items:
            if item.id in seen:
                raise ValueError(f"duplicate id '{item.id}'")
            seen.add(item.id)
        return items

    def to_payload(self) -> dict:
        return self.model_dump()


class CatalogLoader:
    def __init__(self, catalog_dir: Path | None = None) -> None:
        self.catalog_dir = Path(catalog_dir or DEFAULT_CATALOG_DIR)
        self._catalog: Catalog | None = None

    def load(self) -> Catalog:
        path = self.catalog_dir / CATALOG_FILE
        with path.open("r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
        self._catalog = Catalog.model_validate(raw)
        log.info(
            f"Catalog loaded from {path}: {len(self._catalog.entity_types)} types, "
            f"{len(self._catalog.identifier_schemes)} schemes, "
            f"{len(self._catalog.relations)} relations"
        )
        return self._catalog

    def reload(self) -> Catalog:
        return self.load()

    @property
    def catalog(self) -> Catalog:
        if self._catalog is None:
            self.load()
        assert self._catalog is not None
        return self._catalog

    # ── Accessors ────────────────────────────────────────────────────────────

    def entity_type_ids(self) -> list[str]:
        return [t.id for t in self.catalog.entity_types]

    def scheme_ids(self) -> list[str]:
        return [s.id for s in self.catalog.identifier_schemes]

    def relation_ids(self) -> list[str]:
        return [r.id for r in self.catalog.relations]

    def scheme_priority(self, scheme: str) -> int:
        for s in self.catalog.identifier_schemes:
            if s.id == scheme:
                return s.priority
        return UNKNOWN_PRIORITY

    def inverse_relation(self, relation: str) -> str | None:
        for r in self.catalog.relations:
            if r.id == relation:
                return r.inverse
        return None
