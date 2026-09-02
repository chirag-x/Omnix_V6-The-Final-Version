"""
Omnix V6 — Phase 4 deterministic failure flow test.

This test exercises the recovery loop in a fully deterministic way:

    Plan (v1)
      → Step fails
        → Failure
          → RecoveryDecision(REPLAN)
            → Plan (v2)

The failure is simulated by giving the router a capability that
returns FAILED the first time and succeeds the second.  The
"recovery engine" is a tiny stub that emits a REPLAN decision; the
"planner" is a stub that, on a REPLAN with a failure context,
returns a one-step replacement plan.  No real subsystem is touched.
"""

import time
import pytest

from core.orchestration import (
    Goal,
    Plan,
    PlanStep,
    PlanStatus,
    ActionRequest,
    ExecutionContext,
    ExpectedEffect,
    Failure,
    FailureKind,
    RecoveryDecision,
    RecoveryAction,
)

from core.capability import CapabilitySpec, CapabilityParameter, ParamType
from core.capability_registry import CapabilityRegistry
from core.capability_router import CapabilityRouter, AllowAllSafetyPolicy
from core.results import (
    ActionResult,
    ActionStatus,
    CapabilityResult,
    CapabilityStatus,
)


# ---------------------------------------------------------------------------
# A capability whose behavior toggles between FAILED and EXECUTED.
# ---------------------------------------------------------------------------

class _ToggleCapability:
    """Succeeds every other call; alternates per instance.

    Each call flips ``self._next_ok``.  Tests that want deterministic
    behavior use ``set_next_ok(True/False)`` to control the next
    call.
    """

    spec = CapabilitySpec(
        name="test.toggle",
        version="1.0.0",
        description="Returns success on the second call, failure on the first.",
        parameters=(
            CapabilityParameter(name="x", type=ParamType.STRING, required=False),
        ),
    )

    def __init__(self):
        self._next_ok = False
        self.calls = 0

    def set_next_ok(self, ok: bool) -> None:
        self._next_ok = ok

    def is_available(self) -> bool:
        return True

    def execute(self, params):
        self.calls += 1
        ok = self._next_ok
        # Always flip after consulting
        self._next_ok = not ok
        if ok:
            return CapabilityResult(
                capability_name="test.toggle",
                status=CapabilityStatus.VERIFIED,
                attempted=True,
                executed=True,
                verified=True,
                failed=False,
                action=ActionResult(
                    status=ActionStatus.EXECUTED,
                    action_name="test.toggle",
                    details={"params": dict(params)},
                ),
            )
        return CapabilityResult(
            capability_name="test.toggle",
            status=CapabilityStatus.FAILED,
            attempted=True,
            executed=True,
            verified=False,
            failed=True,
            action=ActionResult(
                status=ActionStatus.FAILED,
                action_name="test.toggle",
                details={"reason": "simulated failure"},
            ),
            error=Exception("simulated failure"),
        )


@pytest.fixture
def toggle():
    return _ToggleCapability()


@pytest.fixture
def router(toggle):
    reg = CapabilityRegistry()
    reg.register(toggle)
    return CapabilityRouter(reg, safety_policy=AllowAllSafetyPolicy())


# ---------------------------------------------------------------------------
# The recovery flow
# ---------------------------------------------------------------------------

