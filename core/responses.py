"""
Omnix V6 — Canonical response model (Phase 11).

Phase 11 introduces ONE canonical high-level entry point —
:func:`OmnixEngine.process` — that runs the full request pipeline and
returns a single :class:`OmnixResponse` shape that every consumer
(CLI, voice loop, future GUI, future API) can rely on.

This module is intentionally tiny.  The structured fields are
introspection for tests and debug surfaces; the ``text`` field is the
*only* thing the user (or TTS) is ever shown.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class ResponseStatus(str, Enum):
    """The terminal status of one request through the canonical pipeline.

    Mirrors the :class:`AgentState` outer states but is intentionally
    a smaller set: a user-facing response only needs to know whether
    the request succeeded, was clarified, or was rejected.
    """

    OK = "ok"                              # goal achieved and verified
    CLARIFICATION = "clarification"        # cannot proceed without user input
    FAILED = "failed"                      # unrecoverable failure
    CANCELLED = "cancelled"                # user/system cancelled
    TIMEOUT = "timeout"                    # bounded runtime exceeded
    REJECTED = "rejected"                  # safety gate refused (e.g. dangerous cap)


@dataclass(frozen=True)
class OmnixResponse:
    """The canonical response of one :func:`OmnixEngine.process` call.

    Fields
    ------

    text:
        The user-facing message.  This is the *only* field a non-debug
        consumer should render or speak.  Always non-empty (defaults to
        a sensible human sentence when the pipeline did not produce one).

    status:
        The :class:`ResponseStatus` of the request.

    agent_state:
        The terminal :class:`core.orchestration.AgentState` that
        drove this response.  ``None`` if the pipeline short-circuited
        before the Agent (e.g. unknown intent).

    correlation_id:
        A stable identifier for this single request.  Propagated to
        every structured event the pipeline emits.  Empty string if
        the pipeline never produced one.

    duration_ms:
        Wall-clock duration of the entire pipeline run, in
        milliseconds.

    metadata:
        Structured details for debugging and observability.  Carries
        ``intent_id``, ``goal_id``, ``plan_id``, ``agent_run_id``,
        ``step_count``, ``observation_count``, ``decision_count``
        and similar counters.  NEVER include secrets, raw audio,
        or sensitive memory content here.

    error:
        Short, safe error description when ``status`` is not OK.  Never
        carries the raw exception traceback.  ``None`` for OK/clarification.
    """

    text: str
    status: ResponseStatus
    agent_state: Optional[str] = None
    correlation_id: str = ""
    duration_ms: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None

    # ----------------------------------------------------- projections
    def to_dict(self) -> Dict[str, Any]:
        return {
            "text": self.text,
            "status": self.status.value,
            "agent_state": self.agent_state,
            "correlation_id": self.correlation_id,
            "duration_ms": self.duration_ms,
            "metadata": dict(self.metadata),
            "error": self.error,
        }

    @property
    def ok(self) -> bool:
        return self.status is ResponseStatus.OK


# ----------------------------------------------------------- helpers

def safe_default_text(status: ResponseStatus) -> str:
    """Return a human sentence for a non-OK status.

    Used by the pipeline when the Agent did not produce a final
    message.  Kept here so the strings are testable in isolation
    and not duplicated across the engine, voice service, and CLI.
    """
    if status is ResponseStatus.OK:
        return "Done."
    if status is ResponseStatus.CLARIFICATION:
        return "I need a little more information to help with that."
    if status is ResponseStatus.TIMEOUT:
        return "I ran out of time working on that. Please try again."
    if status is ResponseStatus.CANCELLED:
        return "The request was cancelled."
    if status is ResponseStatus.REJECTED:
        return "I cannot do that — the action was blocked by a safety policy."
    return "I could not complete that request."


def new_correlation_id() -> str:
    """Generate a request correlation id.

    Returns a 16-character hex string suitable for the ``correlation_id``
    field.  Used by :class:`core.pipeline.RequestPipeline` and emitted
    on every observability event the pipeline produces.
    """
    import uuid
    return uuid.uuid4().hex[:16]


__all__ = [
    "OmnixResponse",
    "ResponseStatus",
    "safe_default_text",
    "new_correlation_id",
]
