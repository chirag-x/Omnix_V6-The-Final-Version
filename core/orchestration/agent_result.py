"""
Omnix V6 — Agent orchestrator result contracts (Phase 6C).

This module defines the *outer* state machine and the data shapes
the Agent Orchestrator produces at the end of a goal-driven run.

These are deliberately separate from :mod:`core.orchestration.execution_result`
(which holds the *per-step* executor state) so the boundary between
"what the executor did" and "what the agent decided" stays crisp.

Architectural rules honored here:

- R-8   — every status is a typed enum, never a bare bool.
- R-10  — results are ``frozen=True``; mutation is by ``with_*``.
- R-12  — the Agent is replaceable: the data shapes here describe
          what an Agent produced, not how.
- R-23  — the Agent never mutates :class:`ExecutionContext`; it
          only reads it and produces new values.
- R-24  — the Agent exposes typed end states (COMPLETE, FAILED,
          CANCELLED, TIMEOUT, CLARIFICATION_REQUIRED) so the
          CLI / UX layer can never confuse "succeeded" with
          "verification said yes".
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from .models import Failure, Goal, Plan, RecoveryDecision
from .execution_result import ExecutionResult


# ===========================================================================
# AgentState — the Agent's outer state machine
# ===========================================================================

class AgentState(str, Enum):
    """The Agent Orchestrator's outer lifecycle.

    State transitions::

        IDLE ──► RECEIVING_GOAL ──► PLANNING ──► PLAN_READY
                                                          │
                              ┌───────────────────────────┘
                              ▼
                          EXECUTING ──► OBSERVING ──► EVALUATING
                                                             │
                       ┌────────────────┬─────────────────────┼──────────────┐
                       ▼                ▼                     ▼              ▼
                   CONTINUE         RECOVER                REPLAN        COMPLETE
                       │                │                     │              │
                       └────────┐  ┌────┘                     │              │
                                ▼  ▼                          ▼              ▼
                            (loop back)                (back to PLANNING)  (terminal)
                                                            or EXECUTING
                                                            for the new plan

    Terminal states:
      * ``COMPLETE``                 — goal achieved and verified.
      * ``FAILED``                   — unrecoverable failure.
      * ``CANCELLED``                — user/system cancellation.
      * ``TIMEOUT``                  — bounded runtime exceeded.
      * ``CLARIFICATION_REQUIRED``   — cannot proceed without user input.
    """

    IDLE = "idle"
    RECEIVING_GOAL = "receiving_goal"
    PLANNING = "planning"
    PLAN_READY = "plan_ready"
    EXECUTING = "executing"
    OBSERVING = "observing"
    EVALUATING = "evaluating"
    COMPLETE = "complete"
    CONTINUE = "continue"
    RECOVER = "recover"
    REPLAN = "replan"
    CLARIFICATION_REQUIRED = "clarification_required"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"
    FAILED = "failed"


# Terminal states — the Agent's run has ended.
_TERMINAL_AGENT_STATES = frozenset({
    AgentState.COMPLETE,
    AgentState.FAILED,
    AgentState.CANCELLED,
    AgentState.TIMEOUT,
    AgentState.CLARIFICATION_REQUIRED,
})


def _is_terminal_agent_state(state: AgentState) -> bool:
    return state in _TERMINAL_AGENT_STATES


# ===========================================================================
# Plan history entry
# ===========================================================================

@dataclass(frozen=True)
class PlanHistoryEntry:
    """One slot in the Agent's plan history.

    The Agent keeps a Plan v1, then potentially a Plan v2, etc.  This
    dataclass records the plan and the failure (if any) that caused
    the next plan to be produced.

    Fields
    ------
    plan:
        The :class:`Plan` (v1, v2, ...).
    produced_from_failure:
        The :class:`Failure` that triggered replanning into this
        plan (``None`` for the initial plan).
    decision:
        The :class:`RecoveryDecision` that authorised the replan
        (``None`` for the initial plan).
    timestamp:
        Wall-clock seconds when the plan was produced.
    attempt:
        1-based attempt index — initial plan is 1, first replan is 2.
    """

    plan: Plan
    produced_from_failure: Optional[Failure] = None
    decision: Optional[RecoveryDecision] = None
    timestamp: float = 0.0
    attempt: int = 1


# ===========================================================================
# Observation history entry
# ===========================================================================

@dataclass(frozen=True)
class ObservationEntry:
    """One slot in the Agent's observation history.

    Fields
    ------
    step_id:
        The :class:`PlanStep` this observation pertains to.
    summary:
        Short text summary (safe to log).
    source:
        The :class:`ObservationSource` label.
    passed:
        ``True`` if the step's expected effect was verified,
        ``False`` if it was contradicted, ``None`` if uncertain.
    timestamp:
        Wall-clock seconds.
    metadata:
        Free-form structured fields.
    """

    step_id: str
    summary: str
    source: str
    passed: Optional[bool]
    timestamp: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)


# ===========================================================================
# ===========================================================================
# Step trace entry (Part 2 — per-step observability)
# ===========================================================================

@dataclass(frozen=True)
class StepTraceEntry:
    """One row in the Agent's per-step trace (Part 2 — observability).

    Used by the Part 2 observability surface to answer
    "what did the agent do on step N?".  The trace is an
    append-only list on :class:`AgentResult` (``step_trace``).

    Fields
    ------
    step_id:
        Stable identifier of the plan step (matches
        :class:`PlanStep`).
    plan_id:
        Id of the plan this step belongs to.
    attempt:
        1-based attempt index for this step.
    phase:
        Short phase label (e.g. ``"executing"``, ``"observing"``,
        ``"replanning"``).  Free-form.
    message:
        Human-readable one-liner (e.g. "opened chrome", "click
        failed: target not found").
    state:
        The terminal :class:`ExecutionResult` ``status`` of the
        attempt (``"ok"``, ``"failed"``, ``"timeout"``, ...).
    details:
        Free-form structured fields (e.g. ``{"exit_code": 0}``).
    duration_ms:
        Wall-clock duration of the attempt in milliseconds.
    timestamp:
        Wall-clock seconds when the attempt completed.
    """

    step_id: str
    attempt: int
    phase: str = ""
    message: str = ""
    state: str = ""
    plan_id: str = ""
    details: Dict[str, Any] = field(default_factory=dict)
    duration_ms: float = 0.0
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "step_id": self.step_id,
            "plan_id": self.plan_id,
            "attempt": self.attempt,
            "phase": self.phase,
            "message": self.message,
            "state": self.state,
            "details": dict(self.details),
            "duration_ms": self.duration_ms,
            "timestamp": self.timestamp,
        }


# AgentResult — the Agent's end-of-run summary
# ===========================================================================

@dataclass(frozen=True)
class AgentResult:
    """The Agent Orchestrator's return value for a single goal-driven run.

    The Agent is *goal-oriented* (not just plan-oriented): it returns
    this dataclass describing the full journey from goal to outcome.

    Fields
    ------
    agent_run_id:
        Unique id for this Agent run.
    goal_id:
        The :class:`Goal` the Agent was given.
    final_state:
        Terminal :class:`AgentState`.
    final_plan_id:
        Id of the plan that was last executed (``""`` if no plan was produced).
    final_execution_id:
        Id of the last :class:`ExecutionResult` (the executor's
        boundary), or ``""`` if nothing was executed.
    plan_history:
        Sequence of :class:`PlanHistoryEntry`, oldest first.  The
        initial plan is at index 0; replans follow.
    observation_history:
        Sequence of :class:`ObservationEntry`, oldest first.  Bounded
        to the most recent :attr:`observation_history_limit` entries.
    decision_history:
        Sequence of :class:`RecoveryDecision`, oldest first.  Bounded.
    failure_history:
        Sequence of :class:`Failure`, oldest first.  Bounded.
    started_at / completed_at:
        Wall-clock seconds.
    duration_ms:
        Convenience field cached at construction time.
    attempts:
        Total number of step attempts across all plan versions.
    replans:
        Number of replans performed (= ``len(plan_history) - 1``).
    error:
        Agent-level error message (e.g. "step foo failed irrecoverably");
        the per-step error lives in the per-step
        :class:`StepResult` embedded in the executor's
        :class:`ExecutionResult`.
    clarifying_question:
        The question the Agent wants to ask the user (only set when
        ``final_state == CLARIFICATION_REQUIRED``).
    metadata:
        Free-form structured fields for the audit log.
    """

    agent_run_id: str
    goal_id: str
    final_state: AgentState
    final_plan_id: str = ""
    final_execution_id: str = ""
    plan_history: Tuple[PlanHistoryEntry, ...] = ()
    observation_history: Tuple[ObservationEntry, ...] = ()
    decision_history: Tuple[RecoveryDecision, ...] = ()
    failure_history: Tuple[Failure, ...] = ()
    # Part 2: per-step observability trace.  Append-only; bounded
    # by ``step_trace_limit`` in :meth:`with_appended_step_trace`.
    step_trace: Tuple[StepTraceEntry, ...] = ()
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    duration_ms: float = 0.0
    attempts: int = 0
    replans: int = 0
    error: str = ""
    clarifying_question: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------- derived
    @property
    def completed(self) -> bool:
        """``True`` only when ``final_state`` is ``COMPLETE``."""
        return self.final_state is AgentState.COMPLETE

    @property
    def is_terminal(self) -> bool:
        return _is_terminal_agent_state(self.final_state)

    @property
    def plan_count(self) -> int:
        return len(self.plan_history)

    @property
    def failed(self) -> bool:
        return self.final_state in (
            AgentState.FAILED,
            AgentState.CANCELLED,
            AgentState.TIMEOUT,
        )

    def last_plan(self) -> Optional[Plan]:
        if not self.plan_history:
            return None
        return self.plan_history[-1].plan

    def last_failure(self) -> Optional[Failure]:
        if not self.failure_history:
            return None
        return self.failure_history[-1]

    def last_decision(self) -> Optional[RecoveryDecision]:
        if not self.decision_history:
            return None
        return self.decision_history[-1]

    # ----------------------------------------------------- with_* updates
    def with_final_state(
        self,
        state: AgentState,
        *,
        completed_at: Optional[float] = None,
        error: str = "",
    ) -> "AgentResult":
        new = replace(self, final_state=state, error=error or self.error)
        if completed_at is not None and self.started_at is not None:
            new = replace(
                new,
                completed_at=completed_at,
                duration_ms=max(0.0, (completed_at - self.started_at) * 1000.0),
            )
        elif completed_at is not None:
            new = replace(new, completed_at=completed_at)
        return new

    def with_clarifying_question(self, question: str) -> "AgentResult":
        return replace(
            self,
            clarifying_question=question,
            final_state=AgentState.CLARIFICATION_REQUIRED,
        )

    def with_appended_plan(self, entry: PlanHistoryEntry) -> "AgentResult":
        new_list: List[PlanHistoryEntry] = list(self.plan_history)
        new_list.append(entry)
        return replace(
            self,
            plan_history=tuple(new_list),
            replans=len(new_list) - 1,
            final_plan_id=entry.plan.plan_id,
        )

    def with_appended_observation(
        self, entry: ObservationEntry, *, limit: int = 100
    ) -> "AgentResult":
        new_list: List[ObservationEntry] = list(self.observation_history)
        new_list.append(entry)
        if limit > 0 and len(new_list) > limit:
            new_list = new_list[-limit:]
        return replace(self, observation_history=tuple(new_list))

    def with_appended_decision(
        self, decision: RecoveryDecision, *, limit: int = 100
    ) -> "AgentResult":
        new_list: List[RecoveryDecision] = list(self.decision_history)
        new_list.append(decision)
        if limit > 0 and len(new_list) > limit:
            new_list = new_list[-limit:]
        return replace(self, decision_history=tuple(new_list))

    def with_appended_failure(
        self, failure: Failure, *, limit: int = 100
    ) -> "AgentResult":
        new_list: List[Failure] = list(self.failure_history)
        new_list.append(failure)
        if limit > 0 and len(new_list) > limit:
            new_list = new_list[-limit:]
        return replace(self, failure_history=tuple(new_list))

    def with_appended_step_trace(
        self, entry: StepTraceEntry, *, limit: int = 500
    ) -> "AgentResult":
        """Append a :class:`StepTraceEntry` to the per-step trace.

        Bounded by ``limit`` (default 500).  Set ``limit=0`` to
        disable bounding.
        """
        new_list: List[StepTraceEntry] = list(self.step_trace)
        new_list.append(entry)
        if limit > 0 and len(new_list) > limit:
            new_list = new_list[-limit:]
        return replace(self, step_trace=tuple(new_list))

    def with_attempt(self, delta: int = 1) -> "AgentResult":
        return replace(self, attempts=max(0, self.attempts + delta))

    def with_metadata(self, **extra: Any) -> "AgentResult":
        merged = dict(self.metadata)
        merged.update(extra)
        return replace(self, metadata=merged)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": "AgentResult",
            "agent_run_id": self.agent_run_id,
            "goal_id": self.goal_id,
            "final_state": self.final_state.value,
            "completed": self.completed,
            "failed": self.failed,
            "final_plan_id": self.final_plan_id,
            "final_execution_id": self.final_execution_id,
            "plan_count": self.plan_count,
            "replans": self.replans,
            "attempts": self.attempts,
            "observation_count": len(self.observation_history),
            "decision_count": len(self.decision_history),
            "failure_count": len(self.failure_history),
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "duration_ms": self.duration_ms,
            "error": self.error,
            "clarifying_question": self.clarifying_question,
            "metadata": dict(self.metadata),
            "plan_history": [
                {
                    "plan_id": e.plan.plan_id,
                    "attempt": e.attempt,
                    "replan_count": e.plan.replan_count,
                    "parent_plan_id": e.plan.parent_plan_id,
                    "from_failure_id": (
                        e.produced_from_failure.failure_id
                        if e.produced_from_failure else None
                    ),
                    "decision_id": (
                        e.decision.decision_id if e.decision else None
                    ),
                }
                for e in self.plan_history
            ],
        }


# ===========================================================================
# Small helpers
# ===========================================================================

def new_agent_run_id() -> str:
    """Return a short, stable Agent-run correlation id."""
    return f"agent-{uuid.uuid4().hex[:12]}"


def make_blank_agent_result(
    *,
    agent_run_id: str,
    goal_id: str,
    started_at: Optional[float] = None,
) -> AgentResult:
    """Construct an empty :class:`AgentResult` at the start of a run."""
    return AgentResult(
        agent_run_id=agent_run_id,
        goal_id=goal_id,
        final_state=AgentState.IDLE,
        started_at=started_at if started_at is not None else time.time(),
        completed_at=None,
    )


__all__ = [
    "AgentState",
    "AgentResult",
    "PlanHistoryEntry",
    "ObservationEntry",
    "StepTraceEntry",
    "new_agent_run_id",
    "make_blank_agent_result",
]


# Re-export for convenience so callers can do
# ``from core.orchestration.agent_result import ExecutionResult`` if needed.
# (Not actually re-exported — callers should import ExecutionResult
# from ``core.orchestration.execution_result`` directly.)
_ = ExecutionResult  # silence linters; intentional reference
del _
