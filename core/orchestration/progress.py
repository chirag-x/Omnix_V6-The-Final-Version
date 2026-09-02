"""
Omnix V6 — System 8: structured multi-step progress events.

This module defines :class:`ProgressEvent` — a typed, structured
event record emitted by the Agent Orchestrator at every step
transition.  It complements the :class:`StepTraceEntry` trace in
``agent_result`` (which is *internal* audit data) by providing a
narrow, *forward-facing* shape that observability layers (CLI
spinner, TTS nudges, the voice subsystem, the future debug panel)
can consume without knowing about :class:`AgentState` /
:class:`AgentResult` internals.

Architectural rules honored here:

- R-8   — every status is a typed enum, never a bare bool.
- R-10  — events are ``frozen=True``; mutation is by ``with_*``.
- R-12  — the broadcaster is a Protocol so the Agent can be
          replaced without rewiring the event surface.
- R-17  — loguru only.
- R-21  — the broadcaster is *passive*: it never calls a
          Capability.  It is a pure observer.
- R-23  — it never mutates :class:`AgentResult`; it only emits.
- R-24  — events are typed data, not user-facing strings.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Mapping, Optional, Protocol, runtime_checkable


# ===========================================================================
# ProgressPhase — every step transition the Agent emits
# ===========================================================================

class ProgressPhase(str, Enum):
    """The phase a progress event is reporting.

    Phases correspond to the Agent's outer state-machine transitions,
    but are *narrower* (one progress event per *visible* transition;
    transient internal hops like ``EVALUATING`` collapse into
    ``step_verified``/``step_failed``/``decision``).
    """

    # Plan-level
    PLAN_STARTED = "plan_started"               # the Agent entered PLANNING / EXECUTING
    PLAN_COMPLETED = "plan_completed"           # every step in the plan ran

    # Step-level
    STEP_DISPATCHED = "step_dispatched"         # about to invoke a Capability
    STEP_OBSERVED = "step_observed"             # observation captured
    STEP_VERIFIED = "step_verified"             # verifier returned PASSED
    STEP_FAILED = "step_failed"                 # verifier returned FAILED/UNCERTAIN
    STEP_RETRIED = "step_retried"               # recovery action = RETRY
    STEP_SKIPPED = "step_skipped"               # recovery action = SKIP
    STEP_REPLANNED = "step_replanned"           # recovery action = REPLAN

    # Recovery / Replan
    RECOVERY_DECISION = "recovery_decision"     # a RecoveryDecision was produced
    REPLAN_STARTED = "replan_started"           # a new Plan v(N+1) was produced
    REPLAN_COMPLETED = "replan_completed"       # the new plan was admitted

    # Multi-step coordination
    PRECONDITION_EVALUATED = "precondition_evaluated"
    POSTCONDITION_EVALUATED = "postcondition_evaluated"
    IDEMPOTENCY_CHECKED = "idempotency_checked"
    REGROUND_TRIGGERED = "reground_triggered"

    # Agent terminal
    AGENT_COMPLETE = "agent_complete"
    AGENT_FAILED = "agent_failed"
    AGENT_CANCELLED = "agent_cancelled"
    AGENT_TIMEOUT = "agent_timeout"
    AGENT_CLARIFICATION = "agent_clarification"

    # Catch-all
    INFO = "info"                               # anything else worth a single line


# Terminal phases — the Agent's run has ended.
_TERMINAL_PROGRESS_PHASES = frozenset({
    ProgressPhase.AGENT_COMPLETE,
    ProgressPhase.AGENT_FAILED,
    ProgressPhase.AGENT_CANCELLED,
    ProgressPhase.AGENT_TIMEOUT,
    ProgressPhase.AGENT_CLARIFICATION,
})


def is_terminal_progress_phase(phase: ProgressPhase) -> bool:
    return phase in _TERMINAL_PROGRESS_PHASES


# ===========================================================================
# ProgressEvent — one structured record
# ===========================================================================

@dataclass(frozen=True)
class ProgressEvent:
    """A single structured progress event.

    Fields
    ------
    event_id:
        Unique id for the event (UUID4 short).
    phase:
        The :class:`ProgressPhase` this event reports.
    plan_id:
        Id of the active :class:`Plan` (``""`` before the first plan).
    step_id:
        Id of the :class:`PlanStep` this event pertains to (``""``
        for plan-level events).
    attempt:
        1-based attempt index for the current step (``0`` for plan-level).
    correlation_id:
        Correlation id propagated from the request pipeline (``""``
        if the Agent is invoked outside the pipeline).
    timestamp:
        Wall-clock seconds when the event was emitted.
    message:
        Short human-readable line; safe to log.
    details:
        Free-form structured fields (e.g. verifier verdict, recovery
        action, idempotency key, pre-condition reasons).  Safe to log.
    """

    event_id: str
    phase: ProgressPhase
    plan_id: str = ""
    step_id: str = ""
    attempt: int = 0
    correlation_id: str = ""
    timestamp: float = 0.0
    message: str = ""
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": "ProgressEvent",
            "event_id": self.event_id,
            "phase": self.phase.value,
            "plan_id": self.plan_id,
            "step_id": self.step_id,
            "attempt": self.attempt,
            "correlation_id": self.correlation_id,
            "timestamp": self.timestamp,
            "message": self.message,
            "details": dict(self.details),
        }


def new_progress_event_id() -> str:
    """Return a short, unique progress-event id."""
    return f"pe-{uuid.uuid4().hex[:12]}"


def make_progress_event(
    phase: ProgressPhase,
    *,
    plan_id: str = "",
    step_id: str = "",
    attempt: int = 0,
    correlation_id: str = "",
    message: str = "",
    details: Optional[Mapping[str, Any]] = None,
    timestamp: Optional[float] = None,
) -> ProgressEvent:
    """Construct a :class:`ProgressEvent` with a fresh id and timestamp."""
    return ProgressEvent(
        event_id=new_progress_event_id(),
        phase=phase,
        plan_id=plan_id,
        step_id=step_id,
        attempt=attempt,
        correlation_id=correlation_id,
        timestamp=timestamp if timestamp is not None else time.time(),
        message=message,
        details=dict(details) if isinstance(details, Mapping) else {},
    )


# ===========================================================================
# ProgressBroadcaster — the seam
# ===========================================================================

@runtime_checkable
class ProgressBroadcaster(Protocol):
    """The seam through which the Agent emits :class:`ProgressEvent`s.

    The default production implementation forwards to a structured
    logger (loguru) and, when wired, to the existing event bus under
    the ``RequestEvent`` stream.  Tests pass an in-memory recorder.

    Implementations MUST be:

      * *thread-safe* — the Agent runs synchronously today, but
        future async executors may interleave.
      * *fail-soft*  — an exception from a listener must NEVER
        propagate out of ``publish``; the Agent run is more
        important than the observability stream.
    """

    def publish(self, event: ProgressEvent) -> None:
        ...


# ===========================================================================
# In-memory recorder (the default for unit tests)
# ===========================================================================

class InMemoryProgressBroadcaster:
    """A trivial :class:`ProgressBroadcaster` that records events.

    The Agent uses this when no broadcaster is wired (unit tests,
    headless mode, ...).  Production code passes a real
    :class:`LogProgressBroadcaster` or, when the event bus is up,
    a :class:`BusProgressBroadcaster`.
    """

    def __init__(self) -> None:
        self._events: list = []

    def publish(self, event: ProgressEvent) -> None:
        # ``frozen=True`` dataclasses can be appended by value.
        self._events.append(event)

    def events(self) -> list:
        """Return a copy of the recorded events list."""
        return list(self._events)

    def clear(self) -> None:
        self._events = []

    def of_phase(self, phase: ProgressPhase) -> list:
        return [e for e in self._events if e.phase is phase]

    def count(self, phase: ProgressPhase) -> int:
        return sum(1 for e in self._events if e.phase is phase)


# ===========================================================================
# Log progress broadcaster (production default)
# ===========================================================================

class LogProgressBroadcaster:
    """A :class:`ProgressBroadcaster` that emits via loguru.

    The default loguru logger is used; no other logging stack is
    touched (R-17).
    """

    def __init__(self, logger_name: str = "omnix.progress") -> None:
        # ``loguru`` does not expose a true "named logger" API
        # outside its ``logger.bind`` context, so we bind a single
        # channel to a marker.  This satisfies R-17 ("loguru only")
        # without inventing a parallel logging system.
        from loguru import logger as _loguru
        self._log = _loguru.bind(subsystem=logger_name)

    def publish(self, event: ProgressEvent) -> None:
        try:
            self._log.info(event.message or event.phase.value, **event.to_dict())
        except Exception:  # noqa: BLE001
            # Observability must never break the Agent.
            pass


# ===========================================================================
# Composite broadcaster (fan-out)
# ===========================================================================

class CompositeProgressBroadcaster:
    """A :class:`ProgressBroadcaster` that fans out to several children.

    Each child runs in a ``try/except`` so a broken listener cannot
    affect the Agent or the other listeners (fail-soft contract).
    """

    def __init__(self, *children: ProgressBroadcaster) -> None:
        self._children: list = list(children)

    def add(self, child: ProgressBroadcaster) -> None:
        self._children.append(child)

    def publish(self, event: ProgressEvent) -> None:
        for child in self._children:
            try:
                child.publish(event)
            except Exception:  # noqa: BLE001
                # Fail-soft — a broken listener must not affect the
                # Agent or the other listeners.
                continue


__all__ = [
    "ProgressPhase",
    "ProgressEvent",
    "ProgressBroadcaster",
    "InMemoryProgressBroadcaster",
    "LogProgressBroadcaster",
    "CompositeProgressBroadcaster",
    "make_progress_event",
    "new_progress_event_id",
    "is_terminal_progress_phase",
]
