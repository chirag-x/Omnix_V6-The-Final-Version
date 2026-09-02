"""
Omnix V6 — Event types (R-11).

Defines the typed, frozen event dataclasses that flow through the
:class:`EventBus`.  Events are the *only* integration seam between
subsystems (R-11): direct cross-subsystem method calls are forbidden
outside the engine itself.

Hierarchy:

    Event (frozen dataclass)
    ├── EngineEvent          — engine lifecycle (started, ready, stopped)
    ├── ServiceEvent         — service registry (registered, initialized,
    │                          failed, shutdown)
    ├── CapabilityEvent      — capability router (attempted, executed,
    │                          verified, failed, rejected)
    ├── TaskEvent            — task lifecycle (created, planned, started,
    │                          step, completed, failed, cancelled)
    ├── WorldEvent           — world state changes (window, app, screen)
    ├── ConversationEvent    — user / engine turns
    ├── ErrorEvent           — any subsystem raised a recoverable error
    └── HealthEvent          — health changes (degraded, restored)

Events are *facts about the world*, not commands.  They carry a
``timestamp`` and (optionally) the ``source`` subsystem that emitted
them.  They never carry callbacks or references to mutable state.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Event:
    """Base event.  Every concrete event subclasses this.

    ``source`` is a free-form name of the emitting subsystem
    (``"engine"``, ``"capability_router"``, …) so subscribers can
    filter without parsing the event class.
    """

    timestamp: float = field(default_factory=time.time)
    source: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    # Every event has a stable string name; the bus uses this for
    # wildcard subscriptions (``"capability.*"``).  Subclasses MUST
    # override.
    name: str = "event"


# ---------------------------------------------------------------------------
# Engine lifecycle
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EngineEvent(Event):
    """Engine lifecycle transition."""

    name: str = "engine.event"
    transition: str = ""  # "booted" | "ready" | "running" | "stopping" | "stopped"


# ---------------------------------------------------------------------------
# Service registry
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ServiceEvent(Event):
    """Service added / initialized / failed / shut down."""

    name: str = "service.event"
    service_name: str = ""
    transition: str = ""  # "registered" | "initialized" | "failed" | "shutdown"


# ---------------------------------------------------------------------------
# Capability router
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CapabilityEvent(Event):
    """A capability was attempted, executed, verified, failed, or rejected."""

    name: str = "capability.event"
    capability: str = ""
    transition: str = ""  # "attempted" | "executed" | "verified" | "failed" | "rejected"
    duration_ms: float = 0.0
    error: Optional[str] = None  # string repr; structured errors go through payload


# ---------------------------------------------------------------------------
# Task lifecycle
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TaskEvent(Event):
    """A user-facing task transitioned."""

    name: str = "task.event"
    task_id: str = ""
    transition: str = ""  # "created" | "planned" | "started" | "step_started"
    #                   | "step_completed" | "step_failed"
    #                   | "completed" | "failed" | "cancelled" | "replanning"
    step_index: int = -1
    total_steps: int = 0


# ---------------------------------------------------------------------------
# World state changes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class WorldEvent(Event):
    """The world changed in a way the engine wants to log."""

    name: str = "world.event"
    transition: str = ""  # "window_changed" | "app_changed" | "screen_captured"
    subject: str = ""     # title of the window / app name / sensor name


# ---------------------------------------------------------------------------
# Conversation
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ConversationEvent(Event):
    """A user or assistant turn was appended to the conversation buffer."""

    name: str = "conversation.event"
    role: str = ""        # "user" | "assistant" | "system"
    session_id: str = ""
    turn_length: int = 0  # bytes; do not log full content (may contain secrets)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ErrorEvent(Event):
    """A subsystem raised a recoverable error worth logging."""

    name: str = "error.event"
    subsystem: str = ""
    code: str = ""
    message: str = ""
    recoverable: bool = True


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class HealthEvent(Event):
    """A subsystem changed health state."""

    name: str = "health.event"
    subsystem: str = ""
    transition: str = ""  # "degraded" | "restored" | "unhealthy"
    detail: str = ""


# ---------------------------------------------------------------------------
# Request lifecycle (Phase 11)
# ---------------------------------------------------------------------------

# Canonical pipeline stages emitted on the bus.  Consumers can
# subscribe to ``request.received`` / ``request.completed`` etc. without
# caring which subsystem produced the event.
REQUEST_RECEIVED = "received"          # engine accepted the user text
REQUEST_INTENT_RESOLVED = "intent_resolved"  # Brain classified the input
REQUEST_PLAN_CREATED = "plan_created"        # Brain produced a Plan
REQUEST_EXECUTION_STARTED = "execution_started"
REQUEST_ACTION_EXECUTED = "action_executed"
REQUEST_OBSERVATION_CAPTURED = "observation_captured"
REQUEST_VERIFICATION_COMPLETED = "verification_completed"
REQUEST_RECOVERY_STARTED = "recovery_started"
REQUEST_REPLAN_STARTED = "replan_started"
REQUEST_COMPLETED = "completed"        # terminal: ok | clarification | failed | …
REQUEST_CANCELLED = "cancelled"
REQUEST_TIMED_OUT = "timed_out"
REQUEST_REJECTED = "rejected"          # safety gate refused


@dataclass(frozen=True)
class RequestEvent(Event):
    """A user request progressed through one pipeline stage.

    ``stage`` is one of the ``REQUEST_*`` constants above.  ``correlation_id``
    ties together every event for a single request and matches the
    :class:`OmnixResponse.correlation_id` returned to the caller.
    """

    name: str = "request.event"
    correlation_id: str = ""
    stage: str = ""
    request_kind: str = ""  # "text" | "voice"
    status: str = ""        # terminal: "ok" | "clarification" | "failed" | "cancelled" | "timeout" | "rejected"
    error: str = ""
    duration_ms: float = 0.0
    intent_kind: str = ""
    plan_id: str = ""
    plan_step_count: int = 0
    agent_run_id: str = ""


# ---------------------------------------------------------------------------
# Agent observability (Phase 1 / D5 + Phase 6)
# ---------------------------------------------------------------------------

# The Agent's :meth:`_emit` publishes these "kind" strings as
# free-text events.  When an :class:`AgentEvent` is emitted on the
# bus, ``event_kind`` is one of these.
AGENT_EVENT_KIND_AGENT_STARTED = "agent_started"
AGENT_EVENT_KIND_PLANNING = "planning"
AGENT_EVENT_KIND_PLAN_READY = "plan_ready"
AGENT_EVENT_KIND_EXECUTING = "executing"
AGENT_EVENT_KIND_OBSERVING = "observing"
AGENT_EVENT_KIND_EVALUATING = "evaluating"
AGENT_EVENT_KIND_RETRY = "retry"
AGENT_EVENT_KIND_REPLAN = "replan"
AGENT_EVENT_KIND_AGENT_STATE_TRANSITION = "agent_state_transition"
AGENT_EVENT_KIND_AGENT_FINISHED = "agent_finished"
AGENT_EVENT_KIND_PLAN_REFUSED = "plan_refused"


@dataclass(frozen=True)
class AgentEvent(Event):
    """A structured observability event from the Agent Orchestrator.

    The :class:`core.orchestration.Agent` already emits free-text
    events through its ``observability_sink`` callable.  In
    Phase 1 (D5) + Phase 6 we translate those calls into
    :class:`AgentEvent` envelopes on the bus so that:

      * voice / TTS consumers can subscribe to the Agent's
        lifecycle without parsing the free-text payload, and
      * the audit log and progress dashboard can correlate
        Agent events with the request's ``correlation_id``.

    ``event_kind`` is one of the ``AGENT_EVENT_KIND_*`` constants
    above.  ``payload`` carries the original free-text kwargs
    the Agent emitted (plan_id, step_id, attempt, etc.).
    """

    name: str = "agent.event"
    event_kind: str = ""
    correlation_id: str = ""
    plan_id: str = ""
    step_id: str = ""
    agent_run_id: str = ""
    final_state: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_event(event_class: type, **kwargs: Any) -> Event:
    """Construct an event with sensible defaults (timestamp, source).

    Subsystems are expected to call this rather than the raw
    constructor so the timestamp is always stamped.
    """
    kwargs.setdefault("timestamp", time.time())
    return event_class(**kwargs)
