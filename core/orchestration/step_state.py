"""
Omnix V6 — Phase 14: typed step-state model.

The Phase 4 / Phase 5 ``PlanStep`` is *frozen* and *immutable*; it
describes a request to act.  This module adds the *runtime* state of
that step — the per-step state machine the :class:`PlanExecutor` and
:class:`Agent` walk together.

Why a separate module:
    * R-10 says orchestration models are frozen.  A mutable per-step
      state would violate that.
    * The existing :class:`core.orchestration.execution_result.StepState`
      is a *terminal* classification (SUCCEEDED / FAILED / ...).  It
      answers "what happened at the end" but does not model the
      *transitions* through READY → EXECUTING → OBSERVED → VERIFIED.
    * Phase 14 needs those transitions to:
        - publish per-step events (STEP_STARTED, STEP_COMPLETED);
        - support bounded re-grounding between steps;
        - make replanning well-typed (RECOVERING → REPLANNING).

Architectural isolation:
    This module MUST NOT import:
        * :mod:`core.omnix_engine`
        * :mod:`core.pipeline`
        * :mod:`core.capability_router`
        * :mod:`core.services.*` (vision / browser / memory / voice)
        * any V6 *Windows service* (e.g. ``system.windows.*``)
        * any V6 *AI provider* (e.g. ``ai.provider.*``)

    The state machine is a pure data model; it never executes a
    capability, never calls a service, never reads the screen.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class StepLifecycle(str, Enum):
    """The runtime lifecycle of a single :class:`PlanStep`.

    The state machine is intentionally explicit; transitions are
    *only* allowed in the directions documented below.  Any illegal
    transition must raise :class:`IllegalStepTransition` so a buggy
    executor cannot silently corrupt the lifecycle.

    Valid transitions::

        PLANNED     -> READY
        READY       -> EXECUTING
        READY       -> SKIPPED
        EXECUTING   -> EXECUTED
        EXECUTING   -> FAILED
        EXECUTING   -> TIMED_OUT
        EXECUTING   -> CANCELLED
        EXECUTING   -> RECOVERING
        EXECUTING   -> REPLANNING
        EXECUTED    -> OBSERVED
        EXECUTED    -> RECOVERING
        EXECUTED    -> REPLANNING
        OBSERVED    -> VERIFIED
        OBSERVED    -> UNCERTAIN
        OBSERVED    -> RECOVERING
        OBSERVED    -> REPLANNING
        RECOVERING  -> READY
        RECOVERING  -> REPLANNING
        RECOVERING  -> COMPLETED
        REPLANNING  -> READY
        VERIFIED    -> COMPLETED
        UNCERTAIN   -> COMPLETED
        FAILED      -> RECOVERING
        FAILED      -> REPLANNING
        FAILED      -> COMPLETED

    The terminal states are ``COMPLETED``, ``SKIPPED``, ``CANCELLED``,
    ``TIMED_OUT``, and ``FAILED`` (the last only when recovery itself
    gives up).  A step that finished in any of these stays there.
    """

    PLANNED = "planned"             # planner produced the step; nothing started
    READY = "ready"                 # dependencies satisfied; ready to dispatch
    EXECUTING = "executing"         # capability call in flight
    EXECUTED = "executed"           # capability returned; waiting on observation
    OBSERVED = "observed"           # post-action observation captured
    VERIFIED = "verified"           # observation matched expected effect
    UNCERTAIN = "uncertain"         # observation captured but verifier is uncertain
    RECOVERING = "recovering"       # recovery engine is producing a RecoveryDecision
    REPLANNING = "replanning"       # the whole plan is being regenerated
    COMPLETED = "completed"         # terminal: step finished (verified or uncertain)
    FAILED = "failed"               # terminal: unrecoverable failure
    TIMED_OUT = "timed_out"         # terminal: exceeded deadline
    CANCELLED = "cancelled"         # terminal: user/system cancelled
    SKIPPED = "skipped"             # terminal: executor decided to skip


# Map of allowed transitions.  Stored as a dict of from-state to a
# frozenset of to-states.  The state machine walks this map; tests
# (and runtime callers) can introspect it.
_ALLOWED_TRANSITIONS: Dict[StepLifecycle, frozenset] = {
    StepLifecycle.PLANNED: frozenset({StepLifecycle.READY}),
    StepLifecycle.READY: frozenset(
        {StepLifecycle.EXECUTING, StepLifecycle.SKIPPED}
    ),
    StepLifecycle.EXECUTING: frozenset(
        {
            StepLifecycle.EXECUTED,
            StepLifecycle.FAILED,
            StepLifecycle.TIMED_OUT,
            StepLifecycle.CANCELLED,
            StepLifecycle.RECOVERING,
            StepLifecycle.REPLANNING,
        }
    ),
    StepLifecycle.EXECUTED: frozenset(
        {
            StepLifecycle.OBSERVED,
            StepLifecycle.RECOVERING,
            StepLifecycle.REPLANNING,
        }
    ),
    StepLifecycle.OBSERVED: frozenset(
        {
            StepLifecycle.VERIFIED,
            StepLifecycle.UNCERTAIN,
            StepLifecycle.RECOVERING,
            StepLifecycle.REPLANNING,
        }
    ),
    StepLifecycle.RECOVERING: frozenset(
        {
            StepLifecycle.READY,
            StepLifecycle.REPLANNING,
            StepLifecycle.COMPLETED,
            StepLifecycle.FAILED,
        }
    ),
    StepLifecycle.REPLANNING: frozenset(
        {StepLifecycle.READY, StepLifecycle.COMPLETED, StepLifecycle.FAILED}
    ),
    StepLifecycle.VERIFIED: frozenset({StepLifecycle.COMPLETED}),
    StepLifecycle.UNCERTAIN: frozenset(
        {StepLifecycle.COMPLETED, StepLifecycle.RECOVERING, StepLifecycle.REPLANNING}
    ),
    # Terminal states accept no further transitions.
    StepLifecycle.COMPLETED: frozenset(),
    StepLifecycle.FAILED: frozenset(),
    StepLifecycle.TIMED_OUT: frozenset(),
    StepLifecycle.CANCELLED: frozenset(),
    StepLifecycle.SKIPPED: frozenset(),
}


_TERMINAL_STATES: frozenset = frozenset(
    {
        StepLifecycle.COMPLETED,
        StepLifecycle.FAILED,
        StepLifecycle.TIMED_OUT,
        StepLifecycle.CANCELLED,
        StepLifecycle.SKIPPED,
    }
)


class IllegalStepTransition(Exception):
    """Raised when a step's lifecycle is asked to make an illegal jump."""


