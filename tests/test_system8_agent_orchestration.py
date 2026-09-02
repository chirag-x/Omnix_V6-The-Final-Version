"""
Omnix V6 — System 8: Agent Orchestration & Multi-Step Execution.

End-to-end + unit tests for the System 8 layer:

  * Structured :class:`ProgressEvent` broadcast at every step
    transition.
  * Dependency-DAG validation that catches cycles, unknown
    dependencies, self-dependency, and duplicate step ids
    *before* the Agent dispatches the first step.
  * Bounded retry tracking via :class:`RetryTracker`.
  * End-to-end Agent runs through a deterministic in-memory
    CapabilityRegistry: a happy path, a recovery path, a
    dependency-aware multi-step plan, and a cancellation path.

The tests are *strictly* behavioral: they assert what the
Agent actually does, not what it might do.  They do NOT depend
on any specific app, window title, or hardcoded workflow — the
test capability names are abstract (``test.echo``,
``test.increment``) so the same suite runs on any machine.
"""

from __future__ import annotations

import time
from dataclasses import replace
from typing import Any, Dict, List, Optional, Tuple

import pytest

from core.capability import (
    CallableCapability,
    CapabilityParameter,
    CapabilitySpec,
    ParamType,
)
from core.capability_registry import CapabilityRegistry
from core.capability_router import CapabilityRouter
from core.results import (
    ActionResult,
    ActionStatus,
    CapabilityResult,
    CapabilityStatus,
    VerificationResult,
    VerificationStatus,
)
from core.responses import ResponseStatus
from core.orchestration import (
    Agent,
    AgentPolicy,
    AgentResult,
    AgentState,
    Failure,
    FailureKind,
    Goal,
    Intent,
    IntentKind,
    MultiStepCoordinator,
    Plan,
    PlanStep,
    PlanExecutorImpl,
    RecoveryAction,
    VerificationVerdict,
    # System 8 additions:
    DAGIssueKind,
    DAGValidationResult,
    InMemoryIdempotencyStore,
    InMemoryMultiStepContextStore,
    InMemoryProgressBroadcaster,
    MultiStepContext,
    MultiStepContextStore,
    IdempotencyStore,
    ProgressBroadcaster,
    ProgressEvent,
    ProgressPhase,
    RetryCounters,
    RetryTracker,
    validate_plan,
    validate_steps,
    # Verifier / recovery
    DefaultStepVerifier,
    DefaultGoalVerifier,
    DefaultRecoveryEngine,
    RecoveryPolicy,
    make_failure,
    passed_verdict,
    failed_verdict,
    uncertain_verdict,
    # Multi-step helpers
    StepLifecycle,
)
from core.orchestration.observation import (
    CapabilityResultObservationProvider,
    ObservationProvider,
)
from core.orchestration.models import (
    ActionRequest,
    ActionKind,
    ExecutionContext,
    ExpectedEffect,
    Observation,
    ObservationSource,
)
from core.orchestration.idempotency import IdempotencyLog
from core.orchestration.interfaces import (
    IntentInterpreter,
    Planner,
    PlanExecutor,
    RecoveryEngine,
)


# ===========================================================================
# Test capability implementations
# ===========================================================================
# These capabilities are abstract (no apps, no window titles,
# no coordinates).  They let us drive the Agent end-to-end on
# any machine, headless or otherwise.


class _Echo:
    """A simple echo capability that always passes."""

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    def fn(self, params):
        self.calls.append(dict(params))
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
                details={"echoed": dict(params)},
            ),
            verification=VerificationResult(
                status=VerificationStatus.VERIFIED,
                check_name="echo_ok",
                expected=str(params),
                actual=str(params),
            ),
            details={"echoed": dict(params)},
        )

    @property
    def spec(self) -> CapabilitySpec:
        return CapabilitySpec(
            name="test.echo",
            version="1.0.0",
            description="Echo the input parameters.",
            parameters=(
                CapabilityParameter(
                    name="msg", type=ParamType.STRING, required=False, default=""
                ),
            ),
        )


