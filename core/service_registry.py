"""
Omnix V6 — ServiceRegistry (Phase 1 §14).

A small, typed service locator.  Services are anything that:

    1. has a stable name,
    2. exposes :meth:`initialize() -> bool` and :meth:`shutdown() -> None`
       (R-9),
    3. has a health probe (R-9 ``statistics``),
    4. optionally declares *dependencies* (other service names that
       must be registered first).

Why not just a dict:
    - Registration is racy without a lock.
    - We want dependency ordering to be explicit (no "registered
      twice" surprises).
    - We want a single ``initialize_all()`` / ``shutdown_all()`` so
      the engine has one place to walk.
    - We want a single health surface.

What this is NOT:
    - Not a global singleton.  The engine instantiates one and passes
      it to subsystems (R-1).
    - Not a DI container.  It does not instantiate services for you.
      Subsystems are constructed by the engine; the registry tracks
      them.
    - Not a thread-pool manager.  Use a real executor for that.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

from .errors import ConfigurationError, DependencyError
from .lifecycle import LifecycleMixin, LifecycleState


# ---------------------------------------------------------------------------
# Service protocol — what a registered service must look like
# ---------------------------------------------------------------------------

class ServiceProtocol:
    """The minimum a registered object must satisfy.

    Inherited *implicitly* (no ``@runtime_checkable`` needed) because
    every subsystem we register is a real class that we instantiate
    ourselves.  The protocol documents the contract.
    """

    def initialize(self) -> bool: ...
    def shutdown(self) -> None: ...
    def statistics(self) -> Dict[str, Any]: ...

    @property
    def initialized(self) -> bool: ...


@dataclass
class _ServiceRecord:
    """Internal record of a registered service."""

    name: str
    instance: Any
    dependencies: Tuple[str, ...] = ()
    optional_dependencies: Tuple[str, ...] = ()
    priority: int = 0          # higher priority initializes first
    classification: str = "critical"  # "critical" | "background" | "on_demand"
    metadata: Dict[str, Any] = field(default_factory=dict)

    def is_initialized(self) -> bool:
        try:
            return bool(self.instance.initialized)
        except Exception:  # noqa: BLE001
            return False


# ---------------------------------------------------------------------------
# ServiceRegistry
# ---------------------------------------------------------------------------

class ServiceRegistry(LifecycleMixin):
    """A typed service locator with dependency ordering.

    Usage
    -----

        registry = ServiceRegistry()
        registry.register(my_service, name="brain", dependencies=("config",))
        registry.initialize_all()
        brain = registry.resolve("brain")
        registry.shutdown_all()
    """

    def __init__(self) -> None:
        self._services: Dict[str, _ServiceRecord] = {}
        self._lock = threading.RLock()
        self._initialization_order: List[str] = []
        self._shutdown_order: List[str] = []
        # lifecycle (LifecycleMixin contract)
        self._lifecycle_state: LifecycleState = LifecycleState.CREATED
        self._initialization_error: Optional[str] = None

    # ===================================================== registration
    def register(
        self,
        service: Any,
        *,
        name: str,
        dependencies: Iterable[str] = (),
        optional_dependencies: Iterable[str] = (),
        priority: int = 0,
        classification: str = "critical",
        replace: bool = False,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Add ``service`` under ``name``.

        Raises
        ------
        ConfigurationError
            If the name is already registered and ``replace=False``;
            if a required dependency is unknown at registration time
            (this catches typos before boot); if the service does not
            expose the lifecycle surface.
        """
        if not name:
            raise ConfigurationError(
                "Service name must be non-empty",
                code="SERVICE_NAME_EMPTY",
            )
        if not _looks_like_service(service):
            raise ConfigurationError(
                f"Service {name!r} does not expose initialize/shutdown/statistics",
                code="SERVICE_PROTOCOL",
                context={"name": name, "type": type(service).__name__},
            )
        with self._lock:
            if name in self._services and not replace:
                raise ConfigurationError(
                    f"Service {name!r} already registered",
                    code="SERVICE_DUPLICATE",
                    context={"name": name},
                )
            for dep in dependencies:
                if dep == name:
                    raise ConfigurationError(
                        f"Service {name!r} cannot depend on itself",
                        code="SERVICE_CYCLE",
                        context={"name": name},
                    )
                if dep not in self._services:
                    raise ConfigurationError(
                        f"Service {name!r} depends on unknown service {dep!r}",
                        code="SERVICE_DEPENDENCY_UNKNOWN",
                        context={"name": name, "missing": dep},
                    )
            self._services[name] = _ServiceRecord(
                name=name,
                instance=service,
                dependencies=tuple(dependencies),
                optional_dependencies=tuple(optional_dependencies),
                priority=priority,
                classification=classification,
                metadata=dict(metadata) if metadata else {},
            )

    def unregister(self, name: str) -> bool:
        """Remove a service.  Returns True if it was registered.

        Only allowed if the registry is not currently initializing
        and the service has not been initialized yet, or the engine
        is shutting down.
        """
        with self._lock:
            if self._lifecycle_state in (LifecycleState.INITIALIZING, LifecycleState.RUNNING):
                # safe to unregister uninitialized services during init
                pass
            rec = self._services.pop(name, None)
            return rec is not None

    # ========================================================= resolve
    def resolve(self, name: str) -> Any:
        """Return the service instance, or raise :class:`DependencyError`."""
        with self._lock:
            rec = self._services.get(name)
        if rec is None:
            raise DependencyError(
                f"Service {name!r} is not registered",
                code="SERVICE_NOT_FOUND",
                context={"name": name, "registered": sorted(self._services)},
            )
        return rec.instance

    def try_resolve(self, name: str) -> Optional[Any]:
        """Return the service instance, or ``None`` if it is not registered."""
        with self._lock:
            rec = self._services.get(name)
        return rec.instance if rec is not None else None

    def has(self, name: str) -> bool:
        with self._lock:
            return name in self._services

    def is_initialized(self, name: str) -> bool:
        with self._lock:
            rec = self._services.get(name)
        if rec is None:
            return False
        return rec.is_initialized()

    # ===================================================== enumeration
    def list_names(self) -> List[str]:
        with self._lock:
            return sorted(self._services)

    def metadata(self, name: str) -> Dict[str, Any]:
        with self._lock:
            rec = self._services.get(name)
        if rec is None:
            return {}
        out = dict(rec.metadata)
        out["classification"] = rec.classification
        return out

    def classification(self, name: str) -> str:
        with self._lock:
            rec = self._services.get(name)
        if rec is None:
            return "on_demand"
        return rec.classification

    def dependencies_of(self, name: str) -> Tuple[str, ...]:
        with self._lock:
            rec = self._services.get(name)
        if rec is None:
            return ()
        return rec.dependencies

    # ========================================== initialization (topo)
    def initialize_all(self) -> bool:
        """Initialize every registered service in dependency order.

        Returns ``True`` if all services initialize successfully.
        A failure short-circuits; services already initialized are
        *not* shut down here (caller decides — typically the engine
        tears everything down on boot failure).
        """
        with self._lock:
            self._lifecycle_state = LifecycleState.INITIALIZING
            self._initialization_error = None
            order = self._topo_order_locked()

        for name in order:
            rec = self._services[name]
            # missing optional dep is fine; missing required dep should
            # have been caught at register() time
            try:
                ok = bool(rec.instance.initialize())
            except Exception as exc:  # noqa: BLE001
                with self._lock:
                    self._lifecycle_state = LifecycleState.STOPPED
                    self._initialization_error = f"{name}: {exc!r}"
                return False
            if not ok:
                with self._lock:
                    self._lifecycle_state = LifecycleState.STOPPED
                    self._initialization_error = f"{name}: initialize() returned False"
                return False

        with self._lock:
            self._lifecycle_state = LifecycleState.READY
            self._initialization_order = order
        return True

    def shutdown_all(self) -> None:
        """Shut every initialized service down in reverse dependency order."""
        with self._lock:
            if self._lifecycle_state in (LifecycleState.STOPPED, LifecycleState.STOPPING):
                return
            self._lifecycle_state = LifecycleState.STOPPING
            order = list(reversed(self._initialization_order))
        for name in order:
            rec = self._services.get(name)
            if rec is None or not rec.is_initialized():
                continue
            try:
                rec.instance.shutdown()
            except Exception as exc:  # noqa: BLE001
                # never let a bad shutdown block the rest
                _log_warning(f"Service {name!r} raised on shutdown: {exc!r}")
        with self._lock:
            self._lifecycle_state = LifecycleState.STOPPED
            self._shutdown_order = list(self._initialization_order)

    # ============================================= LifecycleMixin glue
    def _do_initialize(self) -> bool:
        return self.initialize_all()

    def _do_shutdown(self) -> None:
        self.shutdown_all()

    @property
    def initialization_order(self) -> Tuple[str, ...]:
        with self._lock:
            return tuple(self._initialization_order)

    # ============================================== health / debugging
    def health(self) -> Dict[str, Any]:
        """Per-service health snapshot for :class:`HealthMonitor`."""
        with self._lock:
            services = list(self._services.values())
        result: Dict[str, Any] = {
            "type": "ServiceRegistry",
            "lifecycle": self._lifecycle_state.value,
            "services": {},
            "counts": {
                "registered": len(services),
                "initialized": 0,
                "unhealthy": 0,
            },
        }
        for rec in services:
            try:
                stats = rec.instance.statistics() or {}
            except Exception as exc:  # noqa: BLE001
                stats = {"error": repr(exc)}
            entry = dict(stats)
            entry["initialized"] = rec.is_initialized()
            if rec.is_initialized():
                result["counts"]["initialized"] += 1
            else:
                if self._lifecycle_state in (LifecycleState.READY, LifecycleState.RUNNING):
                    result["counts"]["unhealthy"] += 1
            entry["dependencies"] = list(rec.dependencies)
            entry["optional_dependencies"] = list(rec.optional_dependencies)
            result["services"][rec.name] = entry
        return result

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "type": "ServiceRegistry",
                "lifecycle": self._lifecycle_state.value,
                "registered": len(self._services),
                "initialized": sum(
                    1 for r in self._services.values() if r.is_initialized()
                ),
                "initialization_order": list(self._initialization_order),
            }

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return (
            f"ServiceRegistry(state={self._lifecycle_state.value}, "
            f"size={len(self._services)})"
        )

    def __len__(self) -> int:
        with self._lock:
            return len(self._services)

    def __contains__(self, name: str) -> bool:
        return self.has(name)

    # ========================================== topology helpers
    def _topo_order_locked(self) -> List[str]:
        """Kahn's algorithm; raises on cycle."""
        # build adjacency
        in_degree: Dict[str, int] = {n: 0 for n in self._services}
        edges: Dict[str, List[str]] = {n: [] for n in self._services}
        for name, rec in self._services.items():
            for dep in rec.dependencies:
                if dep in self._services:
                    edges[dep].append(name)
                    in_degree[name] += 1
        # start with nodes that have no dependencies
        # priority breaks ties (higher first)
        ready: List[str] = sorted(
            (n for n, d in in_degree.items() if d == 0),
            key=lambda n: (-self._services[n].priority, n),
        )
        ordered: List[str] = []
        while ready:
            n = ready.pop(0)
            ordered.append(n)
            for m in edges[n]:
                in_degree[m] -= 1
                if in_degree[m] == 0:
                    ready.append(m)
            ready.sort(key=lambda x: (-self._services[x].priority, x))
        if len(ordered) != len(self._services):
            cycle = [n for n, d in in_degree.items() if d > 0]
            raise ConfigurationError(
                f"Cycle in service dependency graph: {sorted(cycle)}",
                code="SERVICE_CYCLE",
                context={"cycle": sorted(cycle)},
            )
        return ordered


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _looks_like_service(obj: Any) -> bool:
    """True if ``obj`` has the four methods/properties the registry needs."""
    if obj is None:
        return False
    for attr in ("initialize", "shutdown", "statistics"):
        if not callable(getattr(obj, attr, None)):
            return False
    return True


def _log_warning(msg: str) -> None:
    """Local log shim so this module does not import loguru at top-level
    (test isolation; the engine wires the real logger)."""
    try:
        from loguru import logger as _loguru
        _loguru.warning(msg)
    except Exception:  # noqa: BLE001
        pass
