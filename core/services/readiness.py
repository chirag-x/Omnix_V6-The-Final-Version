"""
Omnix V6 — Readiness gate.

A small, deterministic readiness report that the engine consults
before announcing itself.  Three classifications:

* ``critical`` — must be READY before the user-facing
  announcement.  The engine blocks on these.
* ``background`` — initialised in parallel during boot.  Their
  readiness is reported but does not gate the announcement.
* ``on_demand`` — initialised the first time a request needs
  them.  Not part of the boot-time readiness report.

The gate builds a :class:`ReadinessReport` from the
:class:`ServiceRegistry` so it never has to know about individual
services.  New services just register with the right
``classification=`` argument and the report picks them up.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

from loguru import logger


@dataclass
class ReadinessItem:
    name: str
    ready: bool
    detail: str = ""
    duration_ms: float = 0.0
    classification: str = "critical"  # "critical" | "background" | "on_demand"

    def to_dict(self) -> Dict[str, object]:
        return {
            "name": self.name,
            "ready": self.ready,
            "detail": self.detail,
            "duration_ms": round(self.duration_ms, 2),
            "classification": self.classification,
        }


@dataclass
class ReadinessReport:
    items: Tuple[ReadinessItem, ...] = ()
    generated_at: float = field(default_factory=time.time)

    @property
    def is_ready(self) -> bool:
        """All CRITICAL items are READY."""
        return all(
            it.ready for it in self.items if it.classification == "critical"
        )

    @property
    def critical(self) -> List[ReadinessItem]:
        return [it for it in self.items if it.classification == "critical"]

    @property
    def background(self) -> List[ReadinessItem]:
        return [it for it in self.items if it.classification == "background"]

    def ready_names(self) -> List[str]:
        return [it.name for it in self.items if it.ready]

    def not_ready_names(self) -> List[str]:
        return [it.name for it in self.items if not it.ready]

    def to_dict(self) -> Dict[str, object]:
        return {
            "is_ready": self.is_ready,
            "critical_ready": sum(
                1 for it in self.items if it.classification == "critical" and it.ready
            ),
            "critical_total": sum(
                1 for it in self.items if it.classification == "critical"
            ),
            "background_ready": sum(
                1 for it in self.items if it.classification == "background" and it.ready
            ),
            "background_total": sum(
                1 for it in self.items if it.classification == "background"
            ),
            "items": [it.to_dict() for it in self.items],
            "generated_at": self.generated_at,
        }

    def pretty(self) -> str:
        lines: List[str] = []
        for it in self.items:
            mark = "OK " if it.ready else "!! "
            kind = it.classification.upper()[:4]
            lines.append(
                f"  {mark} [{kind}] {it.name:<24} {it.duration_ms:7.1f}ms  {it.detail}"
            )
        lines.append("")
        lines.append(
            f"  critical: {sum(1 for it in self.items if it.classification == 'critical' and it.ready)}/"
            f"{sum(1 for it in self.items if it.classification == 'critical')} ready"
        )
        lines.append(
            f"  background: {sum(1 for it in self.items if it.classification == 'background' and it.ready)}/"
            f"{sum(1 for it in self.items if it.classification == 'background')} ready"
        )
        lines.append(f"  overall: {'READY' if self.is_ready else 'NOT READY'}")
        return "\n".join(lines)


class ReadinessGate:
    """Readiness coordinator that builds a :class:`ReadinessReport`
    from the live :class:`ServiceRegistry`.

    Usage::

        gate = ReadinessGate(registry)
        report = gate.report()
        if report.is_ready:
            announcer.announce()
    """

    def __init__(self, registry) -> None:
        self._registry = registry

    def report(self) -> ReadinessReport:
        """Snapshot current readiness from the registry.

        A service is considered READY when it is registered **and**
        its ``initialize()`` returned True.  For convenience, the
        readiness gate also injects a few always-ready synthetic
        items (engine itself) so the report has a stable shape.
        """
        items: List[ReadinessItem] = []
        with self._registry._lock:
            snapshot = list(self._registry._services.items())
        for name, rec in snapshot:
            ready = False
            detail = ""
            duration_ms = 0.0
            try:
                ready = bool(getattr(rec.instance, "initialized", False))
            except Exception:  # noqa: BLE001
                ready = False
            try:
                stats = rec.instance.statistics() or {}
                detail = str(stats.get("lifecycle", "")) or ""
            except Exception:  # noqa: BLE001
                detail = ""
            items.append(
                ReadinessItem(
                    name=name,
                    ready=ready,
                    detail=detail,
                    duration_ms=0.0,
                    classification=rec.classification,
                )
            )
        return ReadinessReport(items=tuple(items))