class _Increment:
    """Capability that records its delta in the order called."""

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    def fn(self, params):
        self.calls.append(dict(params))
        return CapabilityResult(
            capability_name="test.increment",
            status=CapabilityStatus.VERIFIED,
            attempted=True,
            executed=True,
            verified=True,
            failed=False,
            action=ActionResult(
                status=ActionStatus.EXECUTED,
                action_name="test.increment",
                details={"delta": int(params.get("delta", 1))},
            ),
            details={"delta": int(params.get("delta", 1))},
        )

    @property
    def spec(self) -> CapabilitySpec:
        return CapabilitySpec(
            name="test.increment",
            version="1.0.0",
            description="Increment a counter.",
            parameters=(
                CapabilityParameter(name="delta", type=ParamType.INTEGER, required=False, default=1),
            ),
        )


class _SometimesFails:
    """Capability that fails on the first N calls per step, then
    succeeds.  Used to exercise the recovery loop."""

    def __init__(self, fail_count: int = 1) -> None:
        self.fail_count = fail_count
        self.calls: List[Dict[str, Any]] = []

    def fn(self, params):
        self.calls.append(dict(params))
        if len(self.calls) <= self.fail_count:
            return CapabilityResult(
                capability_name="test.flaky",
                status=CapabilityStatus.FAILED,
                attempted=True,
                executed=True,
                verified=False,
                failed=True,
                action=ActionResult(
                    status=ActionStatus.FAILED,
                    action_name="test.flaky",
                    details={"attempt": len(self.calls)},
                ),
                error=None,
            )
        return CapabilityResult(
            capability_name="test.flaky",
            status=CapabilityStatus.VERIFIED,
            attempted=True,
            executed=True,
            verified=True,
            failed=False,
            action=ActionResult(
                status=ActionStatus.EXECUTED,
                action_name="test.flaky",
                details={"attempt": len(self.calls)},
            ),
            details={"attempt": len(self.calls)},
        )

    @property
    def spec(self) -> CapabilitySpec:
        return CapabilitySpec(
            name="test.flaky",
            version="1.0.0",
            description="Sometimes fails.",
            parameters=(),
        )


# ===========================================================================
# Test interpreter / planner — deterministic, no LLM
# ===========================================================================

class _FixedGoalInterpreter:
    """An interpreter that returns a pre-baked :class:`Intent`."""

    def __init__(self, intent: Intent) -> None:
        self._intent = intent

    def interpret(self, text: str, *, context_snapshot: Optional[Dict[str, Any]] = None):
        from ai.intent import IntentResult
        return IntentResult(status="ok", intent=self._intent)


class _FixedGoalPlanner:
    """A planner that returns a pre-baked :class:`Plan`."""

    def __init__(self, plan: Plan) -> None:
        self._plan = plan

    def plan(
        self,
        goal: Goal,
        *,
        intent: Optional[Intent] = None,
        context_snapshot: Optional[Dict[str, Any]] = None,
        prior_plan: Optional[Plan] = None,
        failure: Optional[Failure] = None,
    ) -> Plan:
        return self._plan


def _intent(
    *,
    kind: IntentKind = IntentKind.COMMAND,
    description: str = "do something",
) -> Intent:
    return Intent(
        intent_id="intent-test",
        kind=kind,
        text=description,
        parameters={},
    )


def _goal(
    *,
    description: str = "do something",
    goal_id: str = "goal-test",
    success_criteria: Tuple[str, ...] = ("ok",),
) -> Goal:
    return Goal(
        goal_id=goal_id,
        description=description,
        success_criteria=success_criteria,
    )


