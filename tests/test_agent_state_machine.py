"""
Omnix V6 — Phase 6C Agent Orchestrator state machine tests.

These tests pin the *outer* state machine of the Agent: which
transitions are legal, which states are terminal, and what the
final result must look like for each terminal state.  They use
deterministic stub collaborators so no I/O is performed.
"""

from __future__ import annotations

import time
import pytest

from core.orchestration import (
    Agent,
    AgentPolicy,
    AgentState,
    AgentResult,
    DefaultRecoveryEngine,
    DefaultStepVerifier,
    DefaultGoalVerifier,
    CapabilityResultObservationProvider,
    ExecutionContext,
    ExecutionOutcome,
    ExpectedEffect,
    Failure,
    FailureKind,
    Goal,
    Intent,
    IntentKind,
    Plan,
    PlanStep,
    PlanStatus,
    StepResult,
    StepState,
    make_blank_execution_result,
    make_failure,
    RecoveryPolicy,
    Observation,
    ObservationSource,
    VerificationVerdict,
    passed_verdict,
    failed_verdict,
    uncertain_verdict,
)


# ---------------------------------------------------------------------------
# Stub collaborators (no LLM, no OS, no subprocess)
# ---------------------------------------------------------------------------

class _StaticInterpreter:
    name: str = "static-interpreter"

    def interpret(self, text, *, context_snapshot=None):
        return Intent(
            intent_id=f"i-{text}",
            kind=IntentKind.COMMAND,
            text=text,
            confidence=1.0,
        )


class _StaticPlanner:
    """Returns a single-step plan; replans add one retry step."""

    name: str = "static-planner"

    def __init__(self, *, succeed: bool = True) -> None:
        self.succeed = succeed
        self.call_count = 0

    def plan(
        self,
        goal,
        *,
        intent=None,
        context_snapshot=None,
        prior_plan=None,
        failure=None,
    ):
        self.call_count += 1
        # First plan: a single step that runs the test.echo capability.
        if prior_plan is None:
            step = PlanStep(
                step_id="s1",
                description="static step 1",
                capability_name="test.echo",
                parameters={"text": "hello"},
                expected_effect=ExpectedEffect(
                    check_name="echo_ok", expected="hello"
                ),
            )
            return Plan(plan_id="p1", goal_id=goal.goal_id, steps=(step,))
        # Replan: keep the original step and add a retry.
        retry = PlanStep(
            step_id="s1_retry",
            description="static step 1 retry",
            capability_name="test.echo",
            parameters={"text": "hello"},
            expected_effect=ExpectedEffect(
                check_name="echo_ok", expected="hello"
            ),
            depends_on=("s1",),
        )
        return Plan(
            plan_id=f"p{prior_plan.replan_count + 2}",
            goal_id=goal.goal_id,
            steps=(*prior_plan.steps, retry),
            replan_count=prior_plan.replan_count + 1,
            parent_plan_id=prior_plan.plan_id,
        )


