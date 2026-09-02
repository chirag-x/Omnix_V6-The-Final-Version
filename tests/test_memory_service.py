"""
Omnix V6 — Tests for engine integration of the Memory Service (Phase 9).

These tests complement :mod:`tests.test_memory_api`, which exercises the
service in isolation.  Here we focus on the *engine* layer:

* The engine must accept a ``memory=`` keyword and use the injected
  service.
* The engine must construct a default in-memory service when none is
  injected.
* The memory service must be registered in the :class:`ServiceRegistry`
  under the canonical name ``"memory"`` so that the registry walks its
  lifecycle alongside every other service.
* The memory service must be tracked by :class:`HealthMonitor`.
* The engine's :meth:`OmnixEngine.statistics` must include a ``memory``
  block, satisfying R-9.
* The engine must shut the memory service down on engine shutdown.
* The engine must expose ``engine.memory`` for direct inspection by
  host code.
* A end-to-end round-trip (remember -> recall) must work through the
  engine.
* R-13: memory is a service, not a singleton.  Two engines in the same
  process must each have their own, independent service.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Ensure the project root is on sys.path so ``import core`` works when
# this file is invoked directly (e.g. via an IDE).
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from core.configuration import OmnixConfig
from core.omnix_engine import OmnixEngine
from core.services.memory_service import (
    InMemoryStore,
    MemoryEntry,
    MemoryKind,
    MemoryService,
)
from core.services.sqlite_memory_store import SQLiteMemoryStore


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def config() -> OmnixConfig:
    """A default :class:`OmnixConfig` for tests."""
    return OmnixConfig(
        project_root=Path("."),
        data_dir=Path(".data"),
        log_dir=Path(".log"),
        env_file=Path(".env"),
    )


@pytest.fixture
def fresh_store() -> InMemoryStore:
    """An empty in-memory store, already initialized."""
    store = InMemoryStore()
    assert store.initialize() is True
    return store


@pytest.fixture
def fresh_service(fresh_store) -> MemoryService:
    """An initialized :class:`MemoryService` for direct (non-engine) tests."""
    return MemoryService(store=fresh_store)


# ---------------------------------------------------------------------------
# Engine integration: default construction
# ---------------------------------------------------------------------------

class TestEngineDefaultMemory:
    """When the caller does not inject a memory service, the engine
    must build a default one (R-13)."""

    def test_engine_constructs_default_memory_service(self, config):
        engine = OmnixEngine(config)
        # Engine must expose ``self.memory`` (R-9 / R-13).
        assert engine.memory is not None
        assert isinstance(engine.memory, MemoryService)

    def test_default_memory_service_is_initialized(self, config):
        engine = OmnixEngine(config)
        engine.initialize()
        try:
            # The default service must have walked its lifecycle so
            # host code can call remember/recall immediately.
            assert engine.memory.initialized is True
        finally:
            engine.shutdown()

    def test_default_memory_uses_in_memory_store(self, config):
        """The default backend must be the in-memory store (so tests
        do not require a database file)."""
        engine = OmnixEngine(config)
        # Access the underlying store via the private attribute on
        # the service.  The InMemoryStore is the default; the engine
        # itself does not need to expose it.
        store = getattr(engine.memory, "_store", None)
        assert isinstance(store, InMemoryStore)


# ---------------------------------------------------------------------------
# Engine integration: injection
# ---------------------------------------------------------------------------

class TestEngineInjectedMemory:
    """The engine must accept a custom memory service through the
    ``memory=`` keyword (R-13, AD-18)."""

    def test_engine_accepts_injected_memory_service(self, config, fresh_service):
        engine = OmnixEngine(config, memory=fresh_service)
        assert engine.memory is fresh_service

    def test_engine_uses_injected_store(self, config, fresh_service):
        engine = OmnixEngine(config, memory=fresh_service)
        engine.initialize()
        try:
            # Write through the *engine's* memory handle and confirm
            # it lands in the injected store.
            r = engine.memory.remember(
                content="the quick brown fox",
                kind=MemoryKind.FACT,
            )
            assert r.success is True
            assert fresh_service._store.count() == 1
        finally:
            engine.shutdown()

    def test_engine_accepts_sqlite_memory_service(self, config, tmp_path):
        """Hosts may inject the SQLite backend for persistence."""
        db_path = str(tmp_path / "phase9_test.sqlite3")
        store = SQLiteMemoryStore(db_path=db_path)
        assert store.initialize() is True
        service = MemoryService(store=store)
        engine = OmnixEngine(config, memory=service)
        engine.initialize()
        try:
            r = engine.memory.remember(
                content="persistent fact",
                kind=MemoryKind.FACT,
            )
            assert r.success is True
        finally:
            engine.shutdown()
        # SQLite store should still have the entry on disk after the
        # engine shuts down.
        store2 = SQLiteMemoryStore(db_path=db_path)
        assert store2.initialize() is True
        try:
            assert store2.count() == 1
        finally:
            store2.shutdown()


# ---------------------------------------------------------------------------
# ServiceRegistry integration
# ---------------------------------------------------------------------------

class TestMemoryServiceRegistration:
    """The memory service must be registered in the :class:`ServiceRegistry`
    under the canonical name ``"memory"`` so its lifecycle is walked by
    ``initialize_all`` / ``shutdown_all``."""

    def test_memory_registered_in_service_registry(self, config):
        engine = OmnixEngine(config)
        engine.initialize()
        try:
            assert engine.services.has("memory") is True
        finally:
            engine.shutdown()

    def test_memory_resolvable_from_service_registry(self, config):
        engine = OmnixEngine(config)
        engine.initialize()
        try:
            resolved = engine.services.resolve("memory")
            assert resolved is engine.memory
        finally:
            engine.shutdown()

    def test_memory_already_registered_is_not_re_registered(
        self, config, fresh_service
    ):
        """If a host pre-registers a service named ``memory`` before
        the engine is initialized, the engine must reuse that
        instance rather than overwrite it."""
        engine = OmnixEngine(config)
        engine.services.register(fresh_service, name="memory", priority=85)
        engine.initialize()
        try:
            assert engine.services.resolve("memory") is fresh_service
        finally:
            engine.shutdown()


# ---------------------------------------------------------------------------
# HealthMonitor integration
# ---------------------------------------------------------------------------

class TestMemoryInHealthMonitor:
    """R-9: every tracked subsystem must appear in the health report."""

    def test_memory_appears_in_health_report(self, config):
        engine = OmnixEngine(config)
        engine.initialize()
        try:
            report = engine.health.report()
            # The health report groups subsystems under "subsystems".
            assert "memory" in report["subsystems"]
        finally:
            engine.shutdown()

    def test_health_report_memory_block_has_status(self, config):
        engine = OmnixEngine(config)
        engine.initialize()
        try:
            report = engine.health.report()
            mem_block = report["subsystems"]["memory"]
            # The health block must report *something* about status,
            # not raise or return None.
            assert mem_block is not None
            assert "status" in mem_block
        finally:
            engine.shutdown()


# ---------------------------------------------------------------------------
# statistics() integration (R-9)
# ---------------------------------------------------------------------------

class TestMemoryInStatistics:
    """The engine's :meth:`statistics` must include a ``memory`` block."""

    def test_statistics_contains_memory_key(self, config):
        engine = OmnixEngine(config)
        engine.initialize()
        try:
            stats = engine.statistics()
            assert "memory" in stats
        finally:
            engine.shutdown()

    def test_statistics_memory_block_shape(self, config):
        engine = OmnixEngine(config)
        engine.initialize()
        try:
            stats = engine.statistics()
            mem = stats["memory"]
            # The block must at minimum identify the service type.
            assert "type" in mem
            # ``MemoryService.statistics`` reports the entry count.
            assert "entries" in mem
            assert mem["entries"] == 0
        finally:
            engine.shutdown()

    def test_statistics_memory_block_grows_with_writes(self, config):
        engine = OmnixEngine(config)
        engine.initialize()
        try:
            engine.memory.remember("alpha", kind=MemoryKind.FACT)
            engine.memory.remember("beta", kind=MemoryKind.FACT)
            stats = engine.statistics()
            assert stats["memory"]["entries"] == 2
        finally:
            engine.shutdown()


