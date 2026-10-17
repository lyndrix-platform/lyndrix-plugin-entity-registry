"""Test bootstrap and shared fixtures.

Importing ``core.api`` cold trips a circular import inside lyndrix-core
(core.api → plugins → ui.layout → notifications → core.api). The running
platform never hits it because main.py loads core.services first; mirror that
order here before any plugin module pulls in core.api.
"""

# TODO(agent): fix the core.api ↔ ui.layout cycle in lyndrix-core and drop this.
import core.services  # noqa: F401,I001

import importlib
import importlib.util
import logging
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

REPO_ROOT = Path(__file__).resolve().parents[1]


class _FakeCtx:
    """Collects emits; ``cluster=True`` topics are recorded separately."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []
        self.cluster_topics: list[str] = []

    def emit(self, topic, payload, cluster=False):
        self.events.append((topic, payload))
        if cluster:
            self.cluster_topics.append(topic)

    def topics(self) -> list[str]:
        return [t for t, _ in self.events]


class _SetupCtx(_FakeCtx):
    """Enough of ``ModuleContext`` for ``entrypoint.setup()``: records the bus
    handlers per topic and enforces the manifest's subscribe/emit permissions
    like the core does."""

    def __init__(self, manifest) -> None:
        super().__init__()
        self.manifest = manifest
        self.handlers: dict = {}
        self.routers: list = []
        self.log = logging.getLogger("test.entity_registry")

    def subscribe(self, topic: str):
        assert (
            topic in self.manifest.permissions.subscribe
        ), f"undeclared subscribe: {topic}"

        def decorator(fn):
            self.handlers[topic] = fn
            return fn

        return decorator

    def emit(self, topic, payload, cluster=False):
        assert topic in self.manifest.permissions.emit, f"undeclared emit: {topic}"
        super().emit(topic, payload, cluster=cluster)

    def register_routes(self, router, public=False):
        self.routers.append(router)

    def create_task(self, coro, *, name=None):
        coro.close()


@pytest.fixture()
def svc(monkeypatch):
    """Real ``EntityRegistryService`` against in-memory SQLite (FKs enforced)."""
    from app.logic.service import EntityRegistryService
    from app.model import database

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    # Enforce FKs like MariaDB does, so flush-order bugs fail here too.
    event.listen(
        engine, "connect", lambda conn, _rec: conn.execute("PRAGMA foreign_keys=ON")
    )
    monkeypatch.setattr(
        database,
        "db_instance",
        SimpleNamespace(
            engine=engine, SessionLocal=sessionmaker(bind=engine), is_connected=True
        ),
    )
    service = EntityRegistryService()
    service.set_context(_FakeCtx())
    service.catalog.load()
    service.on_db_connected()
    return service


_PKG = "_entity_registry_under_test"


@pytest.fixture()
def entrypoint(svc, monkeypatch):
    """``entrypoint.py`` imported the way the core does (as a package member, so
    its relative imports work) with its module-level ``svc`` bound to the test
    service. The ``app`` package is aliased into the synthetic package so the
    entrypoint and the tests share one set of classes (``RegistryError`` …)."""
    if f"{_PKG}.entrypoint" not in sys.modules:
        importlib.import_module("app.api")
        importlib.import_module("app.logic.service")
        pkg = types.ModuleType(_PKG)
        pkg.__path__ = [str(REPO_ROOT)]  # type: ignore[attr-defined]
        sys.modules[_PKG] = pkg
        for name in list(sys.modules):
            if name == "app" or name.startswith("app."):
                sys.modules[f"{_PKG}.{name}"] = sys.modules[name]
        spec = importlib.util.spec_from_file_location(
            f"{_PKG}.entrypoint", REPO_ROOT / "entrypoint.py"
        )
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    module = sys.modules[f"{_PKG}.entrypoint"]
    monkeypatch.setattr(module, "svc", svc)
    return module


@pytest.fixture()
def bus(entrypoint, svc):
    """Run ``setup()`` against a recording context → ``(ctx, handlers)``."""
    ctx = _SetupCtx(entrypoint.manifest)
    entrypoint.setup(ctx)
    return ctx
