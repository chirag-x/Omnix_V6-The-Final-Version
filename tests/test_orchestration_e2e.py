"""
Omnix V6 — Phase 4 deterministic end-to-end test.

This test proves the orchestration foundation composes correctly
with the existing capability layer (Phase 1/3) in a fully
deterministic flow:

    Goal
      → Plan (one PlanStep)
        → ActionRequest
          → CapabilityRouter.route()
            → CapabilityResult
              → ExecutionContext (updated)

The capability used is a tiny in-test fake (no OS, no LLM, no
subprocess).  The test fails if any link in the chain cannot be
constructed, or if the ActionRequest's safety check blocks the
intent, or if the resulting CapabilityResult does not feed back
into the ExecutionContext correctly.
"""

import pytest
import time

from core.orchestration import (
    Goal,
    Intent,
    IntentKind,
    Plan,
    PlanStep,
    PlanStatus,
    ActionRequest,
    ActionKind,
    ExecutionContext,
    ExpectedEffect,
    Observation,
    ObservationSource,
)

from core.capability import CapabilitySpec, CapabilityParameter, ParamType
from core.capability_registry import CapabilityRegistry
from core.capability_router import CapabilityRouter, AllowAllSafetyPolicy
from core.results import (
    ActionResult,
    ActionStatus,
    CapabilityResult,
    CapabilityStatus,
    VerificationResult,
    VerificationStatus,
)


# ---------------------------------------------------------------------------
# A tiny in-test capability: a "no-op echo" that returns the input.
# ---------------------------------------------------------------------------

class _EchoCapability:
    """A minimal Capability that echoes its input back as the action result.

    Registered in the registry below; not used outside the test.
    """

    spec = CapabilitySpec(
        name="test.echo",
        version="1.0.0",
        description="Echo the input parameters as a structured result.",
        parameters=(
            CapabilityParameter(
                name="text", type=ParamType.STRING, required=True,
            ),
        ),
    )

    def is_available(self) -> bool:
        return True

    def execute(self, params):
        text = params.get("text", "")
        action = ActionResult(
            status=ActionStatus.EXECUTED,
            action_name="test.echo",
            details={"echoed": text},
        )
        verification = VerificationResult(
            status=VerificationStatus.VERIFIED,
            check_name="echo_ok",
            expected=text,
            actual=text,
        )
        return CapabilityResult(
            capability_name="test.echo",
            status=CapabilityStatus.VERIFIED,
            attempted=True,
            executed=True,
            verified=True,
            failed=False,
            action=action,
            verification=verification,
            details={"echoed": text},
        )


@pytest.fixture
def registry_with_echo() -> CapabilityRegistry:
    reg = CapabilityRegistry()
    reg.register(_EchoCapability())
    return reg


@pytest.fixture
def router(registry_with_echo) -> CapabilityRouter:
    return CapabilityRouter(
        registry_with_echo,
        safety_policy=AllowAllSafetyPolicy(),
    )


# ---------------------------------------------------------------------------
# The deterministic end-to-end flow
# ---------------------------------------------------------------------------