# ---------------------------------------------------------------------------
# Lifecycle: shutdown walks the memory service
# ---------------------------------------------------------------------------

class TestMemoryLifecycleOnShutdown:
    """The engine's ``shutdown`` must shut the memory service down so
    SQLite connections close, etc."""

    def test_shutdown_closes_memory_service(self, config):
        engine = OmnixEngine(config)
        engine.initialize()
        # After init the default in-memory store is alive.
        assert engine.memory.initialized is True
        engine.shutdown()
        assert engine.memory.initialized is False

    def test_shutdown_closes_sqlite_memory_service(self, config, tmp_path):
        db_path = str(tmp_path / "lifecycle_test.sqlite3")
        store = SQLiteMemoryStore(db_path=db_path)
        assert store.initialize() is True
        service = MemoryService(store=store)
        engine = OmnixEngine(config, memory=service)
        engine.initialize()
        assert store._initialized is True
        engine.shutdown()
        # The store must be shut down too.
        assert store._initialized is False


# ---------------------------------------------------------------------------
# End-to-end: remember + recall through the engine
# ---------------------------------------------------------------------------

class TestEngineEndToEndMemory:
    """A full remember -> recall round-trip through the engine."""

    def test_engine_remember_and_recall(self, config):
        engine = OmnixEngine(config)
        engine.initialize()
        try:
            r = engine.memory.remember(
                "the capital of France is Paris",
                kind=MemoryKind.FACT,
                tags=("geography",),
            )
            assert r.success is True
            entry_id = r.value.id

            # Direct lookup by id
            g = engine.memory.get(entry_id)
            assert g.success is True
            assert "Paris" in g.value.content

            # Recall by query
            rec = engine.memory.recall("capital of France")
            assert rec.success is True
            assert len(rec.value) >= 1
            assert any("Paris" in h.entry.content for h in rec.value)
        finally:
            engine.shutdown()

    def test_engine_secret_rejection_e2e(self, config):
        engine = OmnixEngine(config)
        engine.initialize()
        try:
            r = engine.memory.remember(
                "my key is sk-abcdef1234567890ABCDEFGH",
                kind=MemoryKind.FACT,
            )
            assert r.success is False
            assert r.error_code == "SECRET_DETECTED"
            # No entry should have been written.
            assert engine.memory._store.count() == 0
        finally:
            engine.shutdown()

    def test_engine_forget_by_id(self, config):
        engine = OmnixEngine(config)
        engine.initialize()
        try:
            r = engine.memory.remember("ephemeral", kind=MemoryKind.FACT)
            assert r.success is True
            entry_id = r.value.id
            f = engine.memory.forget(entry_id=entry_id)
            assert f.success is True
            assert engine.memory.get(entry_id).success is False
        finally:
            engine.shutdown()


