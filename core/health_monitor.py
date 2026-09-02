"""
Omnix V6 — HealthMonitor (Phase 1 §28).

Reports *meaningful* health for the running engine.  Per R-1 and
the engine architecture, the engine is the only place that knows
about all the subsystems.  This monitor lives in the engine and
collects health from each named subsystem on demand.

A subsystem is "healthy" iff:
    1. its lifecycle state is READY or RUNNING, AND
    2. its own ``statistics()`` does not report an "unhealthy" key, AND
    3. (optional) a custom probe returns True.

The monitor is *not* a watchdog; it does not poll, it does not page,
it does not restart.  It is a snapshot surface.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple

from .lifecycle import LifecycleMixin, LifecycleState


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

class HealthStatus(str, Enum):
    """Per-subsystem health verdict."""

    UNKNOWN = "unknown"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"


@dataclass(frozen=True)
class SubsystemHealth:
    """The health of one subsystem at one moment."""

    name: str
    status: HealthStatus
    lifecycle: str
    detail: str = ""
    last_checked: float = field(default_factory=time.time)
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status.value,
            "lifecycle": self.lifecycle,
            "detail": self.detail,
            "last_checked": self.last_checked,
            "extra": dict(self.extra),
        }


# ---------------------------------------------------------------------------
# Health probe
# ---------------------------------------------------------------------------

HealthProbe = Callable[[], bool]


# ---------------------------------------------------------------------------
# HealthMonitor
# ---------------------------------------------------------------------------

class HealthMonitor(LifecycleMixin):
    """Aggregates health across all subsystems.

    The monitor is a *collector*, not a source of truth: it asks each
    subsystem for its state.  Subsystems can also be registered
    with a custom :class:`HealthProbe` if the simple lifecycle check
    is not enough.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        # subsystem_name -> {"lifecycle_attr": str, "probe": callable, "instance": Any}
        self._tracked: Dict[str, Dict[str, Any]] = {}
        self._last_report: Dict[str, Any] = {}
        self._lifecycle_state: LifecycleState = LifecycleState.CREATED
        self._initialization_error: Optional[str] = None

    # ============================================== tracking API
    def track(
        self,
        name: str,
        instance: Any,
        *,
        probe: Optional[HealthProbe] = None,
    ) -> None:
        """Register ``instance`` under ``name`` so the monitor can poll it.

        ``probe`` (optional) is a callable returning True iff the
        subsystem is healthy.  If omitted, the monitor relies on
        ``lifecycle_state`` (READY/RUNNING = healthy) and the
        ``statistics()`` "unhealthy" key.
        """
        with self._lock:
            self._tracked[name] = {
                "instance": instance,
                "probe": probe,
            }

    def untrack(self, name: str) -> bool:
        with self._lock:
            return self._tracked.pop(name, None) is not None

    def tracked(self) -> Tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._tracked))

    # ============================================== polling
    def probe_one(self, name: str) -> SubsystemHealth:
        """Force a fresh health check on one subsystem."""
        with self._lock:
            entry = self._tracked.get(name)
        if entry is None:
            return SubsystemHealth(
                name=name,
                status=HealthStatus.UNKNOWN,
                lifecycle="",
                detail="not tracked",
            )
        instance = entry["instance"]
        probe: Optional[HealthProbe] = entry["probe"]
        # lifecycle
        lifecycle = "unknown"
        try:
            ls = getattr(instance, "lifecycle_state", None)
            if ls is not None:
                lifecycle = (
                    ls.value if hasattr(ls, "value") else str(ls)
                )
        except Exception:  # noqa: BLE001
            lifecycle = "error"
        # custom probe
        if probe is not None:
            try:
                ok = bool(probe())
            except Exception as exc:  # noqa: BLE001
                return SubsystemHealth(
                    name=name,
                    status=HealthStatus.UNHEALTHY,
                    lifecycle=lifecycle,
                    detail=f"probe raised: {exc!r}",
                )
            status = HealthStatus.HEALTHY if ok else HealthStatus.DEGRADED
        else:
            status = self._derive_status_from_lifecycle(lifecycle, instance)
        return SubsystemHealth(name=name, status=status, lifecycle=lifecycle)

    def probe_all(self) -> List[SubsystemHealth]:
        """Force fresh health checks on every tracked subsystem."""
        with self._lock:
            names = list(self._tracked)
        out: List[SubsystemHealth] = []
        for n in names:
            out.append(self.probe_one(n))
        return out

    # =================================================== report
    def report(self) -> Dict[str, Any]:
        """Snapshot of the whole engine.

        Returns a JSON-safe dict shaped as:

            {
                "overall": "healthy" | "degraded" | "unhealthy" | "unknown",
                "checked_at": float,
                "subsystems": {name: SubsystemHealth.to_dict(), ...},
                "engine": {...}
            }
        """
        subs = self.probe_all()
        sub_map = {s.name: s.to_dict() for s in subs}
        if not subs:
            overall = HealthStatus.UNKNOWN
        elif any(s.status is HealthStatus.UNHEALTHY for s in subs):
            overall = HealthStatus.UNHEALTHY
        elif any(s.status is HealthStatus.DEGRADED for s in subs):
            overall = HealthStatus.DEGRADED
        elif all(s.status is HealthStatus.HEALTHY for s in subs):
            overall = HealthStatus.HEALTHY
        else:
            overall = HealthStatus.UNKNOWN
        report = {
            "overall": overall.value,
            "checked_at": time.time(),
            "subsystems": sub_map,
        }
        with self._lock:
            self._last_report = report
        return report

    def last_report(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._last_report)

    # =================================================== lifecycle
    def _do_initialize(self) -> bool:
        # No heavy work; the monitor is a pure collector.
        return True

    def _do_shutdown(self) -> None:
        with self._lock:
            self._tracked.clear()
            self._last_report.clear()

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "type": "HealthMonitor",
                "tracked": len(self._tracked),
                "lifecycle": self._lifecycle_state.value,
            }

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return (
            f"HealthMonitor(state={self._lifecycle_state.value}, "
            f"tracked={len(self._tracked)})"
        )

    # ============================================ helpers
    @staticmethod
    def _derive_status_from_lifecycle(lifecycle: str, instance: Any) -> HealthStatus:
        if lifecycle in ("ready", "running"):
            # peek statistics for an "unhealthy" key
            try:
                stats = instance.statistics() or {}
            except Exception:  # noqa: BLE001
                return HealthStatus.DEGRADED
            if isinstance(stats, dict) and stats.get("unhealthy"):
                return HealthStatus.UNHEALTHY
            return HealthStatus.HEALTHY
        if lifecycle in ("created", "initializing"):
            return HealthStatus.UNKNOWN
        if lifecycle in ("stopping", "stopped"):
            return HealthStatus.UNHEALTHY
        # Lifecycle-agnostic subsystems (LLM providers, plain adapters)
        # expose ``health()`` instead.  Honour it when present so a
        # tracked provider doesn't show up as ``degraded`` just because
        # it has no ``lifecycle_state`` attribute.
        if lifecycle == "unknown":
            try:
                health_fn = getattr(instance, "health", None)
                if callable(health_fn):
                    h = health_fn() or {}
                    if isinstance(h, dict):
                        if h.get("ok") is True:
                            return HealthStatus.HEALTHY
                        if h.get("ok") is False:
                            return HealthStatus.UNHEALTHY
            except Exception:  # noqa: BLE001
                return HealthStatus.DEGRADED
        return HealthStatus.DEGRADED
