"""
Omnix V6 — Phase 4 orchestration foundation tests.

These tests validate:

    1. The shape of every model the AI Orchestration Foundation
       promises (Goal, Intent, Plan, PlanStep, ActionRequest,
       ExecutionContext, Observation, ExpectedEffect, Failure,
       RecoveryDecision, VerificationVerdict, plus the Verifier
       protocol).
    2. The shape of every interface contract (IntentInterpreter,
       Planner, PlanExecutor, Orchestrator).
    3. The **security boundary**: no model in the orchestration
       layer accepts a payload that smells like a shell escape.
       A test that tries to construct an ``ActionRequest`` with
       ``"&& rm -rf /"`` as a parameter value must raise.
    4. The immutability of the models (frozen dataclasses).
    5. The ``with_*`` snapshot updates work as documented.

The point of these tests is to *lock in* the domain contracts so
later phases can rely on them, and to fail loudly if any payload
tries to bypass the closed capability set (R-21).
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
    Failure,
    FailureKind,
    RecoveryDecision,
    RecoveryAction,
    Verifier,
    VerificationVerdict,
    count_decorator,
    IntentInterpreter,
    Planner,
    PlanExecutor,
    Orchestrator,
)


# ===========================================================================
# Goal
# ===========================================================================

class TestGoal:
    def test_minimal_construction(self):
        g = Goal(goal_id="g1", description="open chrome")
        assert g.goal_id == "g1"
        assert g.description == "open chrome"
        assert g.success_criteria == ()
        assert g.priority == 0
        assert g.metadata == {}

    def test_full_construction(self):
        g = Goal(
            goal_id="g2",
            description="prepare for exam",
            success_criteria=("notes_reviewed", "quiz_passed"),
            constraints=("no_send", "offline_only"),
            priority=5,
            created_at=100.0,
            metadata={"source": "user"},
        )
        assert g.success_criteria == ("notes_reviewed", "quiz_passed")
        assert g.priority == 5
        assert g.created_at == 100.0

    def test_to_dict_round_trip_shape(self):
        g = Goal(goal_id="g3", description="x", success_criteria=("a",))
        d = g.to_dict()
        assert d["type"] == "Goal"
        assert d["goal_id"] == "g3"
        assert d["success_criteria"] == ["a"]

    def test_is_frozen(self):
        g = Goal(goal_id="g4", description="x")
        with pytest.raises(Exception):  # FrozenInstanceError
            g.goal_id = "mutated"  # type: ignore[misc]


# ===========================================================================
# Intent + IntentKind
# ===========================================================================

class TestIntent:
    def test_construction(self):
        i = Intent(
            intent_id="i1",
            kind=IntentKind.COMMAND,
            text="open chrome",
            confidence=0.9,
            referenced_entities=("chrome",),
            referenced_goal_id="g1",
        )
        assert i.kind is IntentKind.COMMAND
        assert i.confidence == 0.9
        assert i.referenced_entities == ("chrome",)

    def test_with_confidence_clamps(self):
        i = Intent(intent_id="i2", kind=IntentKind.QUERY, text="?", confidence=0.0)
        assert i.with_confidence(2.0).confidence == 1.0
        assert i.with_confidence(-0.5).confidence == 0.0
        assert i.with_confidence(0.4).confidence == 0.4

    def test_to_dict(self):
        i = Intent(intent_id="i3", kind=IntentKind.CANCEL, text="stop")
        d = i.to_dict()
        assert d["kind"] == "cancel"
        assert d["type"] == "Intent"

    def test_intent_kind_values(self):
        assert IntentKind.INFORM.value == "inform"
        assert IntentKind.QUERY.value == "query"
        assert IntentKind.COMMAND.value == "command"
        assert IntentKind.CLARIFY.value == "clarify"
        assert IntentKind.CANCEL.value == "cancel"
        assert IntentKind.UNKNOWN.value == "unknown"


# ===========================================================================
# ActionRequest — the security boundary
# ===========================================================================

class TestActionRequest:
    def test_basic_construction(self):
        ar = ActionRequest(
            capability_name="application.open",
            parameters={"app_name": "chrome"},
            request_id="ar1",
        )
        assert ar.capability_name == "application.open"
        assert ar.parameters == {"app_name": "chrome"}

    def test_empty_capability_name_rejected(self):
        with pytest.raises(ValueError):
            ActionRequest(capability_name="", parameters={})
        with pytest.raises(ValueError):
            ActionRequest(capability_name="   ", parameters={})

    def test_to_dict(self):
        ar = ActionRequest(
            capability_name="application.open",
            parameters={"app_name": "chrome"},
            request_id="ar2",
            issued_at=42.0,
        )
        d = ar.to_dict()
        assert d["type"] == "ActionRequest"
        assert d["capability_name"] == "application.open"
        assert d["parameters"] == {"app_name": "chrome"}
        assert d["issued_at"] == 42.0

    def test_is_frozen(self):
        ar = ActionRequest(capability_name="x", parameters={})
        with pytest.raises(Exception):
            ar.capability_name = "y"  # type: ignore[misc]

    # --- the security boundary --------------------------------------------

    @pytest.mark.parametrize(
        "forbidden",
        [
            "ls && rm -rf /",
            "open $(whoami)",
            "echo `hostname`",
            "cat file > /etc/passwd",
            "echo < /etc/shadow",
            "shutdown now",
            "format c:",
        ],
    )
    def test_parameters_reject_shell_tokens(self, forbidden):
        """A shell-like payload in any parameter value must be rejected.

        This is the foundation's *static* defense against R-21
        violations: the orchestration layer cannot carry a shell
        escape even if some other layer was careless.
        """
        with pytest.raises(ValueError):
            ActionRequest(
                capability_name="application.open",
                parameters={"app_name": forbidden},
            )

    def test_capability_name_rejects_shell_tokens(self):
        with pytest.raises(ValueError):
            ActionRequest(
                capability_name="x; rm -rf /",
                parameters={},
            )

    def test_nested_dict_rejects_shell_tokens(self):
        with pytest.raises(ValueError):
            ActionRequest(
                capability_name="x",
                parameters={"nested": {"app_name": "ok && bad"}},
            )

    def test_list_param_rejects_shell_tokens(self):
        with pytest.raises(ValueError):
            ActionRequest(
                capability_name="x",
                parameters={"items": ["a", "b; rm -rf /"]},
            )

    def test_benign_payload_with_substring_passes(self):
        # The forbidden pattern is a regex with `|`.  "open Chrome's
        # home" is benign even though the apostrophe and the word
        # 'home' are not special.
        ar = ActionRequest(
            capability_name="application.open",
            parameters={"app_name": "Chrome"},
        )
        assert ar.parameters["app_name"] == "Chrome"


# ===========================================================================
# ExpectedEffect
# ===========================================================================

class TestExpectedEffect:
    def test_construction(self):
        e = ExpectedEffect(
            check_name="app_is_running",
            expected="chrome",
            timeout_s=2.0,
            description="chrome is foreground",
        )
        assert e.check_name == "app_is_running"
        assert e.expected == "chrome"
        assert e.timeout_s == 2.0

    def test_to_dict(self):
        e = ExpectedEffect(check_name="x", expected=True, timeout_s=1.0)
        d = e.to_dict()
        assert d["type"] == "ExpectedEffect"
        assert d["check_name"] == "x"
        assert d["expected"] is True

    def test_shell_token_in_expected_rejected_via_action_request(self):
        e = ExpectedEffect(
            check_name="x",
            expected="value && rm -rf /",
        )
        with pytest.raises(ValueError):
            ActionRequest(
                capability_name="application.open",
                parameters={},
                expected_effect=e,
            )


# ===========================================================================
# Observation
# ===========================================================================

class TestObservation:
    def test_construction(self):
        o = Observation(
            source=ObservationSource.SCREEN,
            data={"width": 1920, "height": 1080},
            timestamp=time.time(),
            subject="desktop",
            confidence=0.9,
        )
        assert o.source is ObservationSource.SCREEN
        assert o.data["width"] == 1920
        assert o.confidence == 0.9

    def test_to_dict(self):
        o = Observation(source=ObservationSource.WORLD, subject="app")
        d = o.to_dict()
        assert d["type"] == "Observation"
        assert d["source"] == "world"

    def test_all_observation_sources_are_strings(self):
        for s in ObservationSource:
            assert isinstance(s.value, str)


# ===========================================================================
# PlanStep
# ===========================================================================

class TestPlanStep:
    def test_basic_construction(self):
        s = PlanStep(
            step_id="s1",
            description="open chrome",
            capability_name="application.open",
            parameters={"app_name": "chrome"},
        )
        assert s.step_id == "s1"
        assert s.action is ActionKind.CAPABILITY_CALL
        assert s.depends_on == ()
        assert s.max_retries == 0
        assert s.timeout_s == 30.0

    def test_capability_call_requires_capability_name(self):
        with pytest.raises(ValueError):
            PlanStep(
                step_id="s1",
                description="x",
                action=ActionKind.CAPABILITY_CALL,
                capability_name="",
            )

    def test_observe_step_does_not_require_capability_name(self):
        # OBSERVE is a non-side-effecting sensor step; no capability
        # name is needed.  Must not raise.
        s = PlanStep(
            step_id="obs1",
            description="sense world",
            action=ActionKind.OBSERVE,
        )
        assert s.action is ActionKind.OBSERVE

    def test_shell_token_in_capability_name_rejected(self):
        with pytest.raises(ValueError):
            PlanStep(
                step_id="s1",
                description="x",
                capability_name="app; rm -rf /",
            )

    def test_shell_token_in_parameters_rejected(self):
        with pytest.raises(ValueError):
            PlanStep(
                step_id="s1",
                description="x",
                capability_name="application.open",
                parameters={"app_name": "ok && bad"},
            )

    def test_to_dict(self):
        s = PlanStep(
            step_id="s1",
            description="x",
            capability_name="application.open",
            parameters={"app_name": "chrome"},
            depends_on=("s0",),
        )
        d = s.to_dict()
        assert d["type"] == "PlanStep"
        assert d["action"] == "capability_call"
        assert d["depends_on"] == ["s0"]

    def test_is_frozen(self):
        s = PlanStep(step_id="s1", description="x", capability_name="x.y")
        with pytest.raises(Exception):
            s.step_id = "mutated"  # type: ignore[misc]


# ===========================================================================
# Plan
# ===========================================================================

class TestPlan:
    def _two_step_plan(self) -> Plan:
        return Plan(
            plan_id="p1",
            goal_id="g1",
            steps=(
                PlanStep(step_id="s1", description="a", capability_name="x.a"),
                PlanStep(step_id="s2", description="b", capability_name="x.b",
                         depends_on=("s1",)),
            ),
        )

    def test_construction(self):
        p = self._two_step_plan()
        assert p.step_count == 2
        assert p.status is PlanStatus.DRAFT
        assert p.replan_count == 0
        assert p.parent_plan_id is None

    def test_step_ids(self):
        p = self._two_step_plan()
        assert p.step_ids == ("s1", "s2")

    def test_find_step(self):
        p = self._two_step_plan()
        assert p.find_step("s2") is not None
        assert p.find_step("nope") is None

    def test_with_status(self):
        p = self._two_step_plan()
        p2 = p.with_status(PlanStatus.EXECUTING)
        assert p2.status is PlanStatus.EXECUTING
        assert p.status is PlanStatus.DRAFT  # immutable

    def test_append_step(self):
        p = self._two_step_plan()
        new_step = PlanStep(step_id="s3", description="c", capability_name="x.c")
        p2 = p.append_step(new_step)
        assert p2.step_count == 3
        assert p.step_count == 2  # original is unchanged

    def test_with_steps(self):
        p = self._two_step_plan()
        new_steps = (PlanStep(step_id="s_new", description="n", capability_name="x.n"),)
        p2 = p.with_steps(new_steps)
        assert p2.step_count == 1
        assert p.step_count == 2

    def test_to_dict(self):
        p = self._two_step_plan()
        d = p.to_dict()
        assert d["type"] == "Plan"
        assert d["status"] == "draft"
        assert d["step_count"] == 2
        assert len(d["steps"]) == 2


# ===========================================================================
# ExecutionContext
# ===========================================================================

class TestExecutionContext:
    def _context(self) -> ExecutionContext:
        goal = Goal(goal_id="g1", description="x")
        plan = Plan(
            plan_id="p1",
            goal_id="g1",
            steps=(
                PlanStep(step_id="s1", description="a", capability_name="x.a"),
                PlanStep(step_id="s2", description="b", capability_name="x.b"),
            ),
        )
        return ExecutionContext(
            execution_id="e1",
            goal=goal,
            plan=plan,
            current_step_id="s1",
            started_at=time.time(),
        )

    def test_construction(self):
        ctx = self._context()
        assert ctx.execution_id == "e1"
        assert ctx.goal.goal_id == "g1"
        assert ctx.plan.step_count == 2
        assert ctx.completed_step_ids == ()
        assert ctx.failed_step_ids == ()

    def test_progress_zero_initially(self):
        assert self._context().progress == 0.0

    def test_with_completed_updates_progress(self):
        ctx = self._context()
        ctx2 = ctx.with_completed("s1")
        assert ctx2.progress == 0.5
        # Original is unchanged
        assert ctx.completed_step_ids == ()
        assert ctx.progress == 0.0

    def test_with_completed_is_idempotent(self):
        ctx = self._context().with_completed("s1")
        ctx2 = ctx.with_completed("s1")
        assert ctx2.completed_step_ids == ("s1",)

    def test_with_failed_tracks_failures_separately(self):
        ctx = self._context()
        ctx2 = ctx.with_failed("s2")
        assert ctx2.failed_step_ids == ("s2",)
        assert ctx2.completed_step_ids == ()

    def test_with_current_step(self):
        ctx = self._context()
        ctx2 = ctx.with_current_step("s2")
        assert ctx2.current_step_id == "s2"

    def test_to_dict(self):
        ctx = self._context()
        d = ctx.to_dict()
        assert d["type"] == "ExecutionContext"
        assert d["execution_id"] == "e1"
        assert d["plan"]["type"] == "Plan"
        assert d["goal"]["type"] == "Goal"


# ===========================================================================
# VerificationVerdict — the tri-state invariant (R-8)
# ===========================================================================

class TestVerificationVerdict:
    def test_passed(self):
        v = VerificationVerdict(
            passed=True, failed=False, uncertain=False,
            check_name="x", expected=1, actual=1,
        )
        assert v.passed is True

    def test_failed(self):
        v = VerificationVerdict(
            passed=False, failed=True, uncertain=False,
            check_name="x", expected=1, actual=2,
        )
        assert v.failed is True

    def test_uncertain(self):
        v = VerificationVerdict(
            passed=False, failed=False, uncertain=True,
            check_name="x", reason="sensor timed out",
        )
        assert v.uncertain is True

    def test_exactly_one_flag_required(self):
        with pytest.raises(ValueError):
            VerificationVerdict(
                passed=True, failed=True, uncertain=False,
                check_name="x",
            )
        with pytest.raises(ValueError):
            VerificationVerdict(
                passed=False, failed=False, uncertain=False,
                check_name="x",
            )
        with pytest.raises(ValueError):
            VerificationVerdict(
                passed=True, failed=False, uncertain=True,
                check_name="x",
            )

    def test_to_dict(self):
        v = VerificationVerdict(
            passed=False, failed=True, uncertain=False,
            check_name="x", reason="mismatch",
        )
        d = v.to_dict()
        assert d["type"] == "VerificationVerdict"
        assert d["failed"] is True


# ===========================================================================
# Verifier protocol
# ===========================================================================

class TestVerifierProtocol:
    def test_structural_protocol_accepts_minimum_implementation(self):
        # A class with the required attribute + method is accepted
        # by ``isinstance(..., Verifier)`` (runtime_checkable).
        class MyVerifier:
            name = "step_verifier"

            def verify(self, *, effect, observation, context):
                return VerificationVerdict(
                    passed=True, failed=False, uncertain=False,
                    check_name=effect.check_name,
                )

        v = MyVerifier()
        assert isinstance(v, Verifier)

        effect = ExpectedEffect(check_name="app_is_running")
        obs = Observation(source=ObservationSource.WORLD)
        ctx = ExecutionContext(
            execution_id="e1",
            goal=Goal(goal_id="g", description="d"),
            plan=Plan(plan_id="p", goal_id="g"),
        )
        verdict = v.verify(effect=effect, observation=obs, context=ctx)
        assert verdict.passed is True


# ===========================================================================
# Failure
# ===========================================================================

class TestFailure:
    def test_minimal_construction(self):
        f = Failure(
            failure_id="f1",
            kind=FailureKind.EXECUTION,
            message="could not click",
        )
        assert f.failure_id == "f1"
        assert f.kind is FailureKind.EXECUTION
        assert f.is_retryable is True
        assert f.attempt == 0

    def test_with_observation(self):
        obs = Observation(source=ObservationSource.SCREEN, subject="x")
        f = Failure(
            failure_id="f1",
            kind=FailureKind.VERIFICATION,
            step_id="s1",
            observation=obs,
            cause="ValueError: nope",
        )
        d = f.to_dict()
        assert d["observation"]["source"] == "screen"
        assert d["cause"] == "ValueError: nope"

    def test_all_failure_kinds_are_strings(self):
        for k in FailureKind:
            assert isinstance(k.value, str)


# ===========================================================================
# RecoveryDecision
# ===========================================================================

class TestRecoveryDecision:
    def test_retry(self):
        r = RecoveryDecision(
            decision_id="d1",
            action=RecoveryAction.RETRY,
            failure_id="f1",
            rationale="transient",
        )
        assert r.action is RecoveryAction.RETRY
        assert r.backoff_s == 0.0

    def test_retry_with_backoff(self):
        r = RecoveryDecision(
            decision_id="d2",
            action=RecoveryAction.RETRY_WITH_BACKOFF,
            failure_id="f1",
            backoff_s=2.5,
        )
        assert r.backoff_s == 2.5

    def test_replan_with_new_step(self):
        new_step = PlanStep(
            step_id="s_fix", description="x", capability_name="x",
        )
        r = RecoveryDecision(
            decision_id="d3",
            action=RecoveryAction.REPLAN,
            failure_id="f1",
            new_step=new_step,
        )
        assert r.new_step is new_step

    def test_ask_user(self):
        r = RecoveryDecision(
            decision_id="d4",
            action=RecoveryAction.ASK_USER,
            failure_id="f1",
            ask_user_message="What should I do?",
        )
        assert r.ask_user_message == "What should I do?"


# ===========================================================================
# count_decorator
# ===========================================================================

class TestCountDecorator:
    def test_counts_invocations(self):
        counter: dict = {}

        @count_decorator(counter, "calls")
        def hello(name: str) -> str:
            return f"hi {name}"

        assert hello("alice") == "hi alice"
        assert hello("bob") == "hi bob"
        assert counter["calls"] == 2

    def test_distinct_keys_have_distinct_counts(self):
        counter: dict = {}

        @count_decorator(counter, "a")
        def a() -> int:
            return 1

        @count_decorator(counter, "b")
        def b() -> int:
            return 2

        a()
        a()
        b()
        assert counter == {"a": 2, "b": 1}


# ===========================================================================
# Interface contracts
# ===========================================================================

class _StubInterpreter:
    """Minimal stand-in for an IntentInterpreter implementation."""

    name = "stub_interpreter"

    def interpret(self, text, *, context_snapshot=None):
        return Intent(
            intent_id="stub",
            kind=IntentKind.COMMAND,
            text=text,
            confidence=1.0,
        )


class _StubPlanner:
    name = "stub_planner"

    def plan(self, goal, *, intent=None, context_snapshot=None,
             prior_plan=None, failure=None):
        step = PlanStep(
            step_id="s1", description="do it", capability_name="x.do",
        )
        return Plan(plan_id="new", goal_id=goal.goal_id, steps=(step,))


class _StubExecutor:
    name = "stub_executor"

    def execute(self, context):
        return context.with_status_snapshot(PlanStatus.COMPLETED) \
            if hasattr(context, "with_status_snapshot") else context

    def execute_step(self, context, step):
        return context.with_completed(step.step_id)


class _StubRecovery:
    name = "stub_recovery"

    def decide(self, failure, context, *, history=None):
        return RecoveryDecision(
            decision_id="d1",
            action=RecoveryAction.RETRY,
            failure_id=failure.failure_id,
        )


class _StubOrchestrator:
    name = "stub_orchestrator"

    def handle_user_input(self, text, *, context_snapshot=None):
        return ExecutionContext(
            execution_id="e",
            goal=Goal(goal_id="g", description=text),
            plan=Plan(plan_id="p", goal_id="g"),
        )

    def step(self, context):
        return context

    def replan(self, context, failure):
        return context

    def cancel(self, context, *, reason=""):
        return context


class TestInterfaceConformance:
    """Verify that minimal stand-ins satisfy the Protocol contracts."""

    def test_intent_interpreter_protocol(self):
        assert isinstance(_StubInterpreter(), IntentInterpreter)

    def test_planner_protocol(self):
        assert isinstance(_StubPlanner(), Planner)

    def test_plan_executor_protocol(self):
        assert isinstance(_StubExecutor(), PlanExecutor)

    def test_orchestrator_protocol(self):
        assert isinstance(_StubOrchestrator(), Orchestrator)

    def test_intent_interpreter_returns_intent(self):
        i = _StubInterpreter().interpret("hello", context_snapshot={})
        assert isinstance(i, Intent)
        assert i.kind is IntentKind.COMMAND

    def test_planner_returns_plan_with_closed_capability(self):
        p = _StubPlanner().plan(Goal(goal_id="g", description="d"))
        assert isinstance(p, Plan)
        assert all(s.capability_name for s in p.steps)

    def test_plan_executor_advances_completed_set(self):
        ex = _StubExecutor()
        ctx = ExecutionContext(
            execution_id="e",
            goal=Goal(goal_id="g", description="d"),
            plan=Plan(
                plan_id="p", goal_id="g",
                steps=(PlanStep(step_id="s1", description="x", capability_name="y"),),
            ),
        )
        ctx2 = ex.execute_step(ctx, ctx.plan.steps[0])
        assert ctx2.completed_step_ids == ("s1",)


# ===========================================================================
# Cross-cutting: the whole package is importable
# ===========================================================================

class TestPublicAPI:
    def test_all_models_importable_from_package_root(self):
        from core import orchestration
        names = {
            "Goal", "Intent", "IntentKind", "Plan", "PlanStep", "PlanStatus",
            "ActionRequest", "ActionKind", "ExecutionContext",
            "Observation", "ObservationSource", "ExpectedEffect",
            "Verifier", "VerificationVerdict",
            "Failure", "FailureKind", "RecoveryDecision", "RecoveryAction",
            "count_decorator",
            "IntentInterpreter", "Planner", "PlanExecutor", "Orchestrator",
        }
        for n in names:
            assert hasattr(orchestration, n), f"missing: {n}"

    def test_orchestration_does_not_import_omnix_engine(self):
        """R-1: the orchestration layer must not depend on the engine."""
        import re
        from pathlib import Path
        import core.orchestration as pkg
        pkg_path = Path(pkg.__file__).parent
        # Look only at *import statements*, not at docstring or
        # comment text.  An actual import is on a line that starts
        # (after whitespace) with ``import`` or ``from``.
        import_re = re.compile(
            r"^\s*(?:import\s+(\S+)|from\s+(\S+)\s+import)",
            re.MULTILINE,
        )
        for py in pkg_path.glob("*.py"):
            text = py.read_text(encoding="utf-8")
            for m in import_re.finditer(text):
                mod = m.group(1) or m.group(2)
                assert "omnix_engine" not in mod, (
                    f"{py.name} imports {mod!r}; R-1 violation "
                    "(orchestration must not depend on the engine)"
                )

    def test_orchestration_does_not_import_subprocess_or_os_system(self):
        """R-21: the orchestration layer must not bring in shell primitives."""
        import re
        from pathlib import Path
        import core.orchestration as pkg
        pkg_path = Path(pkg.__file__).parent
        import_re = re.compile(
            r"^\s*(?:import\s+(\S+)|from\s+(\S+)\s+import)",
            re.MULTILINE,
        )
        forbidden_top_level = {"subprocess", "popen2"}
        for py in pkg_path.glob("*.py"):
            text = py.read_text(encoding="utf-8")
            for m in import_re.finditer(text):
                mod = (m.group(1) or m.group(2) or "").split(".")[0]
                assert mod not in forbidden_top_level, (
                    f"{py.name} imports {mod!r}; R-21 violation"
                )
            # Also reject the specific dangerous calls anywhere in
            # the file (text-search; these names are unmistakable).
            for tok in ("os.system", "os.popen", "os.exec", "os.spawn"):
                assert tok not in text, (
                    f"{py.name} references {tok}; R-21 violation"
                )