class _StaticExecutor:
    """Always returns a clean ExecutionResult unless ``fail`` is set."""

    name: str = "static-executor"

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.call_count = 0

    def _make_verified_capability_result(self, step_id: str):
        from core.results import (
            ActionResult,
            ActionStatus,
            CapabilityResult,
            CapabilityStatus,
            VerificationResult,
            VerificationStatus,
        )
        return CapabilityResult(
            capability_name="test.echo",
            status=CapabilityStatus.VERIFIED,
            attempted=True,
            executed=True,
            verified=True,
            failed=False,
            action=ActionResult(
                status=ActionStatus.EXECUTED,
                action_name="test.echo",
                details={"echoed": "hello"},
            ),
            verification=VerificationResult(
                status=VerificationStatus.VERIFIED,
                check_name="echo_ok",
                expected="hello",
                actual="hello",
            ),
        )

    def _make_failed_capability_result(self, step_id: str, error: str):
        from core.results import (
            ActionResult,
            ActionStatus,
            CapabilityResult,
            CapabilityStatus,
        )
        return CapabilityResult(
            capability_name="test.echo",
            status=CapabilityStatus.FAILED,
            attempted=True,
            executed=True,
            verified=False,
            failed=True,
            action=ActionResult(
                status=ActionStatus.FAILED,
                action_name="test.echo",
                details={"reason": error},
            ),
            error=Exception(error),
        )

    def execute(self, context):
        self.call_count += 1
        exec_result = make_blank_execution_result(
            execution_id=context.execution_id,
            plan_id=context.plan.plan_id,
            goal_id=context.goal.goal_id,
        )
        if self.fail:
            cap = self._make_failed_capability_result("s1", "simulated failure")
            sr = StepResult(
                step_id="s1",
                capability_name="test.echo",
                status=StepState.FAILED,
                capability_result=cap,
                error="simulated failure",
            )
            exec_result = exec_result.with_step_result(sr)
            exec_result = exec_result.with_outcome(
                ExecutionOutcome.FAILED,
                completed_at=time.time(),
                error="step s1 failed",
            )
            return exec_result
        cap = self._make_verified_capability_result("s1")
        sr = StepResult(
            step_id="s1",
            capability_name="test.echo",
            status=StepState.SUCCEEDED,
            capability_result=cap,
        )
        exec_result = exec_result.with_step_result(sr)
        exec_result = exec_result.with_outcome(
            ExecutionOutcome.COMPLETED,
            completed_at=time.time(),
        )
        return exec_result

    def execute_step(self, context, step):
        cap = self._make_verified_capability_result(step.step_id)
        return StepResult(
            step_id=step.step_id,
            capability_name=step.capability_name,
            status=StepState.SUCCEEDED,
            capability_result=cap,
        )


# ---------------------------------------------------------------------------
# Construction / invariants
# ---------------------------------------------------------------------------

class TestAgentConstruction:
    def test_default_collaborators(self):
        a = Agent(
            interpreter=_StaticInterpreter(),
            planner=_StaticPlanner(),
            plan_executor=_StaticExecutor(),
        )
        assert a.state is AgentState.IDLE
        assert a.last_result is None
        stats = a.statistics()
        assert stats["state"] == "idle"
        assert stats["recovery_engine"] == "default-recovery"
        assert stats["step_verifier"] == "verifier-router"
        assert stats["goal_verifier"] == "default-goal"
        assert stats["observation_provider"] == "capability-derived"

    def test_repr_is_safe(self):
        a = Agent(
            interpreter=_StaticInterpreter(),
            planner=_StaticPlanner(),
            plan_executor=_StaticExecutor(),
        )
        r = repr(a)
        assert "Agent" in r
        assert "state=" in r

    def test_reset_returns_to_idle(self):
        a = Agent(
            interpreter=_StaticInterpreter(),
            planner=_StaticPlanner(),
            plan_executor=_StaticExecutor(),
        )
        a._state = AgentState.EXECUTING  # internal poke
        a.reset()
        assert a.state is AgentState.IDLE


# ---------------------------------------------------------------------------
# Happy path: goal achieved, no replan
# ---------------------------------------------------------------------------

class TestAgentHappyPath:
    def test_happy_path_returns_complete(self):
        a = Agent(
            interpreter=_StaticInterpreter(),
            planner=_StaticPlanner(succeed=True),
            plan_executor=_StaticExecutor(fail=False),
        )
        result = a.run("do the thing")
        assert isinstance(result, AgentResult)
        assert result.final_state is AgentState.COMPLETE
        assert result.completed is True
        assert result.failed is False
        assert result.plan_count == 1
        assert result.replans == 0
        assert result.error == ""

    def test_happy_path_records_observations(self):
        a = Agent(
            interpreter=_StaticInterpreter(),
            planner=_StaticPlanner(succeed=True),
            plan_executor=_StaticExecutor(fail=False),
        )
        result = a.run("go")
        # At least one observation per step + the goal verdict.
        assert len(result.observation_history) >= 1

    def test_happy_path_records_one_plan(self):
        a = Agent(
            interpreter=_StaticInterpreter(),
            planner=_StaticPlanner(succeed=True),
            plan_executor=_StaticExecutor(fail=False),
        )
        result = a.run("go")
        assert result.plan_count == 1
        last = result.last_plan()
        assert last is not None
        assert last.replan_count == 0
        assert last.parent_plan_id is None


# ---------------------------------------------------------------------------
# Failure → REPLAN → success
# ---------------------------------------------------------------------------