def is_terminal(state: StepLifecycle) -> bool:
    """Return True if ``state`` is a terminal lifecycle state."""
    return state in _TERMINAL_STATES


def can_transition(src: StepLifecycle, dst: StepLifecycle) -> bool:
    """Return True if a step may move from ``src`` to ``dst``."""
    if src is dst:
        return True
    return dst in _ALLOWED_TRANSITIONS.get(src, frozenset())


def assert_transition(src: StepLifecycle, dst: StepLifecycle) -> None:
    """Raise :class:`IllegalStepTransition` if the transition is not allowed."""
    if src is dst:
        return
    if dst not in _ALLOWED_TRANSITIONS.get(src, frozenset()):
        raise IllegalStepTransition(
            f"Illegal step lifecycle transition: {src.value} -> {dst.value}"
        )


@dataclass
class StepExecutionState:
    """The *mutable* runtime state of a single :class:`PlanStep`.

    The executor owns one ``StepExecutionState`` per step it has
    dispatched.  The state is not frozen because it changes as the
    step walks its lifecycle; it lives alongside the immutable
    :class:`PlanStep` (R-10 is preserved on the plan model itself).

    Attributes
    ----------
    step_id:
        Matches :attr:`PlanStep.step_id` exactly.  The executor uses
        this as the join key between the immutable plan and the
        mutable runtime state.
    state:
        The current :class:`StepLifecycle`.  ``PLANNED`` is the
        default — a freshly imported step has not been touched.
    attempt:
        Number of *executor-attempted* invocations of this step.
        Incremented when the step enters ``EXECUTING``.  Bounded by
        the recovery policy's ``max_attempts_per_step``.
    last_observation_id:
        Identifier of the most recent observation captured for this
        step (``None`` until the executor takes one).  The state does
        not store the observation itself; that lives in the
        executor's observation history.  Storing only the id keeps
        this dataclass small and serialisation-friendly.
    last_verdict:
        ``"passed" | "failed" | "uncertain" | None``.  Mirrors the
        tri-state verifier so the agent can decide between recovery
        and completion without a second lookup.
    grounded_target_id:
        Identifier of the most recent :class:`TargetGroundingContract`
        used to dispatch this step.  ``None`` for steps that don't
        require grounding.  Phase 14 records this so a replan can
        re-ground without leaking the previous coordinates (R-21
        invariant: ground as close to execution time as safely
        possible).
    started_at, finished_at:
        Wall-clock seconds.  Both ``None`` until the relevant
        transition occurs.
    metadata:
        Free-form dict; used to record the recovery decision id, the
        replan id, or any other per-step audit trail that does not
        belong on the immutable plan.
    """

    step_id: str
    state: StepLifecycle = StepLifecycle.PLANNED
    attempt: int = 0
    last_observation_id: Optional[str] = None
    last_verdict: Optional[str] = None
    grounded_target_id: Optional[str] = None
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def transition_to(self, new_state: StepLifecycle) -> "StepExecutionState":
        """Return a new state with ``new_state`` if the transition is legal.

        The current state is left untouched (this dataclass is mutable
        but we keep the *transition* immutable: callers that want to
        change the lifecycle must assign the returned value).  This
        makes the transition visible at every call site and keeps the
        audit log honest.
        """
        assert_transition(self.state, new_state)
        return StepExecutionState(
            step_id=self.step_id,
            state=new_state,
            attempt=self.attempt,
            last_observation_id=self.last_observation_id,
            last_verdict=self.last_verdict,
            grounded_target_id=self.grounded_target_id,
            started_at=self.started_at,
            finished_at=self.finished_at,
            metadata=dict(self.metadata),
        )

    def is_terminal(self) -> bool:
        return is_terminal(self.state)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": "StepExecutionState",
            "step_id": self.step_id,
            "state": self.state.value,
            "attempt": self.attempt,
            "last_observation_id": self.last_observation_id,
            "last_verdict": self.last_verdict,
            "grounded_target_id": self.grounded_target_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "metadata": dict(self.metadata),
        }


__all__ = [
    "StepLifecycle",
    "IllegalStepTransition",
    "StepExecutionState",
    "is_terminal",
    "can_transition",
    "assert_transition",
]