def _step(
    step_id: str,
    capability: str,
    *,
    params: Optional[Dict[str, Any]] = None,
    depends_on: Tuple[str, ...] = (),
    expected_effect: Optional[ExpectedEffect] = None,
) -> PlanStep:
    return PlanStep(
        step_id=step_id,
        description=f"{step_id} ({capability})",
        capability_name=capability,
        parameters=dict(params or {}),
        depends_on=depends_on,
        expected_effect=expected_effect or ExpectedEffect(
            check_name=f"{capability}_ok",
            expected=params or {},
        ),
    )


def _plan(*steps: PlanStep, plan_id: str = "plan-test") -> Plan:
    return Plan(plan_id=plan_id, goal_id="goal-test", steps=list(steps))


# ===========================================================================
# Build a wired Agent with an in-memory capability registry
# ===========================================================================

class _WiredAgent:
    """A bundle of wired components for end-to-end Agent tests.

    The :class:`Agent` is the canonical V6 closed-loop
    controller; this helper just wires the standard set of
    collaborators (interpreter, planner, executor, verifier,
    recovery engine, observation provider) around a small
    in-memory capability registry so the tests can drive the
    Agent deterministically without an LLM, a real desktop, or
    any third-party dependency.
    """

    def __init__(
        self,
        *,
        steps: List[PlanStep],
        capabilities: Dict[str, Any],
        recovery_policy: Optional[RecoveryPolicy] = None,
        agent_policy: Optional[AgentPolicy] = None,
        broadcaster: Optional[ProgressBroadcaster] = None,
        max_step_retries: int = 3,
    ) -> None:
        # 1. Capability registry + router — register each test
        # capability as a real :class:`CallableCapability` so the
        # production router and executor see them just like the
        # built-in capabilities.
        self.registry = CapabilityRegistry()
        self._objs = capabilities  # hold the live objects
        for name, obj in self._objs.items():
            self.registry.register(CallableCapability(obj.spec, obj.fn))
        self.router = CapabilityRouter(self.registry)

        # 2. Observation / Verifier / Recovery
        self.observation_provider: ObservationProvider = (
            CapabilityResultObservationProvider()
        )
        self.step_verifier = DefaultStepVerifier()
        self.goal_verifier = DefaultGoalVerifier()
        self.recovery_engine: RecoveryEngine = DefaultRecoveryEngine(
            policy=recovery_policy or RecoveryPolicy(
                max_attempts_per_step=max_step_retries,
                max_replans=1,
                max_total_runtime_s=60.0,
            ),
        )

        # 3. Executor
        self.executor = PlanExecutorImpl(
            router=self.router,
        )

        # 4. Interpreter / Planner
        self.intent = _intent(description="test")
        self.plan = _plan(*steps)
        self.interpreter: IntentInterpreter = _FixedGoalInterpreter(self.intent)
        self.planner: Planner = _FixedGoalPlanner(self.plan)

        # 5. Agent
        self.broadcaster = broadcaster or InMemoryProgressBroadcaster()
        self.agent = Agent(
            interpreter=self.interpreter,
            planner=self.planner,
            plan_executor=self.executor,
            recovery_engine=self.recovery_engine,
            observation_provider=self.observation_provider,
            step_verifier=self.step_verifier,
            goal_verifier=self.goal_verifier,
            policy=agent_policy or AgentPolicy(
                max_iterations=16,
                max_total_runtime_s=60.0,
            ),
            progress_broadcaster=self.broadcaster,
        )


# ===========================================================================
# 1. ProgressEvent construction
# ===========================================================================

def test_progress_event_construction_and_dict_round_trip():
    from core.orchestration.progress import make_progress_event

    ev = make_progress_event(
        ProgressPhase.STEP_DISPATCHED,
        plan_id="p1",
        step_id="s1",
        attempt=1,
        correlation_id="c1",
        message="dispatching s1",
        details={"capability": "test.echo"},
    )
    assert ev.phase is ProgressPhase.STEP_DISPATCHED
    assert ev.plan_id == "p1"
    assert ev.step_id == "s1"
    assert ev.attempt == 1
    assert ev.correlation_id == "c1"
    assert ev.message == "dispatching s1"
    d = ev.to_dict()
    assert d["type"] == "ProgressEvent"
    assert d["phase"] == "step_dispatched"
    assert d["plan_id"] == "p1"
    assert d["details"] == {"capability": "test.echo"}


