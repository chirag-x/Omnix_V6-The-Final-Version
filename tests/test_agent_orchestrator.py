"""
Omnix V6 — Phase 6C Agent Orchestrator end-to-end tests.

These tests exercise the full closed loop:
    PLAN → EXECUTE → OBSERVE → DECIDE → (CONTINUE | REPLAN | COMPLETE | FAILED)

They pin the contracts that the rest of the system relies on:
- run() and run_goal() return a populated AgentResult
- replan_count and plan_count reflect the actual loop
- observation history records every OBSERVE phase
- final state matches the outcome
- terminal states (COMPLETE / FAILED / CLARIFICATION_REQUIRED) are reached
"""

from __future__ import annotations

import time

import pytest

from core.orchestration import (
    Agent,
    AgentPolicy,
    AgentResult,
    AgentState,
    DefaultRecoveryEngine,
    ExpectedEffect,
    ExecutionContext,
    Failure,
    FailureKind,
    Goal,
    Intent,
    IntentKind,
    Observation,
    ObservationSource,
    Plan,
    PlanStep,
    RecoveryAction,
    RecoveryDecision,
    RecoveryPolicy,
    make_blank_execution_result,
    make_failure,
)
from core.results import (
    ActionResult,
    ActionStatus,
    CapabilityResult,
    CapabilityStatus,
    VerificationResult,
    VerificationStatus,
)


