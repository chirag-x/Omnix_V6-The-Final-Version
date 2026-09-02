"""
Omnix V6 — Execution Step Model for Stage 19.2.

Defines the ExecutionStep and StepAction models that represent
one intended computer interaction in the PRECONDITION → OBSERVE → GROUND → ACT → VERIFY cycle.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Tuple, Optional, Dict, List

# Import for default factory
from .expectation import VerificationExpectation
from .preconditions import Precondition


class StepAction(str, Enum):
    """Closed set of physical actions the cycle can perform.
    This is NOT the full capability set; it is the small set the
    cycle is allowed to dispatch without going through the planner.
    """
    CLICK = "click"
    DOUBLE_CLICK = "double_click"
    RIGHT_CLICK = "right_click"
    TYPE_TEXT = "type_text"
    PRESS_KEY = "press_key"
    HOTKEY = "hotkey"
    SCROLL = "scroll"
    MOVE = "move"
    DRAG = "drag"
    OPEN_APPLICATION = "open_application"
    FOCUS_WINDOW = "focus_window"
    WAIT = "wait"
    SCREENSHOT = "screenshot"


class RecoveryStrategy(str, Enum):
    """Closed set of recovery strategies that can be applied to a failed step.

    These represent the small, well-understood set of recovery moves
    the cycle can attempt before giving up on a step.
    """
    NONE = "none"
    RETRY = "retry"
    RE_OBSERVE = "re_observe"
    RE_GROUND = "re_ground"
    ADJUST_TIMEOUT = "adjust_timeout"
    FOCUS_WINDOW = "focus_window"
    SCROLL_TO_TARGET = "scroll_to_target"
    WAIT_AND_RETRY = "wait_and_retry"
    ABORT = "abort"


@dataclass(frozen=True)
class RecoveryAttempt:
    """Record of a single recovery attempt for a step.

    Captures what strategy was used, when, what the outcome was, and any
    contextual information that explains why the attempt was taken.
    """
    attempt_id: str = ""
    strategy: RecoveryStrategy = RecoveryStrategy.NONE
    attempt_number: int = 0
    started_at: float = 0.0
    completed_at: float = 0.0
    success: bool = False
    error: str = ""
    reason: str = ""
    context: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Generate attempt_id if not provided
        if not self.attempt_id:
            object.__setattr__(self, 'attempt_id', str(uuid4()))
        # Set started_at to now if not provided
        if self.started_at == 0.0:
            object.__setattr__(self, 'started_at', time.time())
        # Set completed_at to started_at if not provided (instantaneous)
        if self.completed_at == 0.0:
            object.__setattr__(self, 'completed_at', self.started_at)

    @property
    def elapsed_ms(self) -> float:
        """Duration of this recovery attempt in milliseconds."""
        return (self.completed_at - self.started_at) * 1000.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "attempt_id": self.attempt_id,
            "strategy": self.strategy.value,
            "attempt_number": self.attempt_number,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "elapsed_ms": self.elapsed_ms,
            "success": self.success,
            "error": self.error,
            "reason": self.reason,
            "context": dict(self.context),
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class RecoveryContext:
    """Mutable-style context describing the current recovery posture for a step.

    This is the recovery "scratchpad": how many attempts have been made,
    what strategies have been tried, when the last attempt happened, and
    any limits that govern further recovery.
    """
    max_attempts: int = 3
    backoff_base_s: float = 0.5
    backoff_factor: float = 2.0
    backoff_max_s: float = 10.0
    cooldown_s: float = 0.0
    last_attempt_at: float = 0.0
    last_error: str = ""
    last_strategy: RecoveryStrategy = RecoveryStrategy.NONE
    tried_strategies: Tuple[RecoveryStrategy, ...] = field(default_factory=tuple)
    failed_phase: str = ""  # which phase failed last ("grounding" | "action" | "verification" | ...)
    notes: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Coerce tuples to tuple type for frozen safety
        if not isinstance(self.tried_strategies, tuple):
            object.__setattr__(self, 'tried_strategies', tuple(self.tried_strategies))
        if not isinstance(self.notes, Mapping):
            object.__setattr__(self, 'notes', dict(self.notes))

    @property
    def attempt_count(self) -> int:
        """Total number of recovery attempts that have been made."""
        return len(self.tried_strategies)

    @property
    def attempts_remaining(self) -> int:
        """How many more attempts are permitted by the max_attempts budget."""
        return max(0, self.max_attempts - self.attempt_count)

    @property
    def can_recover(self) -> bool:
        """True if the recovery budget is not exhausted."""
        return self.attempt_count < self.max_attempts

    def next_backoff_s(self) -> float:
        """Compute the next exponential backoff delay in seconds."""
        if self.attempt_count <= 0:
            return 0.0
        delay = self.backoff_base_s * (self.backoff_factor ** (self.attempt_count - 1))
        return min(delay, self.backoff_max_s)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "max_attempts": self.max_attempts,
            "attempt_count": self.attempt_count,
            "attempts_remaining": self.attempts_remaining,
            "can_recover": self.can_recover,
            "next_backoff_s": self.next_backoff_s(),
            "backoff_base_s": self.backoff_base_s,
            "backoff_factor": self.backoff_factor,
            "backoff_max_s": self.backoff_max_s,
            "cooldown_s": self.cooldown_s,
            "last_attempt_at": self.last_attempt_at,
            "last_error": self.last_error,
            "last_strategy": self.last_strategy.value,
            "tried_strategies": [s.value for s in self.tried_strategies],
            "failed_phase": self.failed_phase,
            "notes": dict(self.notes),
        }


@dataclass(frozen=True)
class ExecutionStep:
    """One intended computer interaction. Generic — no app-specific
    coordinates, no hard-coded UI elements.
    """
    step_id: str                          # stable id for tracing
    action: StepAction                     # the kind of physical action
    description: str = ""                  # human-readable
    capability_name: str = ""              # resolved capability name (e.g. "desktop.mouse.click")
    parameters: Mapping[str, Any] = field(default_factory=dict)
    target_query: str = ""                 # human-readable (e.g. "search bar"); used for grounding
    target_kind: str = ""                  # "coordinate" | "element" | "ocr" | "vision" | "window"
    target_hint: Mapping[str, Any] | None = None  # optional pre-resolved hints
    expectation: "VerificationExpectation" = field(default_factory=lambda: VerificationExpectation.none())
    timeout_s: float = 30.0
    metadata: Mapping[str, Any] = field(default_factory=dict)
    correlation_id: str = ""
    # Stage 19.2: Preconditions for state-aware execution
    preconditions: tuple["Precondition", ...] = field(default_factory=tuple)
    # Stage 19.4: Recovery tracking and context
    recovery_attempts: Tuple["RecoveryAttempt", ...] = field(default_factory=tuple)
    recovery_context: "RecoveryContext" = field(default_factory=RecoveryContext)

    def __post_init__(self) -> None:
        # Ensure expectation is properly initialized if it's a dataclass
        pass  # Default factory ensures proper initialization
        # Coerce recovery_attempts to tuple (frozen safety)
        if not isinstance(self.recovery_attempts, tuple):
            object.__setattr__(self, 'recovery_attempts', tuple(self.recovery_attempts))
        # Ensure recovery_context is a RecoveryContext instance
        if not isinstance(self.recovery_context, RecoveryContext):
            if isinstance(self.recovery_context, Mapping):
                object.__setattr__(self, 'recovery_context', RecoveryContext(**self.recovery_context))
            else:
                object.__setattr__(self, 'recovery_context', RecoveryContext())

    @property
    def recovery_attempt_count(self) -> int:
        """How many recovery attempts have been recorded for this step."""
        return len(self.recovery_attempts)

    @property
    def last_recovery_attempt(self) -> Optional["RecoveryAttempt"]:
        """The most recent recovery attempt, or None if no recovery has been attempted."""
        if not self.recovery_attempts:
            return None
        return self.recovery_attempts[-1]

    @property
    def has_recovery_history(self) -> bool:
        """True if at least one recovery attempt has been recorded."""
        return len(self.recovery_attempts) > 0

    @property
    def can_still_recover(self) -> bool:
        """True if the step's recovery budget is not yet exhausted."""
        return self.recovery_context.can_recover

    def with_recovery_attempt(self, attempt: "RecoveryAttempt") -> "ExecutionStep":
        """Return a new ExecutionStep with the given recovery attempt appended.

        The new step's recovery_context is updated to reflect the attempt:
        the strategy is added to tried_strategies, last_error / last_strategy
        / last_attempt_at are refreshed, and attempt_count is bumped.

        Args:
            attempt: The RecoveryAttempt to record.

        Returns:
            A new ExecutionStep with the attempt recorded and context updated.
        """
        new_attempts = self.recovery_attempts + (attempt,)
        new_tried = self.recovery_context.tried_strategies + (attempt.strategy,)
        new_ctx = RecoveryContext(
            max_attempts=self.recovery_context.max_attempts,
            backoff_base_s=self.recovery_context.backoff_base_s,
            backoff_factor=self.recovery_context.backoff_factor,
            backoff_max_s=self.recovery_context.backoff_max_s,
            cooldown_s=self.recovery_context.cooldown_s,
            last_attempt_at=attempt.completed_at or attempt.started_at,
            last_error=attempt.error,
            last_strategy=attempt.strategy,
            tried_strategies=new_tried,
            failed_phase=self.recovery_context.failed_phase,
            notes=self.recovery_context.notes,
        )
        return ExecutionStep(
            step_id=self.step_id,
            action=self.action,
            description=self.description,
            capability_name=self.capability_name,
            parameters=self.parameters,
            target_query=self.target_query,
            target_kind=self.target_kind,
            target_hint=self.target_hint,
            expectation=self.expectation,
            timeout_s=self.timeout_s,
            metadata=self.metadata,
            correlation_id=self.correlation_id,
            preconditions=self.preconditions,
            recovery_attempts=new_attempts,
            recovery_context=new_ctx,
        )

    def with_recovery_context(self, **updates: Any) -> "ExecutionStep":
        """Return a new ExecutionStep with the recovery_context updated.

        Pass keyword arguments whose names match RecoveryContext fields.
        Unspecified fields are preserved.

        Args:
            **updates: Field names and new values for RecoveryContext.

        Returns:
            A new ExecutionStep with the updated recovery_context.
        """
        current = self.recovery_context
        new_ctx = RecoveryContext(
            max_attempts=updates.get("max_attempts", current.max_attempts),
            backoff_base_s=updates.get("backoff_base_s", current.backoff_base_s),
            backoff_factor=updates.get("backoff_factor", current.backoff_factor),
            backoff_max_s=updates.get("backoff_max_s", current.backoff_max_s),
            cooldown_s=updates.get("cooldown_s", current.cooldown_s),
            last_attempt_at=updates.get("last_attempt_at", current.last_attempt_at),
            last_error=updates.get("last_error", current.last_error),
            last_strategy=updates.get("last_strategy", current.last_strategy),
            tried_strategies=updates.get("tried_strategies", current.tried_strategies),
            failed_phase=updates.get("failed_phase", current.failed_phase),
            notes=updates.get("notes", current.notes),
        )
        return ExecutionStep(
            step_id=self.step_id,
            action=self.action,
            description=self.description,
            capability_name=self.capability_name,
            parameters=self.parameters,
            target_query=self.target_query,
            target_kind=self.target_kind,
            target_hint=self.target_hint,
            expectation=self.expectation,
            timeout_s=self.timeout_s,
            metadata=self.metadata,
            correlation_id=self.correlation_id,
            preconditions=self.preconditions,
            recovery_attempts=self.recovery_attempts,
            recovery_context=new_ctx,
        )

    def to_dict(self) -> Dict[str, Any]:
        """Convert to a dictionary for serialization/logging."""
        return {
            "step_id": self.step_id,
            "action": self.action.value,
            "description": self.description,
            "capability_name": self.capability_name,
            "parameters": dict(self.parameters),
            "target_query": self.target_query,
            "target_kind": self.target_kind,
            "target_hint": dict(self.target_hint) if self.target_hint else None,
            "expectation": self.expectation.to_dict() if hasattr(self.expectation, 'to_dict') else None,
            "timeout_s": self.timeout_s,
            "metadata": dict(self.metadata),
            "correlation_id": self.correlation_id,
            "preconditions": [p.to_dict() if hasattr(p, 'to_dict') else str(p) for p in self.preconditions],
            # Stage 19.4: Recovery tracking
            "recovery_attempts": [a.to_dict() for a in self.recovery_attempts],
            "recovery_context": self.recovery_context.to_dict(),
            "recovery_attempt_count": self.recovery_attempt_count,
            "can_still_recover": self.can_still_recover,
        }


# Late import to avoid circular dependencies
from uuid import uuid4  # noqa: E402
