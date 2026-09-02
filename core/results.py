"""
Omnix V6 — Structured Result Model.

Defines the data types that flow through the engine when something is
*attempted* (an action, an observation, a verification, a capability
call, a full task).

Design constraints (V6 architecture):
    - R-8: "Do not use vague booleans as the only source of truth."
           Every result carries an *enum-like status*, not a bare bool,
           and distinguishes attempted / executed / verified / failed.
    - R-10: Results are immutable once constructed (``frozen=True``).
            Composition is by ``with_*`` methods returning new instances.
    - AD-21: Capability result must expose all four phases
            (``attempted``, ``executed``, ``verified``, ``failed``)
            so the recovery layer can branch on a precise state.
    - No ``Optional`` for status — every result has one.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Dict, Optional

from .errors import OmnixError


# ---------------------------------------------------------------------------
# Status enums
# ---------------------------------------------------------------------------

class ActionStatus(str, Enum):
    """Phases an action can be in when it returns a result."""

    ATTEMPTED = "attempted"        # dispatch accepted; no side effect yet
    EXECUTED = "executed"          # the action ran on the world
    FAILED = "failed"              # the action raised or returned failure
    TIMED_OUT = "timed_out"        # the action exceeded its deadline
    CANCELLED = "cancelled"        # cancelled before completion
    SKIPPED = "skipped"            # pre-condition refused; nothing ran


class ObservationStatus(str, Enum):
    """Phases a sensor can be in when it returns a result."""

    SUCCESS = "success"            # got a real observation
    EMPTY = "empty"                # looked, found nothing (not an error)
    FAILED = "failed"              # sensor raised or returned unusable data
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


class VerificationStatus(str, Enum):
    """Phases a post-action check can be in when it returns a result."""

    VERIFIED = "verified"          # world matches expected post-condition
    UNVERIFIED = "unverified"      # check ran but produced no verdict
    MISMATCH = "mismatch"          # world does not match expected state
    FAILED = "failed"              # the verification itself crashed
    TIMED_OUT = "timed_out"


class CapabilityStatus(str, Enum):
    """Composite status for a full capability invocation.

    AD-21 requires us to surface all four phases:
        attempted, executed, verified, failed
    This enum is the single label callers route on; the per-phase
    booleans on :class:`CapabilityResult` are the *explanation* of how
    the label was reached.
    """

    ATTEMPTED = "attempted"        # router accepted; pre-conditions ok
    EXECUTED = "executed"          # underlying action ran
    VERIFIED = "verified"          # post-condition matched
    FAILED = "failed"              # any phase failed
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"            # safety / availability refused


class TaskStatus(str, Enum):
    """Phases a user-facing task can be in."""

    CREATED = "created"
    PLANNING = "planning"
    READY = "ready"
    RUNNING = "running"
    WAITING = "waiting"            # blocked on a future event
    VERIFYING = "verifying"
    RECOVERING = "recovering"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


# ---------------------------------------------------------------------------
# ActionResult
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ActionResult:
    """Result of a single side-effecting action.

    Booleans ``executed`` and ``failed`` are convenience accessors that
    mirror :attr:`status` for callers that prefer flag-style checks;
    routing decisions must go through :attr:`status`.
    """

    status: ActionStatus
    action_name: str
    details: Dict[str, Any] = field(default_factory=dict)
    error: Optional[OmnixError] = None
    duration_ms: float = 0.0

    # ------------------------------------------------------- derived flags
    @property
    def executed(self) -> bool:
        return self.status is ActionStatus.EXECUTED

    @property
    def failed(self) -> bool:
        return self.status in (
            ActionStatus.FAILED,
            ActionStatus.TIMED_OUT,
            ActionStatus.CANCELLED,
        )

    # ----------------------------------------------------- immutable update
    def with_status(self, status: ActionStatus) -> "ActionResult":
        return replace(self, status=status)

    def with_error(self, error: OmnixError) -> "ActionResult":
        return replace(self, error=error, status=ActionStatus.FAILED)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": "ActionResult",
            "status": self.status.value,
            "action_name": self.action_name,
            "details": dict(self.details),
            "error": self.error.to_dict() if self.error else None,
            "duration_ms": self.duration_ms,
        }


# ---------------------------------------------------------------------------
# ObservationResult
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ObservationResult:
    """Result of a single sensor read (vision, OCR, screen, etc.)."""

    status: ObservationStatus
    sensor_name: str
    data: Any = None
    details: Dict[str, Any] = field(default_factory=dict)
    error: Optional[OmnixError] = None
    duration_ms: float = 0.0

    @property
    def success(self) -> bool:
        return self.status is ObservationStatus.SUCCESS

    @property
    def failed(self) -> bool:
        return self.status in (
            ObservationStatus.FAILED,
            ObservationStatus.TIMED_OUT,
            ObservationStatus.CANCELLED,
        )

    def with_status(self, status: ObservationStatus) -> "ObservationResult":
        return replace(self, status=status)

    def with_error(self, error: OmnixError) -> "ObservationResult":
        return replace(self, error=error, status=ObservationStatus.FAILED)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": "ObservationResult",
            "status": self.status.value,
            "sensor_name": self.sensor_name,
            "data": self.data,
            "details": dict(self.details),
            "error": self.error.to_dict() if self.error else None,
            "duration_ms": self.duration_ms,
        }


# ---------------------------------------------------------------------------
# VerificationResult
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class VerificationResult:
    """Result of a post-action check.

    ``expected`` and ``actual`` are kept as opaque ``Any`` because the
    verification domain is owned by the capability that ran; we only
    standardize the *shape* of the verdict.
    """

    status: VerificationStatus
    check_name: str
    expected: Any = None
    actual: Any = None
    details: Dict[str, Any] = field(default_factory=dict)
    error: Optional[OmnixError] = None
    duration_ms: float = 0.0

    @property
    def verified(self) -> bool:
        return self.status is VerificationStatus.VERIFIED

    @property
    def failed(self) -> bool:
        return self.status in (
            VerificationStatus.MISMATCH,
            VerificationStatus.FAILED,
            VerificationStatus.TIMED_OUT,
        )

    def with_status(self, status: VerificationStatus) -> "VerificationResult":
        return replace(self, status=status)

    def with_error(self, error: OmnixError) -> "VerificationResult":
        return replace(self, error=error, status=VerificationStatus.FAILED)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": "VerificationResult",
            "status": self.status.value,
            "check_name": self.check_name,
            "expected": self.expected,
            "actual": self.actual,
            "details": dict(self.details),
            "error": self.error.to_dict() if self.error else None,
            "duration_ms": self.duration_ms,
        }


# ---------------------------------------------------------------------------
# CapabilityResult
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CapabilityResult:
    """Composite result of a single capability invocation.

    Carries the four phase flags required by AD-21 plus the resolved
    :class:`CapabilityStatus` so the recovery layer can branch on a
    precise state.  ``action`` and ``verification`` are optional because
    short-circuited capabilities (rejected by the router, for example)
    never produce them.
    """

    capability_name: str
    status: CapabilityStatus
    attempted: bool = False
    executed: bool = False
    verified: bool = False
    failed: bool = False
    action: Optional[ActionResult] = None
    verification: Optional[VerificationResult] = None
    error: Optional[OmnixError] = None
    details: Dict[str, Any] = field(default_factory=dict)
    duration_ms: float = 0.0

    # ---------------------------------------------------- derived helpers
    @property
    def ok(self) -> bool:
        """True iff every phase that ran succeeded."""
        return self.status is CapabilityStatus.VERIFIED

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": "CapabilityResult",
            "capability_name": self.capability_name,
            "status": self.status.value,
            "attempted": self.attempted,
            "executed": self.executed,
            "verified": self.verified,
            "failed": self.failed,
            "action": self.action.to_dict() if self.action else None,
            "verification": self.verification.to_dict() if self.verification else None,
            "error": self.error.to_dict() if self.error else None,
            "details": dict(self.details),
            "duration_ms": self.duration_ms,
        }


# ---------------------------------------------------------------------------
# TaskResult
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TaskResult:
    """Final result of a user-facing task.

    A task is a directed-acyclic graph of capability invocations; the
    ``TaskResult`` collects them in :attr:`capability_results` and
    surfaces the final terminal status.
    """

    task_id: str
    status: TaskStatus
    goal: str
    capability_results: tuple = ()
    error: Optional[OmnixError] = None
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    details: Dict[str, Any] = field(default_factory=dict)

    @property
    def completed(self) -> bool:
        return self.status is TaskStatus.COMPLETED

    @property
    def failed(self) -> bool:
        return self.status is TaskStatus.FAILED

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": "TaskResult",
            "task_id": self.task_id,
            "status": self.status.value,
            "goal": self.goal,
            "capability_results": [r.to_dict() for r in self.capability_results],
            "error": self.error.to_dict() if self.error else None,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "details": dict(self.details),
        }