class TestFailureAndRecoveryFlow:
    def test_plan_v1_step_fails_then_replan_to_v2_succeeds(
        self, router, toggle
    ):
        goal = Goal(goal_id="g_rec", description="toggle a value")
        step_v1 = PlanStep(
            step_id="s1",
            description="toggle (will fail first time)",
            capability_name="test.toggle",
            parameters={"x": "1"},
            expected_effect=ExpectedEffect(check_name="toggle_ok", expected=True),
            max_retries=0,  # so the failure surfaces directly
        )
        plan_v1 = Plan(plan_id="p1", goal_id=goal.goal_id, steps=(step_v1,))

        intent = None  # not relevant for the failure flow
        ctx = ExecutionContext(
            execution_id="e_rec",
            goal=goal,
            plan=plan_v1,
            intent=intent,
            current_step_id=step_v1.step_id,
            started_at=time.time(),
        )

        # ---- 1. First call: failure --------------------------------
        toggle.set_next_ok(False)
        result_v1 = router.route(step_v1.capability_name, dict(step_v1.parameters))
        assert result_v1.failed is True
        assert result_v1.status is CapabilityStatus.FAILED
        assert result_v1.action.status is ActionStatus.FAILED

        # ---- 2. Build a Failure from the result --------------------
        failure = Failure(
            failure_id="f1",
            kind=FailureKind.EXECUTION,
            step_id=step_v1.step_id,
            plan_id=plan_v1.plan_id,
            message=result_v1.action.details.get("reason", ""),
            cause=str(result_v1.error) if result_v1.error else None,
            attempt=1,
            is_retryable=True,
        )
        assert failure.kind is FailureKind.EXECUTION
        assert failure.step_id == "s1"

        # Mark the step as failed in the context.
        ctx_after_failure = ctx.with_failed(step_v1.step_id)
        assert ctx_after_failure.failed_step_ids == ("s1",)
        assert ctx_after_failure.completed_step_ids == ()

        # ---- 3. Recovery engine decides REPLAN ---------------------
        # A deterministic recovery engine: if the failure is a
        # plain execution failure, REPLAN.  Otherwise GIVE_UP.
        def decide(failure: Failure, context: ExecutionContext) -> RecoveryDecision:
            if failure.kind is FailureKind.EXECUTION and failure.is_retryable:
                return RecoveryDecision(
                    decision_id="d1",
                    action=RecoveryAction.REPLAN,
                    failure_id=failure.failure_id,
                    rationale="transient execution failure; replan with a single retry step",
                )
            return RecoveryDecision(
                decision_id="d2",
                action=RecoveryAction.GIVE_UP,
                failure_id=failure.failure_id,
                rationale="non-retryable failure",
            )

        decision = decide(failure, ctx_after_failure)
        assert decision.action is RecoveryAction.REPLAN
        assert decision.failure_id == failure.failure_id

        # ---- 4. Planner produces Plan v2 ---------------------------
        # The replanned plan keeps the same goal but adds a retry
        # step.  The replan_count is incremented.
        retry_step = PlanStep(
            step_id="s1_retry",
            description="retry the toggle",
            capability_name="test.toggle",
            parameters={"x": "1"},
            expected_effect=ExpectedEffect(check_name="toggle_ok", expected=True),
            depends_on=(step_v1.step_id,),
        )
        plan_v2 = Plan(
            plan_id="p2",
            goal_id=goal.goal_id,
            steps=(step_v1, retry_step),
            replan_count=plan_v1.replan_count + 1,
            parent_plan_id=plan_v1.plan_id,
            notes="replan after execution failure",
        )
        assert plan_v2.replan_count == 1
        assert plan_v2.parent_plan_id == "p1"
        assert plan_v2.step_count == 2
        assert plan_v2.find_step("s1_retry") is not None

        # ---- 5. Plan v2 is dispatched; second call succeeds -------
        ctx_v2 = ExecutionContext(
            execution_id="e_rec_v2",
            goal=goal,
            plan=plan_v2,
            current_step_id=retry_step.step_id,
            started_at=time.time(),
        )
        toggle.set_next_ok(True)
        result_v2 = router.route(
            retry_step.capability_name, dict(retry_step.parameters)
        )
        assert result_v2.ok, f"second call must succeed: {result_v2}"
        assert result_v2.status is CapabilityStatus.VERIFIED

        # Mark the retry step as completed in v2 context.
        ctx_v2_done = ctx_v2.with_completed(retry_step.step_id)
        assert ctx_v2_done.completed_step_ids == ("s1_retry",)
        assert ctx_v2_done.failed_step_ids == ()

        # The terminal plan is COMPLETED.
        terminal_plan = plan_v2.with_status(PlanStatus.COMPLETED)
        assert terminal_plan.status is PlanStatus.COMPLETED

    def test_non_retryable_failure_yields_give_up(self):
        # No router needed: the failure is the input.
        failure = Failure(
            failure_id="f2",
            kind=FailureKind.SAFETY,  # safety refusals are non-retryable
            message="user is not authorized",
            is_retryable=False,
        )
        assert failure.is_retryable is False
        assert failure.kind is FailureKind.SAFETY

        def decide(failure: Failure, context: ExecutionContext) -> RecoveryDecision:
            if failure.is_retryable:
                return RecoveryDecision(
                    decision_id="d1",
                    action=RecoveryAction.REPLAN,
                    failure_id=failure.failure_id,
                )
            return RecoveryDecision(
                decision_id="d2",
                action=RecoveryAction.GIVE_UP,
                failure_id=failure.failure_id,
                rationale="safety refusal cannot be retried",
            )

        ctx = ExecutionContext(
            execution_id="e_safety",
            goal=Goal(goal_id="g", description="x"),
            plan=Plan(plan_id="p", goal_id="g"),
        )
        d = decide(failure, ctx)
        assert d.action is RecoveryAction.GIVE_UP

    def test_ask_user_decision_carries_message(self):
        failure = Failure(
            failure_id="f3",
            kind=FailureKind.VERIFICATION,
            message="expected chrome, got edge",
        )

        def decide(failure: Failure, context: ExecutionContext) -> RecoveryDecision:
            return RecoveryDecision(
                decision_id="d3",
                action=RecoveryAction.ASK_USER,
                failure_id=failure.failure_id,
                ask_user_message=(
                    "I expected to see Chrome but I see Edge. "
                    "Should I continue with Edge or stop?"
                ),
                rationale="verification mismatch; user must decide",
            )

        ctx = ExecutionContext(
            execution_id="e_ask",
            goal=Goal(goal_id="g", description="x"),
            plan=Plan(plan_id="p", goal_id="g"),
        )
        d = decide(failure, ctx)
        assert d.action is RecoveryAction.ASK_USER
        assert "Chrome" in d.ask_user_message