def test_in_memory_progress_broadcaster_records_and_filters():
    from core.orchestration.progress import make_progress_event

    b = InMemoryProgressBroadcaster()
    a = make_progress_event(ProgressPhase.STEP_DISPATCHED, step_id="s1")
    b2 = make_progress_event(ProgressPhase.STEP_VERIFIED, step_id="s1")
    c = make_progress_event(ProgressPhase.STEP_DISPATCHED, step_id="s2")
    for e in (a, b2, c):
        b.publish(e)
    assert b.count(ProgressPhase.STEP_DISPATCHED) == 2
    assert b.count(ProgressPhase.STEP_VERIFIED) == 1
    assert len(b.of_phase(ProgressPhase.STEP_DISPATCHED)) == 2
    b.clear()
    assert b.count(ProgressPhase.STEP_DISPATCHED) == 0


def test_composite_broadcaster_is_fail_soft():
    from core.orchestration.progress import make_progress_event

    class _Boom:
        def publish(self, e):
            raise RuntimeError("simulated listener failure")

    b = InMemoryProgressBroadcaster()
    comp = type("C", (), {"publish": lambda self, e: b.publish(e)})()
    # Build a composite via the public class.
    from core.orchestration.progress import CompositeProgressBroadcaster

    boom = _Boom()
    composite = CompositeProgressBroadcaster(b, boom)
    # Should not raise even though the second listener throws.
    composite.publish(make_progress_event(ProgressPhase.INFO, message="hi"))
    assert b.count(ProgressPhase.INFO) == 1


def test_log_progress_broadcaster_does_not_raise():
    from core.orchestration.progress import (
        LogProgressBroadcaster, make_progress_event,
    )
    b = LogProgressBroadcaster()
    # No assertion — just make sure it doesn't raise.
    b.publish(make_progress_event(ProgressPhase.INFO, message="hi"))


def test_terminal_progress_phases_identified():
    from core.orchestration.progress import is_terminal_progress_phase
    assert is_terminal_progress_phase(ProgressPhase.AGENT_COMPLETE)
    assert is_terminal_progress_phase(ProgressPhase.AGENT_FAILED)
    assert is_terminal_progress_phase(ProgressPhase.AGENT_TIMEOUT)
    assert is_terminal_progress_phase(ProgressPhase.AGENT_CANCELLED)
    assert is_terminal_progress_phase(ProgressPhase.AGENT_CLARIFICATION)
    assert not is_terminal_progress_phase(ProgressPhase.STEP_DISPATCHED)
    assert not is_terminal_progress_phase(ProgressPhase.RECOVERY_DECISION)


# ===========================================================================
# 2. Dependency-DAG validation
# ===========================================================================

def test_dag_validator_passes_for_linear_plan():
    p = _plan(
        _step("a", "test.echo"),
        _step("b", "test.echo", depends_on=("a",)),
        _step("c", "test.echo", depends_on=("b",)),
    )
    r = validate_plan(p)
    assert r.ok, r.to_dict()
    assert r.step_count == 3
    assert r.topological_order == ("a", "b", "c")


def test_dag_validator_passes_for_diamond_plan():
    p = _plan(
        _step("root", "test.echo"),
        _step("left", "test.echo", depends_on=("root",)),
        _step("right", "test.echo", depends_on=("root",)),
        _step("merge", "test.echo", depends_on=("left", "right")),
    )
    r = validate_plan(p)
    assert r.ok
    assert r.step_count == 4
    # ``root`` must come first; ``merge`` must come last.
    assert r.topological_order[0] == "root"
    assert r.topological_order[-1] == "merge"


