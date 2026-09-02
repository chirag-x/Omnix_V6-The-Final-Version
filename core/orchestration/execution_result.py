"""
Omnix V6 — Plan-execution result contracts (Phase 6A+6B).

This module defines the *result side* of the PlanExecutor contract:

    * :class:`StepState`        — the per-step state machine
    * :class:`StepResult`       — the per-step outcome
    * :class:`ExecutionResult`  — the plan-level outcome

These are the data shapes the executor returns to the orchestrator.
They are deliberately separate from :mod:`core.results` (which holds
the *capability-side* results) so the boundary between "what the
capability did" and "what the executor did *to* the plan" stays crisp.

Architectural rules honored here:

- R-8   — every result carries a typed status enum, never a bare bool.
- R-10  — results are ``frozen=True``; mutation is by ``with_*``.
- R-13  — the executor never invents capability names; it routes
          through the canonical registry.
- R-21  — ActionRequest is the only path to the router; the executor
          constructs one per dispatched step.
- AD-21 — the four phase flags surface through the CapabilityResult
          that the executor embeds in each :class:`StepResult`.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from .models import ActionRequest


# ===========================================================================
# Step state machine
# ===========================================================================

class StepState(str, Enum):
    """Per-step lifecycle managed by the PlanExecutor.

    State transitions::

        PENDING ──► READY ──► RUNNING ──► SUCCEEDED
                      │           │
                      │           ├──► FAILED
                      │           ├──► TIMED_OUT
                      │           ├──► CANCELLED
                      │           └──► BLOCKED
                      │
                      └─────────────► SKIPPED (precondition refused)

    Notes:
      * ``BLOCKED`` is distinct from ``READY``: a step is READY when
        all its dependencies have completed and it is eligible to
        run; it is BLOCKED when at least one dependency failed and
        the step cannot honour its contract without the upstream
        effect.
      * ``SKIPPED`` is a *terminal* state used when a precondition
        (e.g. capability is dangerous and confirmation was refused)
        refuses the step before dispatch.  It is NOT the same as
        ``FAILED`` — the underlying capability was never invoked.
    """

    PENDING = "pending"        # initial; not yet eligible
    READY = "ready"            # all dependencies satisfied
    RUNNING = "running"        # dispatched to the router
    SUCCEEDED = "succeeded"    # capability returned VERIFIED
    FAILED = "failed"          # capability returned FAILED
    TIMED_OUT = "timed_out"    # step exceeded its timeout
    CANCELLED = "cancelled"    # step was cancelled mid-flight
    SKIPPED = "skipped"        # refused before dispatch
    BLOCKED = "blocked"        # a dependency failed and the step cannot run


# Terminal states for the step state machine.
_TERMINAL_STEP_STATES = frozenset({
    StepState.SUCCEEDED,
    StepState.FAILED,
    StepState.TIMED_OUT,
    StepState.CANCELLED,
    StepState.SKIPPED,
})


def _is_terminal_step_state(state: StepState) -> bool:
    return state in _TERMINAL_STEP_STATES


# ===========================================================================
# StepResult
# ===========================================================================

@dataclass(frozen=True)
class StepResult:
    """The outcome of executing a single :class:`PlanStep`.

    The executor returns one of these per step it actually attempts.
    Steps that are blocked by an upstream failure still receive a
    ``StepResult`` with ``status=StepState.BLOCKED`` so the caller
    can audit the full plan.

    Fields
    ------
    step_id:
        Mirrors :attr:`PlanStep.step_id`.
    capability_name:
        The capability the step was routed against (or ``""`` for
        non-INVOKE steps, or steps that were skipped before
        dispatch).
    status:
        The terminal state from :class:`StepState`.
    capability_result:
        The :class:`core.results.CapabilityResult` from the router
        (``None`` when the step did not dispatch).
    action_request:
        The :class:`ActionRequest` the executor constructed
        (``None`` when the step did not dispatch — e.g. SKIPPED or
        BLOCKED).
    started_at / completed_at:
        Wall-clock seconds.  ``completed_at`` is ``None`` while the
        step is still in flight; tests should compare it to
        ``time.time()`` after the executor returns.
    duration_ms:
        Convenience field cached at construction time.
    error:
        A short error code + message for the audit log.  The
        original exception (if any) lives in the embedded
        ``CapabilityResult.error`` so we don't double-encode it.
    attempt:
        1-based attempt index (1 = first attempt; incremented on
        retry).  Phase 6A does not implement retries — this is the
        hook the future recovery layer will use.
    metadata:
        Free-form structured fields for the audit log.
    """

    step_id: str
    capability_name: str
    status: StepState
    capability_result: Any = None       # core.results.CapabilityResult
    action_request: Optional[ActionRequest] = None
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    duration_ms: float = 0.0
    error: str = ""
    attempt: int = 1
    metadata: Dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------- derived
    @property
    def ok(self) -> bool:
        """True iff the step reached SUCCEEDED."""
        return self.status is StepState.SUCCEEDED

    @property
    def is_terminal(self) -> bool:
        return _is_terminal_step_state(self.status)

    # ----------------------------------------------------- with_* updates
    def with_status(
        self,
        status: StepState,
        *,
        completed_at: Optional[float] = None,
    ) -> "StepResult":
        new = replace(self, status=status)
        if completed_at is not None and self.started_at is not None:
            new = replace(
                new,
                completed_at=completed_at,
                duration_ms=max(0.0, (completed_at - self.started_at) * 1000.0),
            )
        elif completed_at is not None:
            new = replace(new, completed_at=completed_at)
        return new

    def with_capability_result(self, capability_result: Any) -> "StepResult":
        return replace(self, capability_result=capability_result)

    def with_action_request(self, action_request: ActionRequest) -> "StepResult":
        return replace(self, action_request=action_request)

    def with_error(self, error: str) -> "StepResult":
        return replace(self, error=error)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": "StepResult",
            "step_id": self.step_id,
            "capability_name": self.capability_name,
            "status": self.status.value,
            "attempt": self.attempt,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "duration_ms": self.duration_ms,
            "error": self.error,
            "capability_result": (
                self.capability_result.to_dict()
                if self.capability_result is not None
                else None
            ),
            "action_request": (
                self.action_request.to_dict() if self.action_request else None
            ),
            "metadata": dict(self.metadata),
        }


# ===========================================================================
# ExecutionResult
# ===========================================================================

class ExecutionOutcome(str, Enum):
    """The terminal outcome of a plan execution.

    Distinct from :class:`core.results.TaskStatus` (which is the
    user-facing task lifecycle): ``ExecutionOutcome`` is the
    *executor-side* summary used by the orchestrator to decide what
    to do next.
    """

    COMPLETED = "completed"        # every step reached SUCCEEDED
    PARTIAL = "partial"            # some non-essential steps skipped
    FAILED = "failed"              # at least one essential step failed
    CANCELLED = "cancelled"        # the executor was cancelled
    BLOCKED = "blocked"            # downstream steps were blocked
    TIMED_OUT = "timed_out"        # the plan-level budget expired


@dataclass(frozen=True)
class ExecutionResult:
    """The executor's return value for a single plan run.

    Fields
    ------
    execution_id:
        Unique id (mirrors :attr:`ExecutionContext.execution_id`).
    plan_id:
        The plan that was executed.
    goal_id:
        The goal the plan was produced against.
    outcome:
        Terminal outcome from :class:`ExecutionOutcome`.
    step_results:
        One :class:`StepResult` per step, in plan order.  A blocked
        step is still included with ``status=BLOCKED`` so the audit
        log is complete.
    started_at / completed_at:
        Wall-clock seconds.
    duration_ms:
        Total wall-clock duration.
    correlation_id:
        Stable id propagated into every :class:`ActionRequest`
        the executor produced.  Useful for cross-system tracing.
    error:
        Plan-level error message (e.g. "step foo failed: bar"); the
        per-step error lives on :attr:`StepResult.error`.
    metadata:
        Free-form structured fields.
    """

    execution_id: str
    plan_id: str
    goal_id: str
    outcome: ExecutionOutcome
    step_results: Tuple[StepResult, ...] = ()
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    duration_ms: float = 0.0
    correlation_id: str = ""
    error: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------- derived
    @property
    def completed(self) -> bool:
        return self.outcome is ExecutionOutcome.COMPLETED

    @property
    def step_count(self) -> int:
        return len(self.step_results)

    @property
    def succeeded_step_count(self) -> int:
        return sum(1 for r in self.step_results if r.ok)

    @property
    def failed_step_count(self) -> int:
        return sum(
            1 for r in self.step_results
            if r.status in (
                StepState.FAILED,
                StepState.TIMED_OUT,
                StepState.CANCELLED,
            )
        )

    @property
    def skipped_step_count(self) -> int:
        return sum(
            1 for r in self.step_results
            if r.status in (StepState.SKIPPED, StepState.BLOCKED)
        )

    def find_step_result(self, step_id: str) -> Optional[StepResult]:
        for r in self.step_results:
            if r.step_id == step_id:
                return r
        return None

    # ----------------------------------------------------- with_* updates
    def with_outcome(
        self,
        outcome: ExecutionOutcome,
        *,
        completed_at: Optional[float] = None,
        error: str = "",
    ) -> "ExecutionResult":
        new = replace(self, outcome=outcome, error=error or self.error)
        if completed_at is not None and self.started_at is not None:
            new = replace(
                new,
                completed_at=completed_at,
                duration_ms=max(0.0, (completed_at - self.started_at) * 1000.0),
            )
        elif completed_at is not None:
            new = replace(new, completed_at=completed_at)
        return new

    def with_step_result(self, result: StepResult) -> "ExecutionResult":
        new_list: List[StepResult] = list(self.step_results)
        replaced = False
        for i, existing in enumerate(new_list):
            if existing.step_id == result.step_id:
                new_list[i] = result
                replaced = True
                break
        if not replaced:
            new_list.append(result)
        return replace(self, step_results=tuple(new_list))

    def with_metadata(self, **extra: Any) -> "ExecutionResult":
        merged = dict(self.metadata)
        merged.update(extra)
        return replace(self, metadata=merged)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": "ExecutionResult",
            "execution_id": self.execution_id,
            "plan_id": self.plan_id,
            "goal_id": self.goal_id,
            "outcome": self.outcome.value,
            "step_count": self.step_count,
            "succeeded_step_count": self.succeeded_step_count,
            "failed_step_count": self.failed_step_count,
            "skipped_step_count": self.skipped_step_count,
            "step_results": [r.to_dict() for r in self.step_results],
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "duration_ms": self.duration_ms,
            "correlation_id": self.correlation_id,
            "error": self.error,
            "metadata": dict(self.metadata),
        }


# ===========================================================================
# Small helpers
# ===========================================================================

def new_correlation_id() -> str:
    """Return a short, stable, executor-level correlation id.

    Phase 6A uses this for log correlation across steps; downstream
    services stamp it onto their own structured events.
    """
    return f"exec-{uuid.uuid4().hex[:12]}"


def make_blank_execution_result(
    *,
    execution_id: str,
    plan_id: str,
    goal_id: str,
    correlation_id: Optional[str] = None,
    started_at: Optional[float] = None,
) -> ExecutionResult:
    """Construct an empty :class:`ExecutionResult` at the start of a run."""
    return ExecutionResult(
        execution_id=execution_id,
        plan_id=plan_id,
        goal_id=goal_id,
        outcome=ExecutionOutcome.COMPLETED,  # optimistic; downgraded on failure
        step_results=(),
        started_at=started_at if started_at is not None else time.time(),
        completed_at=None,
        correlation_id=correlation_id or new_correlation_id(),
    )