# ---------------------------------------------------------------------------
# A combined flow: failure → replan → second failure → ask_user
# ---------------------------------------------------------------------------

class TestCascadingRecovery:
    """Two failures in a row exercise the *history* dimension of recovery."""

    def test_two_failures_in_history_ask_user(self):
        goal = Goal(goal_id="g_c", description="x")
        plan = Plan(
            plan_id="p1", goal_id=goal.goal_id,
            steps=(PlanStep(step_id="s1", description="x", capability_name="x"),),
        )
        ctx = ExecutionContext(
            execution_id="e", goal=goal, plan=plan, current_step_id="s1",
        )

        history: list = []

        def decide(failure, context, history=None):
            history = history or []
            # After two replans, ask the user.
            if len(history) >= 1:
                return RecoveryDecision(
                    decision_id="d2",
                    action=RecoveryAction.ASK_USER,
                    failure_id=failure.failure_id,
                    ask_user_message="I've failed twice; should I continue?",
                )
            return RecoveryDecision(
                decision_id="d1",
                action=RecoveryAction.REPLAN,
                failure_id=failure.failure_id,
            )

        # First failure → REPLAN
        f1 = Failure(failure_id="f1", kind=FailureKind.EXECUTION, is_retryable=True)
        d1 = decide(f1, ctx, history=history)
        assert d1.action is RecoveryAction.REPLAN
        history.append(d1)

        # Second failure (with the first decision in history) → ASK_USER
        f2 = Failure(failure_id="f2", kind=FailureKind.EXECUTION, is_retryable=True)
        d2 = decide(f2, ctx, history=history)
        assert d2.action is RecoveryAction.ASK_USER