# ---------------------------------------------------------------------------
# R-13: two engines, two independent memory services
# ---------------------------------------------------------------------------

class TestEngineMemoryIndependence:
    """R-13: memory is a service, not a singleton.  Two engines in the
    same process must not share state."""

    def test_two_engines_have_independent_memory(self, config):
        engine_a = OmnixEngine(config)
        engine_b = OmnixEngine(config)
        try:
            assert engine_a.memory is not engine_b.memory
            engine_a.initialize()
            engine_b.initialize()
            try:
                engine_a.memory.remember("only in A", kind=MemoryKind.FACT)
                assert engine_a.memory._store.count() == 1
                assert engine_b.memory._store.count() == 0
            finally:
                engine_a.shutdown()
                engine_b.shutdown()
        finally:
            # Defensive double-shutdown in case initialize failed.
            for eng in (engine_a, engine_b):
                if eng._lifecycle_state.name != "STOPPED":
                    try:
                        eng.shutdown()
                    except Exception:  # noqa: BLE001
                        pass

    def test_engine_with_injected_memory_uses_same_instance(self, config, fresh_service):
        engine = OmnixEngine(config, memory=fresh_service)
        engine.initialize()
        try:
            # The service the engine exposes IS the injected one.
            assert engine.memory is fresh_service
            # And the registry resolves to the same object.
            assert engine.services.resolve("memory") is fresh_service
        finally:
            engine.shutdown()


# ---------------------------------------------------------------------------
# Repr / __repr__ contract (R-9)
# ---------------------------------------------------------------------------

class TestMemoryRepr:
    """R-9: every subsystem must implement __repr__."""

    def test_engine_repr_includes_state(self, config):
        engine = OmnixEngine(config)
        r = repr(engine)
        assert "OmnixEngine" in r
        assert "state" in r

    def test_memory_service_repr(self, fresh_service):
        r = repr(fresh_service)
        assert "MemoryService" in r