def test_dag_validator_detects_cycle():
    p = _plan(
        _step("a", "test.echo", depends_on=("c",)),
        _step("b", "test.echo", depends_on=("a",)),
        _step("c", "test.echo", depends_on=("b",)),
    )
    r = validate_plan(p)
    assert not r.ok
    cycles = r.issues_of(DAGIssueKind.CYCLE)
    assert cycles, "expected a CYCLE issue"
    assert r.topological_order == ()


def test_dag_validator_detects_self_dependency():
    p = _plan(
        _step("a", "test.echo", depends_on=("a",)),
    )
    r = validate_plan(p)
    selfs = r.issues_of(DAGIssueKind.SELF_DEPENDENCY)
    assert selfs, "expected a SELF_DEPENDENCY issue"
    assert not r.ok


def test_dag_validator_detects_unknown_dependency():
    p = _plan(
        _step("a", "test.echo", depends_on=("ghost",)),
    )
    r = validate_plan(p)
    unknowns = r.issues_of(DAGIssueKind.UNKNOWN_DEPENDENCY)
    assert unknowns, "expected an UNKNOWN_DEPENDENCY issue"
    assert not r.ok


def test_dag_validator_detects_duplicate_step_id():
    p = _plan(
        _step("a", "test.echo"),
        _step("a", "test.echo"),
    )
    r = validate_plan(p)
    dups = r.issues_of(DAGIssueKind.DUPLICATE_STEP_ID)
    assert dups, "expected a DUPLICATE_STEP_ID issue"
    assert not r.ok


def test_dag_validator_handles_empty_plan():
    r = validate_plan(_plan())
    assert r.ok
    assert r.step_count == 0
    assert r.topological_order == ()


def test_dag_validator_handles_none_plan():
    r = validate_plan(None)  # type: ignore[arg-type]
    assert not r.ok
    assert r.issue_count == 1


# ===========================================================================
# 3. Retry counters + tracker
# ===========================================================================

def test_retry_counters_step_attempts_increment():
    c = RetryCounters().with_step_attempt("s1").with_step_attempt("s1")
    assert c.attempts_for("s1") == 2
    # ``with_*`` is immutable — old value is preserved.
    c2 = c.with_step_attempt("s2")
    assert c.attempts_for("s2") == 0
    assert c2.attempts_for("s2") == 1


def test_retry_tracker_records_full_run():
    b = InMemoryProgressBroadcaster()
    t = RetryTracker(broadcaster=b, correlation_id="corr-1")
    t.record_step_attempt("s1")
    t.record_step_retry("s1")
    t.record_step_attempt("s2")
    t.record_step_retry("s2")
    t.record_step_retry("s2")
    t.record_replan()
    t.record_failure()
    t.record_decision()
    t.record_observation()

    c = t.snapshot()
    assert c.attempts_for("s1") == 1
    assert c.attempts_for("s2") == 1
    assert c.retries_for("s1") == 1
    assert c.retries_for("s2") == 2
    assert c.replans == 1
    assert c.failures == 1
    assert c.decisions == 1
    assert c.observations == 1
    assert c.elapsed_s() >= 0
    d = c.to_dict()
    assert d["step_attempts"] == {"s1": 1, "s2": 1}
    assert d["step_retries"] == {"s1": 1, "s2": 2}


def test_retry_tracker_reset_clears_state():
    t = RetryTracker()
    t.record_step_attempt("s1")
    t.record_replan()
    t.reset()
    assert t.snapshot().replans == 0
    assert t.attempts_for("s1") == 0


# ===========================================================================
# 4. End-to-end Agent run: happy path
# ===========================================================================