# ---------------------------------------------------------------------------
# Stub collaborators
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
    """Returns a single-step plan."""

    name: str = "static-planner"

    def plan(
        self,
        goal,
        *,
        intent=None,
        context_snapshot=None,
        prior_plan=None,
        failure=None,
    ):
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
        # Replan adds a second attempt.
        retry = PlanStep(
            step_id="s1_retry",
            description="retry",
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


def _make_verified_capability_result(step_id: str = "s1") -> CapabilityResult:
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


def _make_failed_capability_result(step_id: str = "s1", error: str = "x") -> CapabilityResult:
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


class _StaticExecutor:
    name: str = "static-executor"

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.call_count = 0

    def execute(self, context):
        self.call_count += 1
        r = make_blank_execution_result(
            execution_id=context.execution_id,
            plan_id=context.plan.plan_id,
            goal_id=context.goal.goal_id,
        )
        if self.fail:
            cap = _make_failed_capability_result("s1", "simulated failure")
            sr = _step_result_failed("s1", cap)
            r = r.with_step_result(sr).with_outcome(
                _EXEC_FAILED, completed_at=time.time(), error="step s1 failed"
            )
            return r
        cap = _make_verified_capability_result("s1")
        sr = _step_result_succeeded("s1", cap)
        r = r.with_step_result(sr).with_outcome(
            _EXEC_COMPLETED, completed_at=time.time()
        )
        return r


from core.orchestration import ExecutionOutcome, StepResult, StepState

_EXEC_COMPLETED = ExecutionOutcome.COMPLETED
_EXEC_FAILED = ExecutionOutcome.FAILED


def _step_result_succeeded(step_id: str, cap: CapabilityResult) -> StepResult:
    return StepResult(
        step_id=step_id,
        capability_name="test.echo",
        status=StepState.SUCCEEDED,
        capability_result=cap,
    )


def _step_result_failed(step_id: str, cap: CapabilityResult) -> StepResult:
    return StepResult(
        step_id=step_id,
        capability_name="test.echo",
        status=StepState.FAILED,
        capability_result=cap,
        error="simulated",
    )


# ---------------------------------------------------------------------------
# Plan/observation history
# ---------------------------------------------------------------------------

class TestPlanAndObservationHistory:
    def test_run_records_plan_history(self):
        a = Agent(
            interpreter=_StaticInterpreter(),
            planner=_StaticPlanner(),
            plan_executor=_StaticExecutor(),
        )
        result = a.run("hi")
        assert len(result.plan_history) == 1
        assert result.plan_count == 1

    def test_run_records_observation_history(self):
        a = Agent(
            interpreter=_StaticInterpreter(),
            planner=_StaticPlanner(),
            plan_executor=_StaticExecutor(),
        )
        result = a.run("hi")
        # At least the per-step observation + the goal verdict.
        assert len(result.observation_history) >= 1

    def test_last_plan_method(self):
        a = Agent(
            interpreter=_StaticInterpreter(),
            planner=_StaticPlanner(),
            plan_executor=_StaticExecutor(),
        )
        result = a.run("hi")
        last = result.last_plan()
        assert last is not None
        assert last.plan_id == "p1"

    def test_run_goal_sets_goal_id(self):
        a = Agent(
            interpreter=_StaticInterpreter(),
            planner=_StaticPlanner(),
            plan_executor=_StaticExecutor(),
        )
        g = Goal(goal_id="g-99", description="specific")
        result = a.run_goal(g)
        assert result.goal_id == "g-99"


# ---------------------------------------------------------------------------
# Result properties
# ---------------------------------------------------------------------------

class TestAgentResultProperties:
    def _result_complete(self):
        a = Agent(
            interpreter=_StaticInterpreter(),
            planner=_StaticPlanner(),
            plan_executor=_StaticExecutor(fail=False),
        )
        return a.run("go")

    def _result_failed(self):
        class _AlwaysFail(_StaticExecutor):
            def execute(self, context):
                r = make_blank_execution_result(
                    execution_id=context.execution_id,
                    plan_id=context.plan.plan_id,
                    goal_id=context.goal.goal_id,
                )
                cap = _make_failed_capability_result("s1", "x")
                sr = _step_result_failed("s1", cap)
                r = r.with_step_result(sr).with_outcome(
                    _EXEC_FAILED, completed_at=time.time(), error="x"
                )
                return r

        a = Agent(
            interpreter=_StaticInterpreter(),
            planner=_StaticPlanner(),
            plan_executor=_AlwaysFail(),
            recovery_engine=DefaultRecoveryEngine(
                policy=RecoveryPolicy(max_replans=0)
            ),
        )
        return a.run("go")

    def test_completed_flag(self):
        r = self._result_complete()
        assert r.completed is True
        assert r.failed is False

    def test_failed_flag(self):
        r = self._result_failed()
        assert r.completed is False
        assert r.failed is True

    def test_is_terminal_for_complete(self):
        r = self._result_complete()
        assert r.is_terminal is True

    def test_is_terminal_for_failed(self):
        r = self._result_failed()
        assert r.is_terminal is True

    def test_to_dict_projection(self):
        r = self._result_complete()
        d = r.to_dict()
        assert d["goal_id"]
        assert d["final_state"] == AgentState.COMPLETE.value
        assert d["completed"] is True
        assert d["failed"] is False
        assert "plan_count" in d
        assert "replans" in d


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------

class TestAgentErrorPaths:
    def test_interpreter_failure_marks_failed(self):
        class _Boom:
            name = "boom"

            def interpret(self, text, *, context_snapshot=None):
                raise RuntimeError("int failed")

        a = Agent(
            interpreter=_Boom(),
            planner=_StaticPlanner(),
            plan_executor=_StaticExecutor(),
        )
        r = a.run("x")
        assert r.final_state is AgentState.FAILED

    def test_planner_returns_none_yields_clarification(self):
        class _None:
            name = "none"

            def plan(self, goal, *, intent=None, context_snapshot=None,
                     prior_plan=None, failure=None):
                return None

        a = Agent(
            interpreter=_StaticInterpreter(),
            planner=_None(),
            plan_executor=_StaticExecutor(),
        )
        r = a.run("x")
        assert r.final_state is AgentState.CLARIFICATION_REQUIRED

    def test_planner_raises_marks_failed(self):
        class _Boom:
            name = "boom"

            def plan(self, goal, *, intent=None, context_snapshot=None,
                     prior_plan=None, failure=None):
                raise RuntimeError("plan failed")

        a = Agent(
            interpreter=_StaticInterpreter(),
            planner=_Boom(),
            plan_executor=_StaticExecutor(),
        )
        r = a.run("x")
        assert r.final_state is AgentState.FAILED

    def test_executor_raises_marks_failed(self):
        class _Boom:
            name = "boom"

            def execute(self, context):
                raise RuntimeError("exec failed")

            def execute_step(self, context, step):
                raise RuntimeError("not used")

        a = Agent(
            interpreter=_StaticInterpreter(),
            planner=_StaticPlanner(),
            plan_executor=_Boom(),
        )
        r = a.run("x")
        assert r.final_state is AgentState.FAILED


# ---------------------------------------------------------------------------
# Bounded runtime
# ---------------------------------------------------------------------------

class TestAgentBounded:
    def test_max_iterations_terminates(self):
        class _AlwaysFail(_StaticExecutor):
            def execute(self, context):
                r = make_blank_execution_result(
                    execution_id=context.execution_id,
                    plan_id=context.plan.plan_id,
                    goal_id=context.goal.goal_id,
                )
                cap = _make_failed_capability_result("s1", "x")
                sr = _step_result_failed("s1", cap)
                r = r.with_step_result(sr).with_outcome(
                    _EXEC_FAILED, completed_at=time.time()
                )
                return r

        a = Agent(
            interpreter=_StaticInterpreter(),
            planner=_StaticPlanner(),
            plan_executor=_AlwaysFail(),
            policy=AgentPolicy(max_iterations=3, max_total_runtime_s=10.0),
        )
        start = time.time()
        r = a.run("x")
        elapsed = time.time() - start
        assert elapsed < 5.0
        assert r.final_state in (
            AgentState.FAILED, AgentState.CLARIFICATION_REQUIRED, AgentState.TIMEOUT
        )

    def test_max_runtime_terminates(self):
        class _AlwaysFail(_StaticExecutor):
            def execute(self, context):
                r = make_blank_execution_result(
                    execution_id=context.execution_id,
                    plan_id=context.plan.plan_id,
                    goal_id=context.goal.goal_id,
                )
                cap = _make_failed_capability_result("s1", "x")
                sr = _step_result_failed("s1", cap)
                r = r.with_step_result(sr).with_outcome(
                    _EXEC_FAILED, completed_at=time.time()
                )
                return r

        a = Agent(
            interpreter=_StaticInterpreter(),
            planner=_StaticPlanner(),
            plan_executor=_AlwaysFail(),
            policy=AgentPolicy(max_iterations=100, max_total_runtime_s=0.05),
        )
        start = time.time()
        r = a.run("x")
        elapsed = time.time() - start
        assert elapsed < 2.0
        assert r.final_state in (
            AgentState.FAILED, AgentState.CLARIFICATION_REQUIRED, AgentState.TIMEOUT
        )


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------

class TestAgentStatistics:
    def test_statistics_keys(self):
        a = Agent(
            interpreter=_StaticInterpreter(),
            planner=_StaticPlanner(),
            plan_executor=_StaticExecutor(),
        )
        s = a.statistics()
        assert "state" in s
        assert "recovery_engine" in s
        assert "step_verifier" in s
        assert "goal_verifier" in s
        assert "observation_provider" in s
        assert "policy" in s