class TestAgentReplanFlow:
    def test_failure_then_replan_to_success(self):
        # Force a clean REPLAN → REPLAN succeeds cycle by using a
        # custom recovery engine that chooses REPLAN on the first
        # failure (skipping the default RETRY_WITH_BACKOFF).
        import uuid as _uuid
        from core.orchestration import (
            RecoveryAction, RecoveryDecision,
        )

        class _ForceReplanRecovery(DefaultRecoveryEngine):
            """Recovery engine: on first failure, REPLAN.  Then GIVE_UP."""

            def __init__(self, **kw):
                super().__init__(**kw)
                self.decisions = 0

            def decide(self, failure, context, *, history=None):
                self.decisions += 1
                if self.decisions == 1:
                    return RecoveryDecision(
                        decision_id=f"rd-{_uuid.uuid4().hex[:10]}",
                        action=RecoveryAction.REPLAN,
                        failure_id=failure.failure_id,
                        backoff_s=0.0,
                        rationale="test: forced replan",
                    )
                return RecoveryDecision(
                    decision_id=f"rd-{_uuid.uuid4().hex[:10]}",
                    action=RecoveryAction.GIVE_UP,
                    failure_id=failure.failure_id,
                    backoff_s=0.0,
                    rationale="test: give up after replan",
                )

        class _FirstFailsThenSucceeds(_StaticPlanner):
            def plan(self, goal, *, intent=None, context_snapshot=None,
                     prior_plan=None, failure=None):
                if prior_plan is None:
                    step = PlanStep(
                        step_id="s1",
                        description="first step (fails)",
                        capability_name="test.echo",
                        parameters={"text": "first"},
                        expected_effect=ExpectedEffect(
                            check_name="echo_ok", expected="hello"
                        ),
                    )
                    return Plan(plan_id="p1", goal_id=goal.goal_id, steps=(step,))
                # Replan: succeed
                step = PlanStep(
                    step_id="s1",
                    description="retry step (verified)",
                    capability_name="test.echo",
                    parameters={"text": "hello"},
                    expected_effect=ExpectedEffect(
                        check_name="echo_ok", expected="hello"
                    ),
                )
                return Plan(
                    plan_id="p2",
                    goal_id=goal.goal_id,
                    steps=(step,),
                    replan_count=prior_plan.replan_count + 1,
                    parent_plan_id=prior_plan.plan_id,
                )

        class _PlanIdKeyedExecutor(_StaticExecutor):
            def execute(self, context):
                r = make_blank_execution_result(
                    execution_id=context.execution_id,
                    plan_id=context.plan.plan_id,
                    goal_id=context.goal.goal_id,
                )
                if context.plan.plan_id == "p1":
                    cap = self._make_failed_capability_result("s1", "first failed")
                    sr = StepResult(
                        step_id="s1",
                        capability_name="test.echo",
                        status=StepState.FAILED,
                        capability_result=cap,
                        error="first failed",
                    )
                    r = r.with_step_result(sr).with_outcome(
                        ExecutionOutcome.FAILED,
                        completed_at=time.time(),
                        error="first failed",
                    )
                else:
                    cap = self._make_verified_capability_result("s1")
                    sr = StepResult(
                        step_id="s1",
                        capability_name="test.echo",
                        status=StepState.SUCCEEDED,
                        capability_result=cap,
                    )
                    r = r.with_step_result(sr).with_outcome(
                        ExecutionOutcome.COMPLETED,
                        completed_at=time.time(),
                    )
                return r

        a = Agent(
            interpreter=_StaticInterpreter(),
            planner=_FirstFailsThenSucceeds(),
            plan_executor=_PlanIdKeyedExecutor(),
            recovery_engine=_ForceReplanRecovery(),
        )
        result = a.run("do thing")
        assert result.is_terminal
        # First failure → REPLAN; second plan succeeds → COMPLETE.
        assert result.replans >= 1
        assert result.final_state is AgentState.COMPLETE

    def test_give_up_after_exhausting_budget(self):
        # Executor always fails; recovery engine has max_replans=1.
        class _AlwaysFail(_StaticExecutor):
            def execute(self, context):
                r = make_blank_execution_result(
                    execution_id=context.execution_id,
                    plan_id=context.plan.plan_id,
                    goal_id=context.goal.goal_id,
                )
                cap = self._make_failed_capability_result("s1", "always fails")
                sr = StepResult(
                    step_id="s1",
                    capability_name="test.echo",
                    status=StepState.FAILED,
                    capability_result=cap,
                    error="always fails",
                )
                r = r.with_step_result(sr).with_outcome(
                    ExecutionOutcome.FAILED,
                    completed_at=time.time(),
                    error="fail",
                )
                return r

        from core.orchestration import RecoveryPolicy
        engine = DefaultRecoveryEngine(
            policy=RecoveryPolicy(max_attempts_per_step=1, max_replans=1)
        )
        a = Agent(
            interpreter=_StaticInterpreter(),
            planner=_StaticPlanner(),
            plan_executor=_AlwaysFail(),
            recovery_engine=engine,
            policy=AgentPolicy(max_iterations=10, max_total_runtime_s=10.0),
        )
        result = a.run("go")
        assert result.final_state in (
            AgentState.FAILED, AgentState.CLARIFICATION_REQUIRED
        )
        # We must have stopped with bounded replans.
        assert result.replans <= 2