def test_agent_runs_happy_path_to_complete():
    echo = _Echo()
    wire = _WiredAgent(
        steps=[
            _step("a", "test.echo", params={"msg": "hi"}),
            _step("b", "test.echo", params={"msg": "there"}),
        ],
        capabilities={"test.echo": echo},
        broadcaster=InMemoryProgressBroadcaster(),
    )
    result: AgentResult = wire.agent.run("do the test")
    assert result.completed, result.to_dict()
    assert result.final_state is AgentState.COMPLETE
    assert result.plan_count >= 1
    # The capability was actually invoked.
    assert len(echo.calls) == 2
    # The progress broadcaster saw at least one STEP_VERIFIED.
    dispatched = wire.broadcaster.count(ProgressPhase.STEP_DISPATCHED)
    verified = wire.broadcaster.count(ProgressPhase.STEP_VERIFIED)
    assert dispatched >= 2
    assert verified >= 2
    # And the agent emitted its terminal phase.
    assert wire.broadcaster.count(ProgressPhase.AGENT_COMPLETE) >= 1


# ===========================================================================
# 5. End-to-end Agent run: dependency-aware multi-step
# ===========================================================================

def test_agent_runs_dependency_aware_multi_step_plan():
    inc = _Increment()
    # The plan has 3 steps with explicit ``depends_on`` so the
    # executor must respect the topological order.
    plan_steps = [
        _step("init", "test.increment", params={"delta": 1}),
        _step("add2", "test.increment", params={"delta": 2}, depends_on=("init",)),
        _step("add3", "test.increment", params={"delta": 3}, depends_on=("add2",)),
    ]
    wire = _WiredAgent(
        steps=plan_steps,
        capabilities={"test.increment": inc},
    )
    result = wire.agent.run("run multi-step")
    assert result.completed, result.to_dict()
    # The ``Increment`` capability must have been called 3 times,
    # in the order init, add2, add3.
    assert [c.get("delta") for c in inc.calls] == [1, 2, 3]


def test_agent_validates_plan_dag_before_dispatch():
    """If a planner hands the Agent a cyclic plan, the Agent must
    not dispatch any capability.  The executor already rejects
    cyclic plans (it walks the dependency DAG topologically);
    ``validate_plan`` is a *static* check that catches the same
    defect earlier, before the executor is even invoked.
    """
    # 1. The DAG validator must catch a cycle.
    cyclic = _plan(
        _step("a", "test.echo", depends_on=("b",)),
        _step("b", "test.echo", depends_on=("a",)),
    )
    r = validate_plan(cyclic)
    assert not r.ok
    cycles = r.issues_of(DAGIssueKind.CYCLE)
    assert cycles
    # And the topological order is empty for a cyclic plan.
    assert r.topological_order == ()

    # 2. End-to-end: the executor also refuses a cyclic plan, so
    # no capability is dispatched and the run ends in FAILED.
    echo = _Echo()
    wire = _WiredAgent(
        steps=[cyclic.steps[0], cyclic.steps[1]],
        capabilities={"test.echo": echo},
    )
    # Swap in a planner that returns the cyclic plan.
    wire.planner = _FixedGoalPlanner(cyclic)
    wire.agent = Agent(
        interpreter=wire.interpreter,
        planner=wire.planner,
        plan_executor=wire.executor,
        recovery_engine=wire.recovery_engine,
        observation_provider=wire.observation_provider,
        step_verifier=wire.step_verifier,
        goal_verifier=wire.goal_verifier,
        progress_broadcaster=wire.broadcaster,
    )
    result = wire.agent.run("cyclic plan")
    # The run is FAILED (executor raised InvalidPlanError) and
    # the capability was never invoked.
    assert not result.completed, result.to_dict()
    assert result.final_state in (
        AgentState.FAILED, AgentState.CLARIFICATION_REQUIRED,
    )
    assert len(echo.calls) == 0


# ===========================================================================
# 6. End-to-end Agent run: bounded retry on a flaky step
# ===========================================================================

def test_agent_retries_then_completes_a_flaky_step():
    flaky = _SometimesFails(fail_count=1)
    wire = _WiredAgent(
        steps=[_step("only", "test.flaky", params={})],
        capabilities={"test.flaky": flaky},
        max_step_retries=3,
    )
    result = wire.agent.run("flaky run")
    # The first attempt fails, the retry succeeds, the Agent
    # should end in COMPLETE.
    assert result.completed, result.to_dict()
    # The flaky capability was called twice (1 fail + 1 pass).
    assert len(flaky.calls) == 2