class TestGoalToExecutionContext:
    def test_full_flow_goal_to_capability_to_context(
        self, router
    ):
        # 1. Goal
        goal = Goal(
            goal_id="g_e2e",
            description="Echo a hello through the orchestration layer",
            success_criteria=("echoed_hello",),
        )

        # 2. Plan
        step = PlanStep(
            step_id="s_echo",
            description="Echo 'hello' via the test.echo capability",
            capability_name="test.echo",
            parameters={"text": "hello"},
            expected_effect=ExpectedEffect(
                check_name="echo_ok",
                expected="hello",
            ),
        )
        plan = Plan(plan_id="p_e2e", goal_id=goal.goal_id, steps=(step,))

        # 3. Intent
        intent = Intent(
            intent_id="i_e2e",
            kind=IntentKind.COMMAND,
            text="echo hello",
            confidence=0.95,
        )

        # 4. ExecutionContext (initial)
        ctx = ExecutionContext(
            execution_id="e_e2e",
            goal=goal,
            plan=plan,
            intent=intent,
            current_step_id=step.step_id,
            started_at=time.time(),
        )
        assert ctx.progress == 0.0
        assert ctx.completed_step_ids == ()

        # 5. The plan is a PlanStep.  Turn it into an ActionRequest
        #    (this is what a real PlanExecutor does).
        ar = ActionRequest(
            capability_name=step.capability_name,
            parameters=dict(step.parameters),
            expected_effect=step.expected_effect,
            request_id="ar_e2e",
        )
        assert ar.capability_name == "test.echo"
        assert ar.parameters == {"text": "hello"}

        # 6. Route the ActionRequest through the CapabilityRouter.
        result = router.route(ar.capability_name, ar.parameters)
        assert result.ok, f"router did not produce ok result: {result}"
        assert result.status is CapabilityStatus.VERIFIED
        assert result.attempted and result.executed and result.verified
        assert not result.failed
        assert result.action is not None
        assert result.action.status is ActionStatus.EXECUTED
        assert result.action.details["echoed"] == "hello"
        assert result.verification is not None
        assert result.verification.verified

        # 7. Synthesize an Observation from the result so the
        #    ExecutionContext can record completion.
        observation = Observation(
            source=ObservationSource.DERIVED,
            data=result.to_dict(),
            subject=result.capability_name,
            timestamp=time.time(),
        )
        assert observation.data["capability_name"] == "test.echo"

        # 8. Commit completion to the ExecutionContext.
        ctx_after = ctx.with_completed(step.step_id)
        assert ctx_after.completed_step_ids == ("s_echo",)
        assert ctx_after.progress == 1.0
        # The original is unchanged (immutability).
        assert ctx.completed_step_ids == ()

        # 9. The Plan can be marked COMPLETED at the end.
        completed_plan = plan.with_status(PlanStatus.COMPLETED)
        assert completed_plan.status is PlanStatus.COMPLETED
        # ...and a fresh context with the completed plan can be
        # reconstructed from scratch.
        ctx_terminal = ExecutionContext(
            execution_id="e_e2e_terminal",
            goal=goal,
            plan=completed_plan,
            intent=intent,
            current_step_id=None,
            completed_step_ids=(step.step_id,),
            started_at=ctx.started_at,
        )
        assert ctx_terminal.plan.status is PlanStatus.COMPLETED
        assert ctx_terminal.progress == 1.0


# ---------------------------------------------------------------------------
# Variants that must also hold
# ---------------------------------------------------------------------------

class TestRouterRejectsUnknownCapability:
    """The Router is the gatekeeper (R-21).  An unknown name must
    produce a SKIPPED result, not execute."""

    def test_unknown_capability_is_skipped(self, router):
        result = router.route("does.not.exist", {"x": 1})
        assert result.status is CapabilityStatus.SKIPPED
        assert not result.ok
        assert result.error is not None

    def test_unknown_capability_does_not_affect_plan(
        self, router
    ):
        # The plan still exists; the unknown name surfaces as a
        # failure result, not a crash.
        plan = Plan(
            plan_id="p_unknown",
            goal_id="g",
            steps=(PlanStep(
                step_id="s1",
                description="x",
                capability_name="does.not.exist",
            ),),
        )
        result = router.route(plan.steps[0].capability_name, {})
        assert result.status is CapabilityStatus.SKIPPED
        # The plan is still iterable; the failure is at the *action*
        # level, not the *plan* level.
        assert plan.step_count == 1


class TestParameterValidation:
    def test_missing_required_parameter_is_rejected(self, router):
        # The echo capability requires ``text``.  Without it, the
        # router returns SKIPPED.
        result = router.route("test.echo", {})
        assert result.status is CapabilityStatus.SKIPPED
        assert not result.ok
        assert result.error is not None