# ---------------------------------------------------------------------------
# Bounded runtime / no infinite loops
# ---------------------------------------------------------------------------

class TestAgentBoundedRuntime:
    def test_max_iterations_cap(self):
        # Executor always fails; agent should stop after max_iterations.
        class _AlwaysFail(_StaticExecutor):
            def execute(self, context):
                r = make_blank_execution_result(
                    execution_id=context.execution_id,
                    plan_id=context.plan.plan_id,
                    goal_id=context.goal.goal_id,
                )
                cap = self._make_failed_capability_result("s1", "always fails")
                sr = StepResult(
                    step_id="s1",
                    capability_name="test.echo",
                    status=StepState.FAILED,
                    capability_result=cap,
                    error="always fails",
                )
                r = r.with_step_result(sr).with_outcome(
                    ExecutionOutcome.FAILED,
                    completed_at=time.time(),
                )
                return r

            def execute_step(self, context, step):
                # Phase 3 single-retry path: when the executor
                # always fails, ``execute_step`` must also fail,
                # otherwise the retry path "succeeds" via the
                # inherited ``_StaticExecutor.execute_step`` and
                # the agent falsely reports COMPLETE.
                cap = self._make_failed_capability_result(
                    step.step_id, "always fails",
                )
                return StepResult(
                    step_id=step.step_id,
                    capability_name=step.capability_name,
                    status=StepState.FAILED,
                    capability_result=cap,
                    error="always fails",
                )

        a = Agent(
            interpreter=_StaticInterpreter(),
            planner=_StaticPlanner(),
            plan_executor=_AlwaysFail(),
            policy=AgentPolicy(max_iterations=3, max_total_runtime_s=10.0),
        )
        start = time.time()
        result = a.run("go")
        elapsed = time.time() - start
        # Should return quickly (well under 5 seconds).
        assert elapsed < 5.0
        assert result.final_state in (
            AgentState.FAILED, AgentState.CLARIFICATION_REQUIRED, AgentState.TIMEOUT,
        )
        assert result.error != ""

    def test_max_total_runtime_cap(self):
        class _AlwaysFail(_StaticExecutor):
            def execute(self, context):
                r = make_blank_execution_result(
                    execution_id=context.execution_id,
                    plan_id=context.plan.plan_id,
                    goal_id=context.goal.goal_id,
                )
                cap = self._make_failed_capability_result("s1", "always fails")
                sr = StepResult(
                    step_id="s1",
                    capability_name="test.echo",
                    status=StepState.FAILED,
                    capability_result=cap,
                    error="always fails",
                )
                r = r.with_step_result(sr).with_outcome(
                    ExecutionOutcome.FAILED,
                    completed_at=time.time(),
                )
                return r

            def execute_step(self, context, step):
                # Phase 3 single-retry path: see
                # test_max_iterations_cap for the rationale.
                cap = self._make_failed_capability_result(
                    step.step_id, "always fails",
                )
                return StepResult(
                    step_id=step.step_id,
                    capability_name=step.capability_name,
                    status=StepState.FAILED,
                    capability_result=cap,
                    error="always fails",
                )

        a = Agent(
            interpreter=_StaticInterpreter(),
            planner=_StaticPlanner(),
            plan_executor=_AlwaysFail(),
            policy=AgentPolicy(max_iterations=100, max_total_runtime_s=0.05),
        )
        start = time.time()
        result = a.run("go")
        elapsed = time.time() - start
        # Should respect the very short runtime cap.
        assert elapsed < 2.0
        assert result.final_state in (
            AgentState.FAILED, AgentState.CLARIFICATION_REQUIRED, AgentState.TIMEOUT,
        )