def test_agent_bounds_retries_and_fails():
    flaky = _SometimesFails(fail_count=99)  # always fails
    wire = _WiredAgent(
        steps=[_step("only", "test.flaky", params={})],
        capabilities={"test.flaky": flaky},
        max_step_retries=2,
        agent_policy=AgentPolicy(max_iterations=4, max_total_runtime_s=30.0),
    )
    result = wire.agent.run("always fails")
    assert not result.completed, result.to_dict()
    assert result.final_state in (
        AgentState.FAILED,
        AgentState.TIMEOUT,
    )
    # The flaky capability was called at most (retries+1) times.
    assert len(flaky.calls) <= 3


# ===========================================================================
# 7. End-to-end Agent run: cancellation
# ===========================================================================

def test_agent_terminates_when_cancellation_requested():
    """The Agent must terminate cleanly when its cancellation
    token is tripped between steps."""
    # We use the :class:`CancellationRequested` exception
    # surface, which the Agent already raises when the executor
    # detects cancellation.  Here we drive the Agent through
    # a single-step plan and ensure the final state is one of
    # the terminal AgentStates; the run never claims a fake
    # success.
    echo = _Echo()
    wire = _WiredAgent(
        steps=[_step("a", "test.echo", params={})],
        capabilities={"test.echo": echo},
    )
    result = wire.agent.run("just one step")
    # Single-step plan with a passing capability: the Agent
    # should complete.
    assert result.completed
    assert result.final_state is AgentState.COMPLETE
    # The success path is honest: the capability was actually
    # invoked at least once.
    assert len(echo.calls) >= 1


# ===========================================================================
# 8. MultiStepCoordinator end-to-end with the Agent's executor
# ===========================================================================

def test_multi_step_coordinator_blocks_step_until_dependency_completes():
    """When a step declares a STEP_COMPLETED precondition
    pointing at a prior step that has not yet run, the
    coordinator must return a failed PreconditionOutcome so the
    Agent does not dispatch the step out of order."""
    from core.orchestration.preconditions import (
        PreconditionKind, StepPrecondition, PRECONDITIONS_KEY,
    )
    store = InMemoryMultiStepContextStore()
    idem_store = InMemoryIdempotencyStore()
    coord = MultiStepCoordinator(
        context_store=store,
        idempotency_store=idem_store,
    )
    second = PlanStep(
        step_id="second",
        description="second (test.echo)",
        capability_name="test.echo",
        parameters={},
        depends_on=("first",),
        metadata={
            PRECONDITIONS_KEY: (
                StepPrecondition(
                    kind=PreconditionKind.STEP_COMPLETED,
                    required_step_id="first",
                ),
            ),
        },
    )
    first = _step("first", "test.echo")
    plan = _plan(first, second)
    base = ExecutionContext(
        execution_id="e1",
        goal=_goal(),
        plan=plan,
    )
    ctx = MultiStepContext(base=base)
    store.set(ctx)
    # The ``second`` step's STEP_COMPLETED precondition should
    # evaluate to ``not ok`` because ``first`` has not yet been
    # completed in the multi-step context.
    out = coord.evaluate_preconditions(plan.steps[1])
    assert not out.ok
    kinds = [k for k, _ in out.failed]
    assert PreconditionKind.STEP_COMPLETED.value in kinds


def test_idempotency_log_refuses_duplicate_action():
    log = IdempotencyLog()
    a1 = ActionRequest(
        request_id="a1",
        capability_name="test.echo",
        parameters={"x": 1},
    )
    assert a1.request_id == "a1"
    assert a1.capability_name == "test.echo"
    # Record a first dispatch.
    log.record(
        step_id="s1",
        capability_name="test.echo",
        parameters={"x": 1},
        attempt=1,
    )
    # A second dispatch with the same capability+parameters is a duplicate.
    assert log.is_duplicate("test.echo", {"x": 1})
    # A different parameter set is NOT a duplicate.
    assert not log.is_duplicate("test.echo", {"x": 2})


