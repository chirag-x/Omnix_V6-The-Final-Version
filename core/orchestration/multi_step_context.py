"""
Omnix V6 — Phase 14: Multi-step execution context.

The Phase 4 :class:`core.orchestration.ExecutionContext` is a frozen
read-only projection over the ContextService for one plan run.  It
already carries the plan, completed-step ids, and failed-step ids —
but it does not carry the *per-step runtime state*, the *grounded
target references* that depend on previous steps, or the *previous
step's observation* that a later step may need to consult.

Phase 14 adds those, *without* modifying the frozen
:class:`ExecutionContext`.  A :class:`MultiStepContext` wraps an
existing :class:`ExecutionContext` and adds the Phase 14 layers:

    * :class:`StepExecutionState` per ``step_id`` (the lifecycle
      machine from :mod:`core.orchestration.step_state`);
    * :class:`TargetGroundingContract` references keyed by
      ``step_id`` (so a step that depends on Step 4's *observed*
      coordinates can resolve them lazily, at execution time);
    * an *inter-step observation log* that holds the latest
      :class:`Observation` for each executed step (so Step 5 can
      read Step 4's observation without re-querying the world).

The wrapper is itself a frozen dataclass; updates go through
``with_*`` methods that return a new instance (R-10).

Architectural isolation:
    This module MUST NOT import:
        * :mod:`core.omnix_engine`
        * :mod:`core.pipeline`
        * :mod:`core.capability_router`
        * :mod:`core.services.*` (vision / browser / memory / voice)
        * any V6 *Windows service* (e.g. ``system.windows.*``)
        * any V6 *AI provider* (e.g. ``ai.provider.*``)

    The context is data.  It never executes a capability, never
    calls a service, never reads the screen.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Dict, Mapping, Optional, Tuple

from core.orchestration.grounding import TargetGroundingContract
from core.orchestration.models import ExecutionContext, Observation
from core.orchestration.step_state import (
    IllegalStepTransition,
    StepExecutionState,
    StepLifecycle,
)


@dataclass(frozen=True)
class MultiStepContext:
    """Phase 14 extension over :class:`ExecutionContext`.

    The wrapper is a *value type*.  Every mutation returns a new
    :class:`MultiStepContext`; the previous one is left untouched.
    The Agent owns one instance per goal and threads it through every
    plan, replan, and step transition.

    Attributes
    ----------
    base:
        The frozen :class:`ExecutionContext` this wrapper enriches.
        All Phase 4 / Phase 5 reads (plan, goal, current step, etc.)
        delegate to ``base``; the wrapper never re-implements them.
    step_states:
        Mapping from ``step_id`` to its current
        :class:`StepExecutionState`.  A step that has not yet been
        touched does not appear in the map.
    grounded_targets:
        Mapping from ``step_id`` to the
        :class:`TargetGroundingContract` that was used to dispatch
        the step.  Stored so a replan can decide whether to
        re-ground (Phase 14 §3) and so the agent can inspect the
        most recent coordinates without re-querying vision.
    previous_observations:
        Mapping from ``step_id`` to the latest
        :class:`Observation` captured for that step.  Step N can
        read ``previous_observations[step_N_minus_1]`` to get the
        post-action snapshot of Step N-1 — that is the Phase 14
        "context between steps" requirement.
    inter_step_observations:
        Append-only list of *cross-step* observations the executor
        emitted on purpose (e.g. a "page loaded" screenshot taken
        between two browser steps).  Distinct from
        ``previous_observations`` which is one-per-step.
    """

    base: ExecutionContext
    step_states: Mapping[str, StepExecutionState] = field(default_factory=dict)
    grounded_targets: Mapping[str, TargetGroundingContract] = field(
        default_factory=dict
    )
    previous_observations: Mapping[str, Observation] = field(default_factory=dict)
    inter_step_observations: Tuple[Observation, ...] = ()

    # ------------------------------------------------------------------
    # Phase 4 / Phase 5 pass-through
    # ------------------------------------------------------------------
    @property
    def execution_id(self) -> str:
        return self.base.execution_id

    @property
    def goal(self):
        return self.base.goal

    @property
    def plan(self):
        return self.base.plan

    @property
    def intent(self):
        return self.base.intent

    @property
    def current_step_id(self) -> Optional[str]:
        return self.base.current_step_id

    @property
    def completed_step_ids(self) -> Tuple[str, ...]:
        return self.base.completed_step_ids

    @property
    def failed_step_ids(self) -> Tuple[str, ...]:
        return self.base.failed_step_ids

    @property
    def progress(self) -> float:
        return self.base.progress

    # ------------------------------------------------------------------
    # Phase 14 read accessors
    # ------------------------------------------------------------------
    def state_of(self, step_id: str) -> Optional[StepExecutionState]:
        """Return the :class:`StepExecutionState` for ``step_id`` or ``None``."""
        return self.step_states.get(step_id)

    def grounded_target_for(
        self, step_id: str
    ) -> Optional[TargetGroundingContract]:
        return self.grounded_targets.get(step_id)

    def previous_observation_for(self, step_id: str) -> Optional[Observation]:
        return self.previous_observations.get(step_id)

    def all_states(self) -> Tuple[StepExecutionState, ...]:
        return tuple(self.step_states.values())

    def pending_steps(self) -> Tuple[str, ...]:
        """Return step_ids whose state is not yet terminal."""
        out: list = []
        for step in self.plan.steps:
            st = self.step_states.get(step.step_id)
            if st is None or not st.is_terminal():
                out.append(step.step_id)
        return tuple(out)

    # ------------------------------------------------------------------
    # Phase 14 immutability-preserving updates
    # ------------------------------------------------------------------
    def with_step_state(self, state: StepExecutionState) -> "MultiStepContext":
        merged = dict(self.step_states)
        merged[state.step_id] = state
        return replace(self, step_states=merged)

    def with_grounded_target(
        self,
        step_id: str,
        target: TargetGroundingContract,
    ) -> "MultiStepContext":
        merged = dict(self.grounded_targets)
        merged[step_id] = target
        return replace(self, grounded_targets=merged)

    def with_previous_observation(
        self,
        step_id: str,
        observation: Observation,
    ) -> "MultiStepContext":
        merged = dict(self.previous_observations)
        merged[step_id] = observation
        return replace(self, previous_observations=merged)

    def with_inter_step_observation(
        self, observation: Observation
    ) -> "MultiStepContext":
        return replace(
            self,
            inter_step_observations=tuple(
                [*self.inter_step_observations, observation]
            ),
        )

    def with_base(self, base: ExecutionContext) -> "MultiStepContext":
        """Return a new wrapper over a different frozen ``base``."""
        return replace(self, base=base)

    def mark_step_started(
        self,
        step_id: str,
        *,
        started_at: Optional[float] = None,
    ) -> "MultiStepContext":
        """Record that ``step_id`` has entered EXECUTING.

        The lifecycle walk is automatic: ``PLANNED → READY → EXECUTING``
        if the step has not been touched yet, or just
        ``READY → EXECUTING`` if it has been through READY before
        (a recovery retry, for example).  The caller does not have
        to drive the state machine step-by-step; this helper does
        the bookkeeping.
        """
        existing = self.step_states.get(step_id) or StepExecutionState(step_id=step_id)
        if existing.state is StepLifecycle.PLANNED:
            existing = existing.transition_to(StepLifecycle.READY)
        if existing.state is not StepLifecycle.READY:
            # The step is already in flight or finished.  Caller
            # asked us to mark it started; that is a logic error
            # they need to see, not a silent override.
            raise IllegalStepTransition(
                f"Cannot mark_step_started on step {step_id!r}: "
                f"current state is {existing.state.value}, expected "
                f"PLANNED or READY."
            )
        next_state = existing.transition_to(StepLifecycle.EXECUTING)
        next_state = StepExecutionState(
            step_id=step_id,
            state=next_state.state,
            attempt=existing.attempt + 1,
            last_observation_id=existing.last_observation_id,
            last_verdict=existing.last_verdict,
            grounded_target_id=existing.grounded_target_id,
            started_at=started_at if started_at is not None else existing.started_at,
            finished_at=existing.finished_at,
            metadata=dict(existing.metadata),
        )
        return self.with_step_state(next_state)

    def mark_step_finished(
        self,
        step_id: str,
        *,
        new_state: StepLifecycle,
        finished_at: Optional[float] = None,
        verdict: Optional[str] = None,
    ) -> "MultiStepContext":
        """Record that ``step_id`` reached ``new_state`` (terminal or not)."""
        existing = self.step_states.get(step_id) or StepExecutionState(step_id=step_id)
        next_state = existing.transition_to(new_state)
        next_state = StepExecutionState(
            step_id=step_id,
            state=next_state.state,
            attempt=existing.attempt,
            last_observation_id=existing.last_observation_id,
            last_verdict=verdict if verdict is not None else existing.last_verdict,
            grounded_target_id=existing.grounded_target_id,
            started_at=existing.started_at,
            finished_at=finished_at if finished_at is not None else existing.finished_at,
            metadata=dict(existing.metadata),
        )
        return self.with_step_state(next_state)

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": "MultiStepContext",
            "execution_id": self.execution_id,
            "step_states": {
                sid: st.to_dict() for sid, st in self.step_states.items()
            },
            "grounded_targets": {
                sid: g.to_dict() for sid, g in self.grounded_targets.items()
            },
            "previous_observations": {
                sid: obs.to_dict()
                for sid, obs in self.previous_observations.items()
            },
            "inter_step_observations": [
                obs.to_dict() for obs in self.inter_step_observations
            ],
            "base": self.base.to_dict(),
        }


__all__ = ["MultiStepContext"]
