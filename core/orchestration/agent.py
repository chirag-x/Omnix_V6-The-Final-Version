"""
Omnix V6 — Agent Orchestrator (Phase 6C).

The Agent is the *outer loop* that wires the rest of the V6
orchestration together.  It is the canonical V6 closed-loop
controller — the only component in V6 that holds the goal and
drives the plan → execute → observe → decide cycle.

Closed loop
-----------

::

    Goal
     ↓
    PLANNING          Brain.plan(goal, ...)              → Plan v1
     ↓
    EXECUTING         PlanExecutorImpl.execute(ctx)      → ExecutionResult
     ↓
    OBSERVING         ObservationProvider.observe(...)   → Observation
     ↓
    EVALUATING        StepVerifier.verify(...)           → VerificationVerdict
                       GoalVerifier.verify(...)          → VerificationVerdict
     ↓
    DECIDING          DefaultRecoveryEngine.decide(...)  → RecoveryDecision
     ↓
    ├─ COMPLETE                                  (terminal)
    ├─ FAILED                                    (terminal)
    ├─ CANCELLED                                 (terminal)
    ├─ TIMEOUT                                   (terminal)
    ├─ CLARIFICATION_REQUIRED                    (terminal)
    ├─ CONTINUE  →  (back to EXECUTING)
    ├─ RECOVER   →  (apply decision: RETRY, SKIP, …)
    └─ REPLAN    →  (back to PLANNING with prior_plan + failure)

Architectural rules honored here:

- R-1  — the Agent is constructed once at engine boot; it does not
         create a second execution architecture.
- R-5  — every step's post-condition is checked by the
         :class:`Verifier`; every goal's success criteria are
         checked by the :class:`GoalVerifier`.
- R-8  — typed status enums throughout; no bare booleans.
- R-10 — all result dataclasses are ``frozen=True``; the Agent
         produces new :class:`AgentResult` values via ``with_*``.
- R-12 — the Agent is replaceable: ``Brain``, ``PlanExecutor``,
         ``RecoveryEngine``, ``Verifier``, ``ObservationProvider``
         are all constructor-injected.
- R-13 — the Agent never invents capability names; the closed
         registry set is the only valid surface.
- R-17 — ``loguru`` only.
- R-19 — the Agent's test surface lives in ``tests/test_agent_*.py``.
- R-21 — the Agent NEVER calls a Capability directly.  All execution
         goes through the :class:`PlanExecutor` (which itself
         dispatches through the :class:`CapabilityRouter`).
- R-23 — the Agent never mutates :class:`ExecutionContext`; it
         only reads it and produces new values.
- R-24 — the Agent is internal: it produces typed
         :class:`AgentState` / :class:`AgentResult` values, not
         user-facing strings.

What this module deliberately does NOT do
-----------------------------------------

- Implement vision, browser, voice, or persistent memory.
- Spawn a long-running asyncio loop; everything is synchronous.
- Bypass the :class:`CapabilityRouter` under any circumstance.
- Run forever: the recovery engine enforces a hard
  ``max_total_runtime_s`` budget, and the Agent enforces a
  per-run ``max_iterations`` cap.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from loguru import logger

from .models import (
    ExecutionContext,
    ExpectedEffect,
    Failure,
    FailureKind,
    Goal,
    Intent,
    Observation,
    ObservationSource,
    Plan,
    PlanStep,
    RecoveryAction,
    RecoveryDecision,
    VerificationVerdict,
)
from .execution_result import (
    ExecutionOutcome,
    ExecutionResult,
    StepResult,
    StepState,
)
from .agent_result import (
    AgentResult,
    AgentState,
    ObservationEntry,
    PlanHistoryEntry,
    make_blank_agent_result,
    new_agent_run_id,
)
from .observation import (
    CapabilityResultObservationProvider,
    ObservationProvider,
)
from .verifier import (
    DefaultGoalVerifier,
    DefaultStepVerifier,
    failed_verdict,
    passed_verdict,
    uncertain_verdict,
)
from .verifier_router import (
    VerifierRouter,
    build_default_router,
)
from .recovery import (
    DefaultRecoveryEngine,
    RecoveryPolicy,
    make_failure,
)
from .failure_classifier import (
    FailureClassifier,
)
from .grounding import (
    DEFAULT_CONFIDENCE_THRESHOLD,
    GroundingStatus,
    TargetGroundingContract,
)
from .progress import (
    ProgressBroadcaster,
    ProgressEvent,
    ProgressPhase,
    make_progress_event,
)
from .retry import RetryTracker, make_blank_retry_counters
from .dag import validate_plan as validate_plan_dag
from .cancellation import CancellationToken


def _default_cancellation_token() -> CancellationToken:
    """Return a fresh, never-cancelled :class:`CancellationToken`.

    The Agent's :attr:`cancellation_token` is initialised with
    one of these so the field is never ``None`` in production
    code.  Tests may pass an explicit token to drive the
    cancellation path deterministically.
    """
    return CancellationToken()


# Phase 2: a tiny factory used when the Agent is constructed
# without an explicit ``step_verifier``.  Wrapping the default in
# a router gives every capability a routing point — Phase 3
# can register specialized verifiers against this same router
# without changing the Agent's call site.
_default_step_verifier_router = build_default_router


# ===========================================================================
# Emit-kind → ProgressPhase mapping (System 8)
# ===========================================================================
# The Agent's internal ``_emit`` calls use free-text ``kind`` strings
# (preserved from Phase 6C for backwards compatibility).  When a
# :class:`ProgressBroadcaster` is wired, we forward each event as a
# typed :class:`ProgressEvent` whose phase is mapped from the
# internal kind.  Unknown kinds collapse to ``ProgressPhase.INFO``.

_EMIT_KIND_TO_PROGRESS_PHASE: Dict[str, ProgressPhase] = {
    "agent_started": ProgressPhase.PLAN_STARTED,
    "plan_built": ProgressPhase.PLAN_STARTED,
    "executing": ProgressPhase.STEP_DISPATCHED,
    "step_dispatched": ProgressPhase.STEP_DISPATCHED,
    "step_observed": ProgressPhase.STEP_OBSERVED,
    "step_verified": ProgressPhase.STEP_VERIFIED,
    "step_failed": ProgressPhase.STEP_FAILED,
    "retry": ProgressPhase.STEP_RETRIED,
    "skip": ProgressPhase.STEP_SKIPPED,
    "replan": ProgressPhase.REPLAN_STARTED,
    "decision": ProgressPhase.RECOVERY_DECISION,
    "precondition_evaluated": ProgressPhase.PRECONDITION_EVALUATED,
    "postcondition_evaluated": ProgressPhase.POSTCONDITION_EVALUATED,
    "idempotency_checked": ProgressPhase.IDEMPOTENCY_CHECKED,
    "reground_triggered": ProgressPhase.REGROUND_TRIGGERED,
    "agent_state_transition": ProgressPhase.INFO,
}

_TERMINAL_STATE_TO_PROGRESS_PHASE: Dict[str, ProgressPhase] = {
    "complete": ProgressPhase.AGENT_COMPLETE,
    "completed": ProgressPhase.AGENT_COMPLETE,
    "failed": ProgressPhase.AGENT_FAILED,
    "cancelled": ProgressPhase.AGENT_CANCELLED,
    "timeout": ProgressPhase.AGENT_TIMEOUT,
    "clarification_required": ProgressPhase.AGENT_CLARIFICATION,
}


# ===========================================================================
# Public Protocols the Agent uses (typed injection points)
# ===========================================================================

from .interfaces import (  # noqa: E402  (after constants for clarity)
    IntentInterpreter,
    Planner,
    PlanExecutor,
    RecoveryEngine,
)


# ===========================================================================
# Tunables (Agent-level)
# ===========================================================================

@dataclass(frozen=True)
class AgentPolicy:
    """Bounded policy that prevents the Agent from looping forever.

    The Agent enforces two independent caps:

      * ``max_iterations``  — total number of EXECUTE / REPLAN cycles
                              in a single run.  When exceeded, the
                              Agent returns ``AgentState.FAILED`` with
                              a deterministic message.
      * ``max_total_runtime_s`` — wall-clock cap.  Mirrors the
                                   recovery engine's runtime cap;
                                   the Agent checks it independently
                                   so a slow recovery engine cannot
                                   make the run hang.
    """

    max_iterations: int = 16
    max_total_runtime_s: float = 120.0
    observation_history_limit: int = 100
    decision_history_limit: int = 100
    failure_history_limit: int = 100

    def with_overrides(self, **kwargs: Any) -> "AgentPolicy":
        return replace(self, **kwargs)


# ===========================================================================
# Agent
# ===========================================================================

class Agent:
    """The V6 Agent Orchestrator.

    The Agent is the **only** component in V6 that drives the
    closed loop.  It holds the goal, the plan history, the
    observation history, the decision history, and the failure
    history.  It does not contain any LLM call (the
    :class:`IntentInterpreter` and :class:`Planner` are injected),
    and it does not contain any capability execution (the
    :class:`PlanExecutor` is injected).

    Construction
    ------------
    Required collaborators:

      * ``interpreter``     — :class:`IntentInterpreter` (text → Intent).
      * ``planner``         — :class:`Planner` (Goal → Plan).
      * ``plan_executor``   — :class:`PlanExecutor` (Plan → ExecutionResult).
      * ``recovery_engine`` — :class:`RecoveryEngine` (Failure → RecoveryDecision).
      * ``step_verifier``   — :class:`Verifier` for step-level checks.
      * ``goal_verifier``   — :class:`Verifier` for goal-level checks.
      * ``observation_provider`` — :class:`ObservationProvider` (default:
        :class:`CapabilityResultObservationProvider`).

    Optional collaborators:

      * ``policy``            — :class:`AgentPolicy` (bounded runtime).
      * ``observability_sink`` — callable invoked with structured
                                 events the Agent emits.

    Phase 14: optional ``multi_step_coordinator`` — a
    :class:`MultiStepCoordinator` that owns the per-execution
    :class:`MultiStepContext`, :class:`IdempotencyLog`, pre/post
    conditions, re-grounding, and scroll fallback.  When wired,
    the Agent consults it before dispatch (preconditions +
    idempotency + re-grounding) and after dispatch
    (postconditions + world-fact stamping).  When not wired, the
    Agent behaves exactly as in Phase 6C.
    """

    def __init__(
        self,
        *,
        interpreter: IntentInterpreter,
        planner: Planner,
        plan_executor: PlanExecutor,
        recovery_engine: Optional[RecoveryEngine] = None,
        step_verifier: Optional[Any] = None,
        goal_verifier: Optional[Any] = None,
        observation_provider: Optional[ObservationProvider] = None,
        policy: Optional[AgentPolicy] = None,
        observability_sink: Optional[Callable[[str, Dict[str, Any]], None]] = None,
        vision_service: Optional[Any] = None,
        confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
        multi_step_coordinator: Optional[Any] = None,
        progress_broadcaster: Optional[Any] = None,
        cancellation_token: Optional[Any] = None,
        failure_classifier: Optional[FailureClassifier] = None,
        retry_tracker: Optional[RetryTracker] = None,
    ) -> None:
        self.interpreter = interpreter
        self.planner = planner
        self.plan_executor = plan_executor
        self.recovery_engine = recovery_engine or DefaultRecoveryEngine()
        self.step_verifier = step_verifier or _default_step_verifier_router()
        self.goal_verifier = goal_verifier or DefaultGoalVerifier()
        self.observation_provider = (
            observation_provider or CapabilityResultObservationProvider()
        )
        # Phase 3: the single place that reads OmnixError codes and
        # maps them to a FailureKind.  Defaults to a stock
        # classifier; tests can pass a custom one.
        self.failure_classifier = failure_classifier or FailureClassifier()
        self.policy = policy or AgentPolicy()
        self.observability_sink = observability_sink
        # System 8: optional structured progress broadcaster.  When
        # set, every ``_emit`` call is forwarded to it as a typed
        # :class:`ProgressEvent`.  The broadcaster is fail-soft by
        # contract — a bad listener never breaks the Agent.
        self.progress_broadcaster = progress_broadcaster
        # System 8: optional retry counter.  When not provided we
        # build a private one (the engine usually wires its own
        # tracker so observability spans the full request).
        self.retry_tracker: RetryTracker = (
            retry_tracker
            if retry_tracker is not None
            else RetryTracker(
                broadcaster=progress_broadcaster,
                correlation_id="",
            )
        )
        # Phase 7.2: optional vision pre-action grounding service.
        # When set, the Agent consults it before any step whose
        # ``metadata["vision_pre_action"]`` is set.  The Agent never
        # calls capabilities directly; vision results flow through
        # the typed :class:`TargetGroundingContract` and the
        # :mod:`core.orchestration.vision_adapter` adapter.
        self.vision_service = vision_service
        self.confidence_threshold = confidence_threshold
        # Phase 14: optional multi-step coordination layer.  When
        # wired, the Agent consults it for preconditions,
        # idempotency, and re-grounding before dispatch, and for
        # postconditions + world-fact stamping after dispatch.
        # The coordinator never bypasses the executor or the
        # capability router.
        self.multi_step_coordinator = multi_step_coordinator

        self._name: str = "agent-orchestrator"
        self._state: AgentState = AgentState.IDLE
        self._last_result: Optional[AgentResult] = None
        # Carries the next plan produced by REPLAN between iterations.
        self._pending_next_plan: Optional[Plan] = None
        # Phase 3 single-retry path: caches the successful retry
        # result so the next loop iteration does NOT re-execute
        # the whole plan (which would re-dispatch the step we
        # just retried).  The pending step result is consumed by
        # the loop at the start of the next iteration and merged
        # into a synthetic ExecutionResult.
        self._pending_step_results: Tuple["StepResult", ...] = ()
        # Phase 1 / D11 + Phase 4: cooperative cancellation.
        # When the token flips, the closed loop finalises on the
        # next iteration.  We default to a fresh token so the
        # field is always non-None in production code.
        self.cancellation_token = (
            cancellation_token
            if cancellation_token is not None
            else _default_cancellation_token()
        )

    # --------------------------------------------------------- identity
    @property
    def name(self) -> str:
        return self._name

    @property
    def state(self) -> AgentState:
        return self._state

    @property
    def last_result(self) -> Optional[AgentResult]:
        return self._last_result

    def reset(self) -> None:
        """Reset per-run state.

        Calls ``reset()`` on the recovery engine (if it supports it)
        so the next ``run`` starts from a clean slate.  The Agent's
        own observable state moves back to :data:`AgentState.IDLE`.
        """
        if hasattr(self.recovery_engine, "reset"):
            try:
                self.recovery_engine.reset()
            except Exception:  # noqa: BLE001
                # A flaky recovery engine must not poison the Agent.
                logger.debug("recovery_engine.reset() raised; continuing")
        # Phase 1 / D2: also reset the multi-step coordinator so
        # its world facts, idempotency log, and grounding cache
        # do not leak between runs.
        if self.multi_step_coordinator is not None and \
                hasattr(self.multi_step_coordinator, "reset"):
            try:
                self.multi_step_coordinator.reset()
            except Exception:  # noqa: BLE001
                # A flaky coordinator must not poison the Agent.
                logger.debug("multi_step_coordinator.reset() raised; continuing")
        self._state = AgentState.IDLE
        self._last_result = None
        self._pending_next_plan = None
        self._pending_step_results = ()

    def set_cancellation_token(self, token: Optional[Any]) -> None:
        """Install a per-run :class:`CancellationToken`.

        Phase 4: the pipeline passes the same token it created
        for this ``correlation_id`` so the engine can cancel
        by-id.  The token is *not* reset by :meth:`reset` — the
        engine owns the lifetime, and the next request will
        overwrite it via this method.
        """
        self.cancellation_token = token

    # ============================================================== API
    def run(
        self,
        text: str,
        *,
        context_snapshot: Optional[Dict[str, Any]] = None,
    ) -> AgentResult:
        """End-to-end: text → intent → goal → plan(s) → execute.

        This is the public entry point most callers will use.  It
        drives the Agent from :data:`AgentState.IDLE` to a
        terminal state and returns the :class:`AgentResult`.

        Parameters
        ----------
        text:
            The user's utterance.
        context_snapshot:
            Optional read-only context projection (mirrors
            :meth:`Orchestrator.handle_user_input`).

        Returns
        -------
        :class:`AgentResult`
            The terminal result.  Never ``None``.
        """
        self._transition(AgentState.RECEIVING_GOAL)
        self._emit("agent_started", text=text)

        started_at = time.time()
        agent_run_id = new_agent_run_id()
        blank = make_blank_agent_result(
            agent_run_id=agent_run_id,
            goal_id="",  # filled in once the intent → goal
            started_at=started_at,
        )

        # ---- 1. Intent ------------------------------------------------
        try:
            raw = self.interpreter.interpret(
                text, context_snapshot=context_snapshot
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("interpreter.interpret() raised: {!r}", exc)
            self._transition(AgentState.FAILED)
            return blank.with_final_state(
                AgentState.FAILED,
                completed_at=time.time(),
                error=f"interpreter failed: {exc!r}",
            )

        # The Agent's IntentInterpreter protocol declares that
        # interpret() returns a bare :class:`Intent`.  The
        # production LLM interpreter in ``ai.intent.interpreter``
        # instead returns an :class:`IntentResult` envelope with
        # ``status``/``intent``/``clarifying_question``/``error_*``
        # fields.  Normalize both shapes into a single (intent,
        # terminal_state, error) tuple so the rest of the loop is
        # unaware of the interpreter's wire format.
        intent, terminal_state, terminal_error, clarifying = (
            self._unwrap_interpreter_result(raw)
        )
        if intent is None or terminal_state is not None:
            if terminal_state is AgentState.CLARIFICATION_REQUIRED:
                return blank.with_clarifying_question(
                    clarifying or "Could you clarify your request?",
                ).with_final_state(
                    AgentState.CLARIFICATION_REQUIRED,
                    completed_at=time.time(),
                    error=terminal_error or "clarification required",
                )
            return blank.with_final_state(
                terminal_state or AgentState.FAILED,
                completed_at=time.time(),
                error=terminal_error or "interpreter returned no intent",
            )

        # ---- 2. Goal --------------------------------------------------
        try:
            goal = intent.to_goal()
        except Exception as exc:  # noqa: BLE001
            logger.warning("intent.to_goal() raised: {!r}", exc)
            self._transition(AgentState.FAILED)
            return blank.with_final_state(
                AgentState.FAILED,
                completed_at=time.time(),
                error=f"could not derive goal from intent: {exc!r}",
            )

        result = blank.with_metadata(intent_id=intent.intent_id)
        result = replace(result, goal_id=goal.goal_id)
        return self._run_goal(goal, intent=intent, initial_result=result)

    @staticmethod
    def _unwrap_interpreter_result(
        raw: Any,
    ) -> Tuple[Optional[Intent], Optional[AgentState], Optional[str], Optional[str]]:
        """Normalize an interpreter return value into ``(intent, state, error, clarifying)``.

        The protocol contract is ``-> Intent``, so test stubs return a
        bare :class:`Intent`.  The real LLM interpreter returns an
        :class:`IntentResult` envelope.  This helper accepts both.
        """
        # Bare Intent path (Protocol contract; matches test stubs).
        if raw is not None and hasattr(raw, "to_goal") and not hasattr(raw, "status"):
            return raw, None, None, None
        # IntentResult envelope path.
        status = getattr(raw, "status", None)
        if status == "ok":
            inner = getattr(raw, "intent", None)
            if inner is None:
                return None, AgentState.FAILED, "interpreter returned ok with no intent", None
            return inner, None, None, None
        if status == "clarification":
            return (
                None,
                AgentState.CLARIFICATION_REQUIRED,
                "clarification required",
                getattr(raw, "clarifying_question", None),
            )
        if status == "unknown":
            return (
                None,
                AgentState.FAILED,
                getattr(raw, "error_message", None) or "intent not understood",
                None,
            )
        if status == "error":
            code = getattr(raw, "error_code", None) or "INTENT_ERROR"
            msg = getattr(raw, "error_message", None) or "interpreter error"
            return None, AgentState.FAILED, f"{code}: {msg}", None
        # Unknown shape: treat as failure rather than crash.
        return None, AgentState.FAILED, f"unrecognized interpreter result: {raw!r}", None

    def run_goal(
        self,
        goal: Goal,
        *,
        intent: Optional[Intent] = None,
    ) -> AgentResult:
        """Run a pre-built :class:`Goal` end-to-end.

        Useful when the caller has already produced a Goal (e.g. a
        pre-defined test scenario or a downstream consumer of the
        :class:`Planner`).  Identical semantics to :meth:`run` minus
        the intent-interpretation step.
        """
        self._transition(AgentState.RECEIVING_GOAL)
        self._emit("agent_started", goal_id=goal.goal_id)

        started_at = time.time()
        blank = make_blank_agent_result(
            agent_run_id=new_agent_run_id(),
            goal_id=goal.goal_id,
            started_at=started_at,
        )
        return self._run_goal(goal, intent=intent, initial_result=blank)

    # ====================================================== internal loop
    def _run_goal(
        self,
        goal: Goal,
        *,
        intent: Optional[Intent],
        initial_result: AgentResult,
    ) -> AgentResult:
        """Drive the closed loop for one goal."""
        result = initial_result
        current_plan: Optional[Plan] = None
        iter_count = 0

        # ---- State: PLANNING (initial) -------------------------------
        self._transition(AgentState.PLANNING)
        result, current_plan = self._plan_once(
            result=result,
            goal=goal,
            intent=intent,
            prior_plan=None,
            failure=None,
        )
        if current_plan is None:
            return result  # _plan_once already finalized it

        # System 8: validate the dependency DAG *once* on the first
        # plan admission.  Defects (cycles, unknown deps, duplicate
        # step ids) are surfaced as a DAG failure kind and the run
        # is finalised; replans reuse the same validator.
        if current_plan is not None:
            dag_result = validate_plan_dag(current_plan)
            if not dag_result.ok:
                self._emit(
                    "plan_refused",
                    goal_id=goal.goal_id,
                    plan_id=current_plan.plan_id,
                    dag_issues=[i.to_dict() for i in dag_result.issues],
                )
                return self._finalize(
                    result,
                    AgentState.FAILED,
                    error=(
                        f"plan DAG invalid: "
                        f"{dag_result.issue_count} issue(s): "
                        f"{', '.join(i.kind.value for i in dag_result.issues[:3])}"
                    ),
                )

        # ---- State: EXECUTING (first plan) ---------------------------
        self._transition(AgentState.PLAN_READY)
        while True:
            iter_count += 1
            if iter_count > self.policy.max_iterations:
                return self._finalize(
                    result, AgentState.FAILED,
                    error=(
                        f"Agent exceeded max_iterations="
                        f"{self.policy.max_iterations}"
                    ),
                )

            # Phase 1 / D11 + Phase 4: cooperative cancellation.
            # The token is checked at the top of every loop
            # iteration.  When the user (or voice) flips the
            # token, the loop finalises immediately and the
            # final state is ``CANCELLED``, not ``FAILED``.
            token = getattr(self, "cancellation_token", None)
            if token is not None and getattr(token, "is_cancelled", False):
                return self._finalize(
                    result, AgentState.CANCELLED,
                    error=(
                        getattr(token, "reason", "")
                        or "cancelled by user or system"
                    ),
                )

            # Runtime cap (Agent-level; recovery engine has its own too).
            if self._runtime_exceeded(result):
                return self._finalize(
                    result, AgentState.TIMEOUT,
                    error=(
                        f"Agent exceeded max_total_runtime_s="
                        f"{self.policy.max_total_runtime_s}"
                    ),
                )

            # Apply any plan that REPLAN produced in the previous
            # iteration.
            if self._pending_next_plan is not None:
                current_plan = self._pending_next_plan
                self._pending_next_plan = None
                self._transition(AgentState.PLAN_READY)

            # Phase 3 single-retry path: if a previous iteration
            # cached a successful (or failed) step result from
            # ``_retry_single_step``, build a synthetic
            # ExecutionResult and skip re-running the whole plan.
            # Without this, the executor would re-dispatch every
            # step in the plan, including the one we just retried.
            if self._pending_step_results:
                pending = self._pending_step_results
                self._pending_step_results = ()
                self._transition(AgentState.OBSERVING)
                exec_result = self._build_synthetic_exec_result(
                    plan=current_plan, step_results=pending,
                )
                self._transition(AgentState.EVALUATING)
                # Run observation + verification on the synthetic
                # result so the loop can decide whether the goal
                # is now satisfied.
                result = self._observe_and_record(
                    result=result, plan=current_plan, exec_result=exec_result,
                )
                goal_verdict, step_verdicts = self._evaluate(
                    plan=current_plan, exec_result=exec_result,
                    goal=goal, result=result,
                )
                result = self._record_evaluations(
                    result=result, goal_verdict=goal_verdict,
                    step_verdicts=step_verdicts,
                )
                # Extract the first failed step from the synthetic
                # result so the recovery engine can decide on it.
                # If the retry succeeded, ``current_failure`` is
                # None and the loop completes via the goal-verifier
                # happy path.
                current_failure: Optional[Failure] = None
                for sr in exec_result.step_results:
                    if sr.status in (
                        StepState.FAILED,
                        StepState.TIMED_OUT,
                        StepState.CANCELLED,
                    ):
                        current_failure = self._failure_from_step(
                            sr, current_plan
                        )
                        result = result.with_appended_failure(
                            current_failure,
                            limit=self.policy.failure_history_limit,
                        )
                        break
                decision = self._decide(
                    result=result, exec_result=exec_result,
                    current_failure=current_failure,
                    goal_verdict=goal_verdict, goal=goal, plan=current_plan,
                )
                if decision is not None:
                    result = result.with_appended_decision(
                        decision, limit=self.policy.decision_history_limit,
                    )
                terminal = self._branch(
                    result=result, decision=decision,
                    current_failure=current_failure,
                    goal_verdict=goal_verdict, goal=goal, intent=intent,
                    current_plan=current_plan,
                )
                if terminal is not None:
                    return terminal
                result = self._last_result or result
                self._transition(AgentState.CONTINUE)
                continue  # pragma: no cover -- defensive; loop continues

            # ---- EXECUTING ----------------------------------------
            self._transition(AgentState.EXECUTING)
            result, exec_result, current_failure = self._execute_plan(
                result=result, plan=current_plan, goal=goal, intent=intent,
            )
            if exec_result is None:
                return self._finalize(
                    result, AgentState.FAILED,
                    error="executor returned no result",
                )

            # ---- OBSERVING + EVALUATING ---------------------------
            self._transition(AgentState.OBSERVING)
            result = self._observe_and_record(
                result=result, plan=current_plan, exec_result=exec_result,
            )

            self._transition(AgentState.EVALUATING)
            goal_verdict, step_verdicts = self._evaluate(
                plan=current_plan,
                exec_result=exec_result,
                goal=goal,
                result=result,
            )
            result = self._record_evaluations(
                result=result, goal_verdict=goal_verdict,
                step_verdicts=step_verdicts,
            )

            # ---- DECIDING -----------------------------------------
            decision = self._decide(
                result=result,
                exec_result=exec_result,
                current_failure=current_failure,
                goal_verdict=goal_verdict,
                goal=goal,
                plan=current_plan,
            )
            if decision is not None:
                result = result.with_appended_decision(
                    decision, limit=self.policy.decision_history_limit
                )

            # ---- Branching ----------------------------------------
            terminal = self._branch(
                result=result,
                decision=decision,
                current_failure=current_failure,
                goal_verdict=goal_verdict,
                goal=goal,
                intent=intent,
                current_plan=current_plan,
            )
            if terminal is not None:
                return terminal
            # _branch returned None (continue) — but it may have
            # mutated the result (e.g. via _replan appending a new
            # plan entry).  Use the latest result on the instance.
            result = self._last_result or result
            # Phase 1 / D3: emit the AgentState.CONTINUE transition
            # so the state machine is honest about the closed-loop
            # iteration.  Previously CONTINUE was defined but never
            # set, so the FSM jumped from EVALUATING straight back
            # to EXECUTING without an observable transition.
            self._transition(AgentState.CONTINUE)

    # --------------------------------------------------------- planning
    def _plan_once(
        self,
        *,
        result: AgentResult,
        goal: Goal,
        intent: Optional[Intent],
        prior_plan: Optional[Plan],
        failure: Optional[Failure],
    ) -> Tuple[AgentResult, Optional[Plan]]:
        """Call the planner once.  Returns ``(result, plan_or_None)``."""
        self._emit(
            "planning",
            goal_id=goal.goal_id,
            replan=failure is not None,
        )
        try:
            plan = self.planner.plan(
                goal,
                intent=intent,
                prior_plan=prior_plan,
                failure=failure,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("planner.plan() raised: {!r}", exc)
            err = result.with_final_state(
                AgentState.FAILED,
                completed_at=time.time(),
                error=f"planner failed: {exc!r}",
            )
            return err, None

        if plan is None or not plan.steps:
            # Some planners return None to request clarification.
            # We surface that as a terminal state.
            self._emit("plan_refused", goal_id=goal.goal_id)
            err = result.with_clarifying_question(
                "I need more information to plan this goal."
            )
            err = err.with_final_state(
                AgentState.CLARIFICATION_REQUIRED,
                completed_at=time.time(),
            )
            return err, None

        attempt = (prior_plan.replan_count + 2) if prior_plan is not None else 1
        entry = PlanHistoryEntry(
            plan=plan,
            produced_from_failure=failure,
            timestamp=time.time(),
            attempt=attempt,
        )
        result = result.with_appended_plan(entry)
        self._emit(
            "plan_ready",
            plan_id=plan.plan_id,
            step_count=plan.step_count,
            replan_count=plan.replan_count,
        )
        return result, plan

    # --------------------------------------------------------- executing
    def _execute_plan(
        self,
        *,
        result: AgentResult,
        plan: Plan,
        goal: Goal,
        intent: Optional[Intent],
    ) -> Tuple[AgentResult, Optional[ExecutionResult], Optional[Failure]]:
        """Run the executor on the plan.

        Returns ``(result, exec_result, last_failure)``.  The
        ``last_failure`` is the first failed step's :class:`Failure`
        (or ``None`` if everything succeeded).
        """
        self._emit("executing", plan_id=plan.plan_id)

        # System 8: emit one STEP_DISPATCHED event per step so the
        # observability layer can show real-time progress.  We do
        # this *before* the executor runs so a watcher sees the
        # dispatch even if the executor blocks.
        for step in plan.steps:
            self._emit(
                "step_dispatched",
                plan_id=plan.plan_id,
                step_id=step.step_id,
                capability_name=step.capability_name,
            )

        # --- PRE-ACTION GROUNDING ---
        modified_steps: List[PlanStep] = []
        for step in plan.steps:
            contract, failure = self._apply_pre_action_grounding(step, plan.plan_id)
            if failure is not None:
                # Pre-action grounding failed (e.g. ambiguity, low confidence)
                # We block execution entirely and return the failure to REPLAN.
                logger.warning("Pre-action grounding blocked step {}: {}", step.step_id, failure.message)
                result = result.with_appended_failure(
                    failure, limit=self.policy.failure_history_limit
                )
                return result, None, failure

            if contract is not None and contract.is_grounded:
                from .vision_adapter import adapt_pre_action
                # Adapt the PlanStep precisely to the ActionRequest shape specified by the adapter
                adapted = adapt_pre_action(
                    contract,
                    kind=step.metadata["vision_pre_action"],
                    text=step.parameters.get("text")
                )

                # Replace capability, params, expected_effect, and metadata on the step
                step = replace(
                    step,
                    capability_name=adapted.capability_name,
                    parameters=dict(adapted.request.parameters),
                    expected_effect=adapted.request.expected_effect,
                    metadata={**step.metadata, **adapted.request.metadata}
                )

            modified_steps.append(step)

        plan = replace(plan, steps=tuple(modified_steps))
        # -----------------------------

        # --- PHASE 14: preconditions + idempotency + re-grounding ---
        if self.multi_step_coordinator is not None:
            blocked_step: Optional[PlanStep] = None
            for step in plan.steps:
                pre = self.multi_step_coordinator.evaluate_preconditions(step)
                if not pre.ok:
                    failure = make_failure(
                        kind=FailureKind.PRECONDITION,
                        plan_id=plan.plan_id,
                        step_id=step.step_id,
                        message=(
                            f"step {step.step_id!r} failed preconditions: "
                            f"{list(pre.failed)}"
                        ),
                        is_retryable=False,
                    )
                    result = result.with_appended_failure(
                        failure, limit=self.policy.failure_history_limit
                    )
                    blocked_step = step
                    break
                try:
                    self.multi_step_coordinator.reground_for_step(step)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "MultiStepCoordinator.reground_for_step raised: {!r}",
                        exc,
                    )
            if blocked_step is not None:
                return result, None, failure
        # ------------------------------------------------------------

        execution_id = f"{result.agent_run_id}:{plan.plan_id}"
        try:
            ctx = ExecutionContext(
                execution_id=execution_id,
                goal=goal,
                plan=plan,
                intent=intent,
                started_at=time.time(),
                # Phase 4: thread the cancellation token into the
                # executor so it can short-circuit between steps.
                cancellation_token=self.cancellation_token,
            )
            exec_result = self.plan_executor.execute(ctx)
        except Exception as exc:  # noqa: BLE001
            logger.warning("plan_executor.execute() raised: {!r}", exc)
            failure = make_failure(
                kind=FailureKind.INTERNAL,
                plan_id=plan.plan_id,
                message=f"executor raised: {exc!r}",
            )
            result = result.with_appended_failure(
                failure, limit=self.policy.failure_history_limit
            )
            return result, None, failure

        result = replace(
            result,
            final_execution_id=exec_result.execution_id,
        )

        # --- PHASE 14: postconditions + world-fact stamping ---------
        if self.multi_step_coordinator is not None:
            try:
                for step in plan.steps:
                    self.multi_step_coordinator.evaluate_postconditions(step)
                    self.multi_step_coordinator.stamp_world_facts(step)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "MultiStepCoordinator post-dispatch raised: {!r}", exc,
                )
        # ------------------------------------------------------------

        # Find the first failed step → first failure.
        last_failure: Optional[Failure] = None
        for sr in exec_result.step_results:
            if sr.status in (
                StepState.FAILED,
                StepState.TIMED_OUT,
                StepState.CANCELLED,
            ):
                last_failure = self._failure_from_step(sr, plan)
                result = result.with_appended_failure(
                    last_failure, limit=self.policy.failure_history_limit
                )
                break

        return result, exec_result, last_failure

    # --------------------------------------------------------- vision grounding
    def _apply_pre_action_grounding(
        self,
        step: PlanStep,
        plan_id: str,
    ) -> Tuple[Optional[TargetGroundingContract], Optional[Failure]]:
        """Resolve a step's pre-action vision grounding (Phase 7.2).

        Returns ``(contract, None)`` when grounding is *not* required
        (no ``vision_pre_action`` metadata, or ``vision_service`` is
        not configured) — the caller treats this as "skip the vision
        step in the planner".  Returns ``(contract, None)`` when the
        target is *GROUNDED* and the contract can be adapted.

        Returns ``(None, failure)`` when:

          * the target is :data:`AMBIGUOUS` / :data:`NOT_FOUND` /
            :data:`ERROR` (the step cannot proceed — recovery is the
            caller's job);
          * the confidence is below ``self.confidence_threshold``
            (a *safety* failure — vision refused to dispatch).

        The method never raises for a normal grounding miss; the
        contract status alone tells the caller what to do.
        """
        kind = step.metadata.get("vision_pre_action")
        if not kind:
            return TargetGroundingContract.skipped(step.step_id), None

        if self.vision_service is None:
            # Vision is not configured; refuse to dispatch a
            # pre-action step that explicitly requires it.
            failure = make_failure(
                kind=FailureKind.SAFETY,
                plan_id=plan_id,
                step_id=step.step_id,
                message=(
                    f"step {step.step_id} declares vision_pre_action={kind!r} "
                    f"but Agent has no vision_service configured."
                ),
                is_retryable=False,
            )
            return None, failure

        target_query = step.metadata.get("vision_target_query") or step.subject or step.step_id
        preferred = step.metadata.get("vision_preferred_strategy")

        try:
            vision_result = self.vision_service.ground_target(
                target_query,
                preferred_strategy=preferred,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("vision_service.ground_target raised: {!r}", exc)
            failure = make_failure(
                kind=FailureKind.INTERNAL,
                plan_id=plan_id,
                step_id=step.step_id,
                message=f"vision service raised: {exc!r}",
                is_retryable=True,
            )
            return None, failure

        contract = _vision_result_to_contract(
            vision_result, target_query=target_query
        )

        # Confidence gate (safety).
        if (
            contract.is_grounded
            and contract.confidence < self.confidence_threshold
        ):
            failure = make_failure(
                kind=FailureKind.SAFETY,
                plan_id=plan_id,
                step_id=step.step_id,
                message=(
                    f"grounding confidence {contract.confidence:.2f} is below "
                    f"threshold {self.confidence_threshold:.2f} for "
                    f"target={target_query!r}; refusing to dispatch."
                ),
                is_retryable=False,
            )
            return None, failure

        # Blocking statuses (AMBIGUOUS / NOT_FOUND / ERROR / REJECTED).
        if contract.is_blocking:
            failure = make_failure(
                kind=FailureKind.VERIFICATION,
                plan_id=plan_id,
                step_id=step.step_id,
                message=(
                    f"vision grounding for {target_query!r} returned "
                    f"blocking status {contract.status.value!r}: "
                    f"{contract.error or 'no reason given'}"
                ),
                is_retryable=contract.status is GroundingStatus.NOT_FOUND,
            )
            return None, failure

        return contract, None

    # --------------------------------------------------------- observing
    def _observe_and_record(
        self,
        *,
        result: AgentResult,
        plan: Plan,
        exec_result: ExecutionResult,
    ) -> AgentResult:
        """Walk every :class:`StepResult` and produce observations."""
        step_by_id = {s.step_id: s for s in plan.steps}
        for sr in exec_result.step_results:
            step = step_by_id.get(sr.step_id)
            if step is None:
                continue
            try:
                obs = self.observation_provider.observe(step, sr)
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "observation_provider.observe() raised for step {}: {!r}",
                    sr.step_id, exc,
                )
                obs = None
            if obs is None:
                continue
            passed: Optional[bool]
            cap_status = ""
            if (
                sr.capability_result is not None
                and getattr(sr.capability_result, "status", None) is not None
            ):
                cap_status = sr.capability_result.status.value
            if cap_status == "verified":
                passed = True
            elif cap_status in ("failed", "timed_out", "cancelled"):
                passed = False
            else:
                passed = None
            entry = ObservationEntry(
                step_id=sr.step_id,
                summary=(
                    f"step {sr.step_id} → {sr.status.value} "
                    f"(capability_status={cap_status!r})"
                ),
                source=(obs.source.value if obs.source is not None else "derived"),
                passed=passed,
                timestamp=sr.completed_at or time.time(),
                metadata={
                    "observation_confidence": obs.confidence,
                },
            )
            result = result.with_appended_observation(
                entry, limit=self.policy.observation_history_limit
            )
        return result

    # --------------------------------------------------------- evaluating
    def _evaluate(
        self,
        *,
        plan: Plan,
        exec_result: ExecutionResult,
        goal: Goal,
        result: AgentResult,
    ) -> Tuple[VerificationVerdict, Dict[str, VerificationVerdict]]:
        """Run step + goal verifiers.

        Returns ``(goal_verdict, step_verdicts)``.
        """
        step_by_id = {s.step_id: s for s in plan.steps}
        step_verdicts: Dict[str, VerificationVerdict] = {}

        for sr in exec_result.step_results:
            step = step_by_id.get(sr.step_id)
            if step is None or step.expected_effect is None:
                continue
            obs = self._observation_for_step(step, sr)
            # Phase 2: when the step_verifier is a router, route by
            # capability name; when it is a single verifier, call it
            # directly with the same kwargs.  We detect the router
            # by duck-typing ``verify``'s signature instead of an
            # isinstance check so the Agent stays decoupled from
            # the router's import path.
            try:
                if isinstance(self.step_verifier, VerifierRouter):
                    verdict = self.step_verifier.verify(
                        capability_name=step.capability_name,
                        effect=step.expected_effect,
                        observation=obs,
                        before_observation=getattr(
                            sr, "before_observation", None
                        ),
                        context=None,
                    )
                else:
                    verdict = self.step_verifier.verify(
                        effect=step.expected_effect,
                        observation=obs,
                        before_observation=getattr(
                            sr, "before_observation", None
                        ),
                        context=None,
                    )
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "step_verifier raised for {}: {!r}", sr.step_id, exc,
                )
                verdict = failed_verdict(
                    check_name=step.expected_effect.check_name,
                    expected=step.expected_effect.expected,
                    actual=None,
                    reason=f"step verifier raised: {exc!r}",
                )
            step_verdicts[sr.step_id] = verdict
            # System 8: emit a per-step verdict event so the
            # observability layer can show step-by-step status.
            if verdict.failed or verdict.uncertain:
                verdict_kind = "step_failed"
                verdict_str = "failed" if verdict.failed else "uncertain"
            else:
                verdict_kind = "step_verified"
                verdict_str = "passed"
            self._emit(
                verdict_kind,
                plan_id=plan.plan_id,
                step_id=sr.step_id,
                capability_name=step.capability_name,
                verdict=verdict_str,
                reason=str(verdict.reason or ""),
            )

        # Build a synthetic "aggregate" observation for the goal verifier.
        aggregate = self._aggregate_observation(
            goal=goal,
            exec_result=exec_result,
            step_verdicts=step_verdicts,
        )
        try:
            goal_effect = _goal_expected_effect(goal)
            goal_verdict = self.goal_verifier.verify(
                effect=goal_effect,
                observation=aggregate,
                context=None,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("goal_verifier raised: {!r}", exc)
            goal_verdict = failed_verdict(
                check_name="goal",
                expected=None,
                actual=None,
                reason=f"goal verifier raised: {exc!r}",
            )
        return goal_verdict, step_verdicts

    def _observation_for_step(
        self, step: PlanStep, sr: StepResult
    ) -> Optional[Observation]:
        try:
            return self.observation_provider.observe(step, sr)
        except Exception:  # noqa: BLE001
            return None

    def _aggregate_observation(
        self,
        *,
        goal: Goal,
        exec_result: ExecutionResult,
        step_verdicts: Mapping[str, VerificationVerdict],
    ) -> Observation:
        """Build a single DERIVED observation summarising all step verdicts."""
        step_verdicts_payload = []
        for sid, v in step_verdicts.items():
            step_verdicts_payload.append({
                "step_id": sid,
                "status": (
                    "passed" if v.passed else
                    "failed" if v.failed else
                    "uncertain" if v.uncertain else "unknown"
                ),
                "reason": v.reason,
            })
        confidence = 1.0
        if step_verdicts:
            # Phase 2: v.confidence is now guaranteed on every
            # verdict (Phase 1 added the field), so the average is
            # direct.  We use the arithmetic mean so a 0.7
            # partial-confidence verdict combined with a 1.0
            # strict verdict yields 0.85 — the goal verifier can
            # then decide whether that meets the goal threshold.
            confidence = sum(
                v.confidence for v in step_verdicts.values()
            ) / max(1, len(step_verdicts))
        return Observation(
            source=ObservationSource.DERIVED,
            data={
                "step_verdicts": step_verdicts_payload,
                "execution_outcome": exec_result.outcome.value,
            },
            timestamp=time.time(),
            subject=goal.goal_id,
            confidence=confidence,
            metadata={"goal_id": goal.goal_id},
        )

    def _record_evaluations(
        self,
        *,
        result: AgentResult,
        goal_verdict: VerificationVerdict,
        step_verdicts: Mapping[str, VerificationVerdict],
    ) -> AgentResult:
        # Record the goal verdict as an observation entry for the
        # audit log, but only if there is at least one step verdict
        # to summarise.
        if not step_verdicts:
            return result
        if goal_verdict.passed:
            status = "PASSED"
        elif goal_verdict.failed:
            status = "FAILED"
        else:
            status = "UNCERTAIN"
        entry = ObservationEntry(
            step_id="<goal>",
            summary=f"goal verdict: {status}",
            source="derived",
            passed=(
                True if goal_verdict.passed else
                False if goal_verdict.failed else
                None
            ),
            timestamp=time.time(),
            metadata={
                "verifier": self.goal_verifier.name,
                "check_name": goal_verdict.check_name,
            },
        )
        return result.with_appended_observation(
            entry, limit=self.policy.observation_history_limit
        )

    # --------------------------------------------------------- deciding
    def _decide(
        self,
        *,
        result: AgentResult,
        exec_result: ExecutionResult,
        current_failure: Optional[Failure],
        goal_verdict: VerificationVerdict,
        goal: Goal,
        plan: Plan,
    ) -> Optional[RecoveryDecision]:
        """Decide what to do next.

        Returns ``None`` when the run should continue without a
        recovery decision (i.e. the plan completed cleanly and the
        goal is verified, or the run is at a terminal state already).
        """
        # ---- Terminal: plan completed AND goal verified ------------
        if (
            exec_result.outcome is ExecutionOutcome.COMPLETED
            and goal_verdict.passed
        ):
            return None

        # If there is no step failure but the goal was not verified,
        # synthesize a verification failure so recovery can act.
        if current_failure is None and not goal_verdict.passed:
            current_failure = make_failure(
                kind=FailureKind.VERIFICATION,
                plan_id=plan.plan_id,
                step_id=None,
                message=(
                    "goal verification did not pass "
                    f"({goal_verdict.reason or 'no reason given'})"
                ),
                is_retryable=True,
            )

        if current_failure is None:
            return None

        # Increment attempt counter on the recovery engine.
        if (
            hasattr(self.recovery_engine, "record_attempt")
            and current_failure.step_id
        ):
            try:
                self.recovery_engine.record_attempt(current_failure.step_id)
            except Exception:  # noqa: BLE001
                pass

        try:
            decision = self.recovery_engine.decide(
                current_failure,
                context=ExecutionContext(
                    execution_id=result.final_execution_id,
                    goal=goal,
                    plan=plan,
                ),
                history=list(result.decision_history),
                cancellation_token=self.cancellation_token,
            )
        except TypeError:
            # Older recovery engines don't accept the kwarg.
            decision = self.recovery_engine.decide(
                current_failure,
                context=ExecutionContext(
                    execution_id=result.final_execution_id,
                    goal=goal,
                    plan=plan,
                ),
                history=list(result.decision_history),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("recovery_engine.decide() raised: {!r}", exc)
            decision = None

        return decision

    # --------------------------------------------------------- branching
    def _branch(
        self,
        *,
        result: AgentResult,
        decision: Optional[RecoveryDecision],
        current_failure: Optional[Failure],
        goal_verdict: VerificationVerdict,
        goal: Goal,
        intent: Optional[Intent],
        current_plan: Plan,
    ) -> Optional[AgentResult]:
        """Apply a :class:`RecoveryDecision` (or terminal state).

        Returns ``None`` when the loop should keep iterating.
        Returns a *terminal* :class:`AgentResult` when the run has
        ended.
        """
        # Plan completed + goal verified → COMPLETE
        if decision is None and goal_verdict.passed:
            return self._finalize(result, AgentState.COMPLETE, error="")

        # No decision and no failure → done with a final state.
        if decision is None:
            return self._finalize(
                result, AgentState.FAILED,
                error=(
                    current_failure.message
                    if current_failure else "no recovery decision"
                ),
            )

        action = decision.action

        if action is RecoveryAction.GIVE_UP:
            return self._finalize(
                result, AgentState.FAILED,
                error=decision.rationale or "recovery gave up",
            )
        if action is RecoveryAction.ABORT:
            return self._finalize(
                result, AgentState.CANCELLED,
                error=decision.rationale or "recovery aborted",
            )
        if action is RecoveryAction.ASK_USER:
            return self._finalize(
                result, AgentState.CLARIFICATION_REQUIRED,
                error=(
                    decision.ask_user_message
                    or decision.rationale
                    or "user input required"
                ),
            )
        if action is RecoveryAction.SKIP:
            # No way to skip a step in the V6 executor's contract;
            # treat as "fall through and replan" to keep invariants.
            if current_failure is not None and current_failure.step_id:
                self.retry_tracker.record_step_skip(current_failure.step_id)
            return self._replan(
                result=result, goal=goal, intent=intent,
                current_plan=current_plan, failure=current_failure,
                decision=decision,
            )
        if action in (RecoveryAction.RETRY, RecoveryAction.RETRY_WITH_BACKOFF):
            # Phase 3 single-retry path: when the recovery engine
            # has a *specific* step to retry (i.e. the failure has
            # a step_id), call ``plan_executor.execute_step`` for
            # just that step.  When the failure has no step_id
            # (e.g. plan-level failure), fall back to re-executing
            # the whole plan.  Either way, the attempt counter is
            # recorded so the audit log is honest.
            self._transition(AgentState.RECOVER)
            if current_failure is not None and current_failure.step_id:
                self.retry_tracker.record_step_retry(current_failure.step_id)
            if (
                current_failure is not None
                and current_failure.step_id
                and hasattr(self.plan_executor, "execute_step")
            ):
                retried_result = self._retry_single_step(
                    result=result,
                    goal=goal,
                    intent=intent,
                    plan=current_plan,
                    failure=current_failure,
                    decision=decision,
                )
                return retried_result
            self._emit(
                "retry",
                plan_id=current_plan.plan_id,
                failure_id=current_failure.failure_id if current_failure else "",
                backoff_s=decision.backoff_s,
            )
            return None
        if action is RecoveryAction.REPLAN:
            self.retry_tracker.record_replan()
            return self._replan(
                result=result, goal=goal, intent=intent,
                current_plan=current_plan, failure=current_failure,
                decision=decision,
            )

        # Unknown action — bail out safely.
        return self._finalize(
            result, AgentState.FAILED,
            error=f"unknown recovery action: {action!r}",
        )

    def _replan(
        self,
        *,
        result: AgentResult,
        goal: Goal,
        intent: Optional[Intent],
        current_plan: Plan,
        failure: Optional[Failure],
        decision: RecoveryDecision,
    ) -> Optional[AgentResult]:
        """Produce a new plan and continue.

        The previous plan is preserved in ``result.plan_history``
        (R-23 / R-12 invariant: plan history is append-only).
        """
        self._transition(AgentState.REPLAN)
        if hasattr(self.recovery_engine, "record_replan"):
            try:
                self.recovery_engine.record_replan()
            except Exception:  # noqa: BLE001
                pass
        # Inject decision into the last plan history entry for audit.
        new_history: List[PlanHistoryEntry] = list(result.plan_history)
        if new_history:
            last = new_history[-1]
            new_history[-1] = replace(last, decision=decision)
        result = replace(result, plan_history=tuple(new_history))

        result, next_plan = self._plan_once(
            result=result,
            goal=goal,
            intent=intent,
            prior_plan=current_plan,
            failure=failure,
        )
        if next_plan is None:
            return self._finalize(
                result, AgentState.CLARIFICATION_REQUIRED,
                error="replan produced no plan",
            )

        # Stash the next plan for the next loop iteration.
        self._pending_next_plan = next_plan
        # Update last_result so the loop can pick up replan_count
        # changes (e.g. when _replan returns None to continue).
        self._last_result = result
        return None

    def _retry_single_step(
        self,
        *,
        result: AgentResult,
        goal: Goal,
        intent: Optional[Intent],
        plan: Plan,
        failure: Failure,
        decision: RecoveryDecision,
    ) -> Optional[AgentResult]:
        """Phase 3 single-retry path.

        The recovery engine decided ``RETRY`` or
        ``RETRY_WITH_BACKOFF`` for a specific step.  Instead of
        re-executing the whole plan, call
        :meth:`PlanExecutor.execute_step` for just that step.
        Honors ``decision.backoff_s`` when the engine asked for
        a pause (a no-op in tests, but production code wires it
        up via the cancellation token or a sleep helper).
        """
        from .execution_result import (
            ExecutionOutcome,
            new_correlation_id,
        )

        assert failure.step_id, "_retry_single_step requires failure.step_id"
        step = plan.find_step(failure.step_id)
        if step is None:
            # Step vanished from the plan; nothing to retry.
            return None

        # Honor the backoff (a sleep, but bounded).
        backoff = float(decision.backoff_s or 0.0)
        if backoff > 0.0 and backoff < 10.0:
            time.sleep(backoff)

        # Mark the attempt on the recovery engine so the audit log
        # sees it.  Use the step_id the failure is for.
        if hasattr(self.recovery_engine, "record_attempt"):
            try:
                self.recovery_engine.record_attempt(failure.step_id)
            except Exception:  # noqa: BLE001
                pass

        # Emit a structured retry event so observability knows
        # what happened.
        self._emit(
            "retry",
            plan_id=plan.plan_id,
            step_id=failure.step_id,
            failure_id=failure.failure_id,
            backoff_s=backoff,
        )

        # Build a minimal execution context for the single step.
        ctx = ExecutionContext(
            execution_id=f"{result.agent_run_id}:retry:{failure.step_id}",
            goal=goal,
            plan=plan,
            intent=intent,
            started_at=time.time(),
            cancellation_token=self.cancellation_token,
        )
        try:
            step_result = self.plan_executor.execute_step(ctx, step)
        except Exception as exc:  # noqa: BLE001
            logger.warning("execute_step raised during retry: {!r}", exc)
            return None

        if step_result.status in (
            StepState.SUCCEEDED,
        ):
            # The retry succeeded.  Cache the StepResult so the
            # main loop's next iteration can build a synthetic
            # ExecutionResult and skip re-running the whole plan
            # (which would re-dispatch this very step a third
            # time).  Returning None tells the loop to continue;
            # the cached step result is consumed at the top of
            # the next iteration.
            self._pending_step_results = (step_result,)
            return None

        # The retry itself failed.  Cache the failed step result
        # so the main loop's next iteration does NOT re-execute
        # the whole plan (which would burn another call on the
        # already-failing step).  Instead, the loop builds a
        # synthetic ExecutionResult and re-decides on this step
        # alone, letting the recovery engine enforce its budget
        # (max_step_retries) without wasting calls on a re-plan
        # roundtrip.
        self._pending_step_results = (step_result,)
        # Returning None here means the loop iterates again with
        # the synthetic result.  ``_last_result`` is updated so
        # the loop picks up the new failure we are about to
        # append below.
        # We will append the new failure after the loop has had a
        # chance to build the synthetic result; the loop's pending
        # path will see the failure in ``result.failures`` and
        # pass it to the next ``_decide`` call.
        new_failure = make_failure(
            kind=(
                self.failure_classifier.classify(
                    step_result.capability_result
                )
                if step_result.capability_result is not None
                and self.failure_classifier is not None
                else FailureKind.EXECUTION
            ),
            step_id=failure.step_id,
            plan_id=plan.plan_id,
            message=(
                step_result.error
                or f"step {failure.step_id!r} failed again on retry"
            ),
            attempt=int(getattr(self.recovery_engine, "attempts_for", lambda _: 0)(
                failure.step_id
            ) or 1),
            is_retryable=True,
        )
        result = result.with_appended_failure(
            new_failure, limit=self.policy.failure_history_limit
        )
        self._last_result = result
        return None

    def _build_synthetic_exec_result(
        self,
        *,
        plan: Plan,
        step_results: Tuple["StepResult", ...],
    ) -> "ExecutionResult":
        """Build a synthetic :class:`ExecutionResult` for the
        post-retry path.

        After :meth:`_retry_single_step` succeeds, the main loop
        consumes the cached step result via this helper.  The
        synthetic result lets the observing/evaluating/deciding
        code path run end-to-end without re-executing the plan.
        """
        from .execution_result import (
            ExecutionOutcome,
            new_correlation_id,
        )
        all_ok = all(sr.ok for sr in step_results)
        outcome = (
            ExecutionOutcome.COMPLETED
            if all_ok
            else ExecutionOutcome.PARTIAL
        )
        return ExecutionResult(
            execution_id=f"{plan.plan_id}:retry",
            plan_id=plan.plan_id,
            goal_id=plan.goal_id,
            outcome=outcome,
            step_results=tuple(step_results),
            started_at=time.time(),
            completed_at=time.time(),
            duration_ms=0.0,
            correlation_id=new_correlation_id(),
        )

    # --------------------------------------------------------- helpers
    def _runtime_exceeded(self, result: AgentResult) -> bool:
        if not result.started_at:
            return False
        if self.policy.max_total_runtime_s <= 0:
            return False
        return (time.time() - result.started_at) > self.policy.max_total_runtime_s

    def _failure_from_step(
        self, sr: StepResult, plan: Plan
    ) -> Failure:
        kind = FailureKind.EXECUTION
        if sr.status is StepState.TIMED_OUT:
            kind = FailureKind.TIMEOUT
        elif sr.status is StepState.CANCELLED:
            kind = FailureKind.CANCELLED
        # Phase 3: if the step's CapabilityResult carries a
        # recognisable error code (TARGET_NOT_FOUND, FOCUS_FAILED,
        # WINDOW_NOT_READY, STALE_TARGET, PROVIDER_FAILURE,
        # PERMISSION_FAILURE), promote the kind to the matching
        # FailureKind.  The classifier is the single place that
        # reads error codes; this method just consults it.
        if sr.capability_result is not None and self.failure_classifier is not None:
            try:
                classified = self.failure_classifier.classify(sr.capability_result)
                if classified is not None:
                    kind = classified
            except Exception:  # noqa: BLE001
                # Classifier is best-effort; fall back to EXECUTION.
                pass
        # Phase 1 / D4: read the attempt count from the recovery
        # engine rather than hardcoding 1.  The engine tracks
        # per-step attempt history; the Failure audit log should
        # reflect the actual attempt number so recovery decisions
        # can reason about budget correctly.
        attempt = 1
        if self.recovery_engine is not None and \
                hasattr(self.recovery_engine, "attempts_for"):
            try:
                attempt = max(
                    1, int(self.recovery_engine.attempts_for(sr.step_id))
                )
            except Exception:  # noqa: BLE001
                attempt = 1
        return make_failure(
            kind=kind,
            step_id=sr.step_id,
            plan_id=plan.plan_id,
            message=sr.error or f"step status {sr.status.value}",
            cause=str(sr.capability_result.error) if (
                sr.capability_result is not None
                and getattr(sr.capability_result, "error", None) is not None
            ) else None,
            attempt=attempt,
            is_retryable=True,
        )

    def _finalize(
        self,
        result: AgentResult,
        state: AgentState,
        *,
        error: str = "",
    ) -> AgentResult:
        self._transition(state)
        result = result.with_final_state(
            state, completed_at=time.time(), error=error or result.error,
        )
        self._last_result = result
        self._emit(
            "agent_finished",
            final_state=state.value,
            completed=result.completed,
            failed=result.failed,
            attempts=result.attempts,
            replans=result.replans,
        )
        return result

    def _transition(self, state: AgentState) -> None:
        prev = self._state
        self._state = state
        if prev is not state:
            self._emit(
                "agent_state_transition",
                previous=prev.value,
                next_=state.value,
            )

    def _emit(self, kind: str, **payload: Any) -> None:
        # 1. Free-text observability sink (preserved for backwards
        #    compatibility with Phase 6C callers).
        if self.observability_sink is not None:
            try:
                self.observability_sink(kind, payload)
            except Exception:  # noqa: BLE001
                # A bad sink must never break the Agent.
                logger.debug("observability_sink raised for event {}", kind)

        # 2. Typed progress broadcaster (System 8).  When wired,
        #    forward the same event as a :class:`ProgressEvent`
        #    with a structured :class:`ProgressPhase`.
        if self.progress_broadcaster is not None:
            try:
                phase = _EMIT_KIND_TO_PROGRESS_PHASE.get(kind, ProgressPhase.INFO)
                # The terminal ``agent_finished`` event carries the
                # final state in its payload; remap to the right
                # terminal phase.
                if kind == "agent_finished":
                    fs = str(payload.get("final_state", "") or "")
                    phase = _TERMINAL_STATE_TO_PROGRESS_PHASE.get(fs, phase)
                step_id = str(payload.get("step_id", "") or "")
                plan_id = str(payload.get("plan_id", "") or "")
                attempt = int(payload.get("attempt", 0) or 0)
                correlation_id = str(payload.get("correlation_id", "") or "")
                msg = str(
                    payload.get("message")
                    or payload.get("description")
                    or step_id
                    or plan_id
                    or kind
                )
                ev: ProgressEvent = make_progress_event(
                    phase,
                    plan_id=plan_id,
                    step_id=step_id,
                    attempt=attempt,
                    correlation_id=correlation_id,
                    message=msg,
                    details={k: v for k, v in payload.items() if k not in {"step_id", "plan_id", "attempt", "correlation_id", "message", "description"}},
                )
                self.progress_broadcaster.publish(ev)
            except Exception:  # noqa: BLE001
                # A bad broadcaster must never break the Agent.
                logger.debug("progress_broadcaster.publish raised for {}", kind)

    # ============================================================== stats
    def statistics(self) -> Dict[str, Any]:
        return {
            "name": self._name,
            "state": self._state.value,
            "policy": {
                "max_iterations": self.policy.max_iterations,
                "max_total_runtime_s": self.policy.max_total_runtime_s,
            },
            "interpreter": getattr(self.interpreter, "name", "?"),
            "planner": getattr(self.planner, "name", "?"),
            "plan_executor": getattr(self.plan_executor, "name", "?"),
            "recovery_engine": getattr(self.recovery_engine, "name", "?"),
            "step_verifier": getattr(self.step_verifier, "name", "?"),
            "goal_verifier": getattr(self.goal_verifier, "name", "?"),
            "observation_provider": getattr(self.observation_provider, "name", "?"),
        }

    def __repr__(self) -> str:
        return f"Agent(state={self._state.value!r})"


# ===========================================================================
# Small helpers
# ===========================================================================

def _goal_expected_effect(goal: Goal) -> ExpectedEffect:
    """Build an :class:`ExpectedEffect` that names the goal's success criteria."""
    return ExpectedEffect(
        check_name="goal",
        expected=list(goal.success_criteria or ()),
        description=goal.description,
    )


# Status strings the :class:`core.services.vision_service.VisionService`
# can return.  Kept here as a private mapping so the Agent never
# imports the vision service module directly (it is duck-typed via
# ``Any`` in :class:`Agent.__init__`).
_VISION_STATUS_TO_GROUNDING = {
    "OBSERVED": GroundingStatus.GROUNDED,
    "AMBIGUOUS": GroundingStatus.AMBIGUOUS,
    "NOT_FOUND": GroundingStatus.NOT_FOUND,
    "ERROR": GroundingStatus.ERROR,
}


def _vision_result_to_contract(
    vision_result: Any,
    *,
    target_query: str,
) -> TargetGroundingContract:
    """Translate a :class:`VisionResult` into a :class:`TargetGroundingContract`.

    The Agent never inspects vision objects; it only consumes the
    typed contract.  This function is the *only* place that does
    the translation, so a future schema change in
    :class:`VisionResult` is contained.
    """
    status_str = getattr(vision_result, "status", "ERROR")
    status = _VISION_STATUS_TO_GROUNDING.get(status_str, GroundingStatus.ERROR)

    observation = getattr(vision_result, "observation", None) or {}
    bbox = observation.get("bbox") if isinstance(observation, dict) else None
    confidence = float(observation.get("confidence", 0.0)) if isinstance(observation, dict) else 0.0
    source_str = observation.get("source") if isinstance(observation, dict) else None

    source: Optional[ObservationSource] = None
    if source_str:
        try:
            source = ObservationSource(source_str)
        except (ValueError, TypeError):
            source = None

    # Pre-compute center so the adapter does not have to.
    center: Optional[Tuple[int, int]] = None
    if bbox is not None and len(bbox) == 4:
        try:
            l, t, r, b = (int(v) for v in bbox)
            center = ((l + r) // 2, (t + b) // 2)
            bbox = (l, t, r, b)
        except (TypeError, ValueError):
            bbox = None
            center = None

    text = observation.get("text", "") if isinstance(observation, dict) else ""
    resolution_method = getattr(vision_result, "resolution_method", "") or ""
    error = getattr(vision_result, "error", None) or ""

    candidates_payload = []
    if status is GroundingStatus.AMBIGUOUS and isinstance(observation, dict):
        for c in observation.get("candidates", ()) or ():
            if isinstance(c, dict):
                candidates_payload.append(dict(c))

    return TargetGroundingContract(
        status=status,
        target_query=target_query,
        bbox=bbox,
        center=center,
        confidence=confidence,
        source=source,
        text=text if isinstance(text, str) else "",
        resolution_method=resolution_method,
        candidates=tuple(candidates_payload),
        error=error,
        metadata={},
    )


__all__ = [
    "Agent",
    "AgentPolicy",
]