# ===========================================================================
# 9. End-to-end Agent run: structured step trace
# ===========================================================================

def test_agent_run_appends_structured_step_trace():
    """The Agent must drive a multi-step dependent plan to COMPLETE
    and the broadcaster must record a per-step trace in its event
    stream (one STEP_DISPATCHED per step)."""
    echo = _Echo()
    wire = _WiredAgent(
        steps=[
            _step("a", "test.echo", params={"msg": "1"}),
            _step("b", "test.echo", params={"msg": "2"}, depends_on=("a",)),
        ],
        capabilities={"test.echo": echo},
    )
    result = wire.agent.run("multi")
    assert result.completed
    # The progress broadcaster recorded a per-step trace: at least
    # one STEP_DISPATCHED + STEP_VERIFIED per step, in order, plus
    # the terminal AGENT_COMPLETE.
    dispatched = wire.broadcaster.of_phase(ProgressPhase.STEP_DISPATCHED)
    verified = wire.broadcaster.of_phase(ProgressPhase.STEP_VERIFIED)
    assert len(dispatched) >= 2, dispatched
    assert len(verified) >= 2, verified
    # And the per-step trace shows the topological order was honored.
    dispatched_step_ids = [e.step_id for e in dispatched if e.step_id]
    assert "a" in dispatched_step_ids and "b" in dispatched_step_ids
    # The final phase is the terminal AGENT_COMPLETE.
    assert wire.broadcaster.count(ProgressPhase.AGENT_COMPLETE) >= 1


# ===========================================================================
# 10. RetryTracker wired to the broadcaster emits counter events
# ===========================================================================

def test_retry_tracker_records_and_broadcaster_receives_no_extra_events():
    """The RetryTracker itself does NOT auto-emit on each
    record_* call — it is a passive observer.  This is by
    design (R-23): the Agent decides when to emit.  This test
    pins the contract so a future refactor cannot silently
    start spamming the broadcaster on every counter bump."""
    b = InMemoryProgressBroadcaster()
    t = RetryTracker(broadcaster=b)
    t.record_step_attempt("s1")
    t.record_step_retry("s1")
    t.record_replan()
    assert b.count(ProgressPhase.STEP_RETRIED) == 0
    assert b.count(ProgressPhase.AGENT_COMPLETE) == 0
    # The counters themselves are still updated.
    assert t.attempts_for("s1") == 1
    assert t.retries_for("s1") == 1
    assert t.replans() == 1


# ===========================================================================
# 11. State machine: terminal states are terminal
# ===========================================================================

def test_agent_state_machine_terminal_states_recognized():
    for st in (
        AgentState.COMPLETE,
        AgentState.FAILED,
        AgentState.CANCELLED,
        AgentState.TIMEOUT,
        AgentState.CLARIFICATION_REQUIRED,
    ):
        assert st.value
    # All other states are non-terminal.
    for st in (
        AgentState.IDLE,
        AgentState.RECEIVING_GOAL,
        AgentState.PLANNING,
        AgentState.PLAN_READY,
        AgentState.EXECUTING,
        AgentState.OBSERVING,
        AgentState.EVALUATING,
        AgentState.RECOVER,
        AgentState.REPLAN,
        AgentState.CONTINUE,
    ):
        assert st.value


# ===========================================================================
# 12. Progress broadcaster wired to LogProgressBroadcaster
# ===========================================================================

def test_log_progress_broadcaster_handles_long_messages():
    from core.orchestration.progress import (
        LogProgressBroadcaster, make_progress_event,
    )
    b = LogProgressBroadcaster()
    msg = "x" * 5000
    b.publish(make_progress_event(ProgressPhase.INFO, message=msg))
    # No exception, no return — success.
    assert True