# ---------------------------------------------------------------------------
# Goal / interpreter error paths
# ---------------------------------------------------------------------------

class TestAgentErrorPaths:
    def test_interpreter_failure(self):
        class _BoomInterpreter:
            name = "boom"

            def interpret(self, text, *, context_snapshot=None):
                raise RuntimeError("interpret failed")

        a = Agent(
            interpreter=_BoomInterpreter(),
            planner=_StaticPlanner(),
            plan_executor=_StaticExecutor(),
        )
        result = a.run("go")
        assert result.final_state is AgentState.FAILED
        assert "interpreter" in result.error

    def test_planner_returns_none(self):
        class _NonePlanner:
            name = "none-planner"

            def plan(self, goal, *, intent=None, context_snapshot=None,
                     prior_plan=None, failure=None):
                return None

        a = Agent(
            interpreter=_StaticInterpreter(),
            planner=_NonePlanner(),
            plan_executor=_StaticExecutor(),
        )
        result = a.run("go")
        assert result.final_state is AgentState.CLARIFICATION_REQUIRED

    def test_planner_raises(self):
        class _BoomPlanner:
            name = "boom-planner"

            def plan(self, goal, *, intent=None, context_snapshot=None,
                     prior_plan=None, failure=None):
                raise RuntimeError("plan failed")

        a = Agent(
            interpreter=_StaticInterpreter(),
            planner=_BoomPlanner(),
            plan_executor=_StaticExecutor(),
        )
        result = a.run("go")
        assert result.final_state is AgentState.FAILED
        assert "planner" in result.error

    def test_executor_raises(self):
        class _BoomExecutor:
            name = "boom-exec"

            def execute(self, context):
                raise RuntimeError("executor failed")

            def execute_step(self, context, step):
                raise RuntimeError("not used")

        a = Agent(
            interpreter=_StaticInterpreter(),
            planner=_StaticPlanner(),
            plan_executor=_BoomExecutor(),
        )
        result = a.run("go")
        assert result.final_state is AgentState.FAILED
        assert "executor" in result.error

    def test_intent_to_goal_failure(self):
        class _BadIntent:
            intent_id = "i-bad"
            kind = IntentKind.COMMAND
            text = "x"
            confidence = 1.0

            def to_goal(self):
                raise RuntimeError("to_goal failed")

        class _BadInterpreter:
            name = "bad-int"

            def interpret(self, text, *, context_snapshot=None):
                return _BadIntent()

        a = Agent(
            interpreter=_BadInterpreter(),
            planner=_StaticPlanner(),
            plan_executor=_StaticExecutor(),
        )
        result = a.run("go")
        assert result.final_state is AgentState.FAILED
        assert "goal" in result.error


# ---------------------------------------------------------------------------
# run_goal entry point
# ---------------------------------------------------------------------------

class TestAgentRunGoal:
    def test_run_goal_skips_interpreter(self):
        a = Agent(
            interpreter=_StaticInterpreter(),
            planner=_StaticPlanner(),
            plan_executor=_StaticExecutor(),
        )
        goal = Goal(goal_id="g1", description="a goal")
        result = a.run_goal(goal)
        assert result.goal_id == "g1"
        assert result.final_state is AgentState.COMPLETE

    def test_run_goal_records_goal_id(self):
        a = Agent(
            interpreter=_StaticInterpreter(),
            planner=_StaticPlanner(),
            plan_executor=_StaticExecutor(),
        )
        goal = Goal(
            goal_id="g-test-99",
            description="explicit goal",
            success_criteria=("criterion_a", "criterion_b"),
        )
        result = a.run_goal(goal)
        assert result.goal_id == "g-test-99"
