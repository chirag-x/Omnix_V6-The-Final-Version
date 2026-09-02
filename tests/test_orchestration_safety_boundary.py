"""
Omnix V6 — Phase 4 safety boundary test.

This test proves the *static* and *structural* defenses the
orchestration foundation puts in place against R-21 violations
(``Planner`` / ``PlanExecutor`` / ``Orchestrator`` MUST NOT execute
shell commands, GUI automation primitives, or any other OS-level
action directly — the only authorized path is through
``CapabilityRouter``).

It does NOT try to be clever.  It is deliberately
``import``-oriented and ``str``-oriented because the rule is a
*source-level* invariant: no path through the foundation may reach
``subprocess``, ``pyautogui``, ``win32gui`` (or their equivalents).

The test is split into three groups:

1. **Source-level audit** — the orchestration package must not
   import forbidden top-level modules, and the ``Planner`` /
   ``PlanExecutor`` / ``Orchestrator`` Protocol method bodies do
   not exist (they are abstract).
2. **Construction-time audit** — domain models refuse to *carry*
   shell-like payloads, no matter how the caller formed them.
3. **Behavioural audit** — even a perfectly well-behaved
   ``Planner`` implementation cannot cause side effects on the OS,
   because its only return type is :class:`Plan` (a frozen
   dataclass with no IO).
"""

import re
import textwrap
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from core.orchestration import (
    Goal,
    Plan,
    PlanStep,
    PlanStatus,
    ActionRequest,
    ActionKind,
    ExecutionContext,
    Intent,
    IntentKind,
    Failure,
    FailureKind,
    RecoveryDecision,
    RecoveryAction,
    Planner,
    PlanExecutor,
    Orchestrator,
    IntentInterpreter,
    count_decorator,
)

# These are the modules a Planner / Executor MUST NOT reach.
# They cover POSIX shell, Windows GUI automation, and the Python
# popen/spawn family.
FORBIDDEN_TOP_LEVEL_MODULES = {
    "subprocess",
    "popen2",
    "pyautogui",
    "win32gui",
    "win32api",
    "win32con",
    "ctypes",  # too low-level for the orchestration foundation
    "cffi",
}

# These are *call patterns* that would also be R-21 violations
# even if the top-level module is not in the set above.
FORBIDDEN_CALL_PATTERNS = (
    "os.system",
    "os.popen",
    "os.exec",
    "os.spawn",
    "subprocess.run",
    "subprocess.Popen",
    "subprocess.call",
    "subprocess.check_output",
    "pyautogui.click",
    "pyautogui.typewrite",
    "pyautogui.hotkey",
    "pyautogui.moveTo",
    "win32gui.FindWindow",
    "win32api.keybd_event",
    "win32api.mouse_event",
    "ctypes.windll",
    "ShellExecute",
)


# ---------------------------------------------------------------------------
# 1. Source-level audit
# ---------------------------------------------------------------------------

class TestOrchestrationSourceDoesNotReachShellOrGUI:
    """The orchestration package is a *contracts-only* layer.

    The check is a pure textual audit: scan the package's Python
    source for import statements and forbidden call names.  No
    runtime introspection needed.
    """

    def _pkg_path(self) -> Path:
        import core.orchestration as pkg
        return Path(pkg.__file__).parent

    def _iter_py_files(self) -> List[Path]:
        return list(self._pkg_path().glob("*.py"))

    def test_no_forbidden_top_level_imports(self):
        import_re = re.compile(
            r"^\s*(?:import\s+(\S+)|from\s+(\S+)\s+import)",
            re.MULTILINE,
        )
        for py in self._iter_py_files():
            text = py.read_text(encoding="utf-8")
            for m in import_re.finditer(text):
                mod = (m.group(1) or m.group(2) or "").split(".")[0]
                assert mod not in FORBIDDEN_TOP_LEVEL_MODULES, (
                    f"{py.name} imports top-level {mod!r}; "
                    "R-21 violation: orchestration cannot reach OS primitives"
                )

    def test_no_forbidden_call_patterns_anywhere(self):
        # Pure text search.  These strings are unmistakable; even if
        # they appear inside a docstring we want to know.
        for py in self._iter_py_files():
            text = py.read_text(encoding="utf-8")
            for tok in FORBIDDEN_CALL_PATTERNS:
                assert tok not in text, (
                    f"{py.name} references {tok!r}; "
                    "R-21 violation: orchestration cannot reach OS primitives"
                )


# ---------------------------------------------------------------------------
# 2. Construction-time audit — the foundation refuses to carry
#    shell-like payloads.
# ---------------------------------------------------------------------------

class TestDomainModelsRefuseShellPayloads:
    """Even if a caller tries to *construct* a malicious step or
    request, the models reject the payload at construction time.
    """

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
            "del /f /q C:\\Windows",
        ],
    )
    def test_action_request_rejects_shell_in_parameters(self, forbidden):
        with pytest.raises(ValueError):
            ActionRequest(
                capability_name="application.open",
                parameters={"app_name": forbidden},
            )

    def test_action_request_rejects_shell_in_capability_name(self):
        with pytest.raises(ValueError):
            ActionRequest(
                capability_name="os.system; rm -rf /",
                parameters={},
            )

    def test_plan_step_rejects_shell_in_parameters(self):
        with pytest.raises(ValueError):
            PlanStep(
                step_id="s1",
                description="open",
                capability_name="application.open",
                parameters={"app_name": "chrome && rm -rf /"},
            )

    def test_nested_dict_in_parameters_rejected(self):
        with pytest.raises(ValueError):
            ActionRequest(
                capability_name="x",
                parameters={"nested": {"app_name": "ok && bad"}},
            )

    def test_list_in_parameters_rejected(self):
        with pytest.raises(ValueError):
            ActionRequest(
                capability_name="x",
                parameters={"items": ["a", "b; rm -rf /"]},
            )

    def test_unicode_shell_obfuscation_rejected(self):
        # Fullwidth semicolon that some shells normalise.
        with pytest.raises(ValueError):
            ActionRequest(
                capability_name="x",
                parameters={"app_name": "chrome\uff1b rm -rf /"},
            )


# ---------------------------------------------------------------------------
# 3. Behavioural audit — the Protocol contracts cannot be used
#    to bypass the router.
# ---------------------------------------------------------------------------

class _StaticPlanner:
    """A minimal ``Planner`` that just returns a one-step plan.

    The point is to prove that *any* Planner implementation is
    structurally constrained to return a :class:`Plan`; it has no
    way to perform IO.
    """

    name = "static"

    def plan(self, goal, *, intent=None, context_snapshot=None,
             prior_plan=None, failure=None):
        step = PlanStep(
            step_id="s1",
            description="noop",
            capability_name="test.echo",
            parameters={"text": "hi"},
        )
        return Plan(plan_id="p_static", goal_id=goal.goal_id, steps=(step,))


class _StaticExecutor:
    """A minimal ``PlanExecutor`` that just records the call.

    Crucially, it has no shell / GUI hooks — its only action is to
    return a new :class:`ExecutionContext` built from the existing
    one.
    """

    name = "static"

    def execute(self, context):
        return context.with_completed("s1") if "s1" in context.plan.step_ids else context

    def execute_step(self, context, step):
        return context


class _StaticOrchestrator:
    """A minimal ``Orchestrator`` that wires the static pieces."""

    name = "static"

    def handle_user_input(self, text, *, context_snapshot=None):
        goal = Goal(goal_id="g", description=text or "")
        plan = Plan(plan_id="p", goal_id=goal.goal_id)
        return ExecutionContext(
            execution_id="e", goal=goal, plan=plan,
        )

    def step(self, context):
        return context

    def replan(self, context, failure):
        return context

    def cancel(self, context, *, reason=""):
        return context.with_status_note(reason) if hasattr(context, "with_status_note") else context


class TestProtocolContractsAreClosedSets:
    """A Planner, an Executor, and an Orchestrator are forced by
    their Protocol contracts to return frozen dataclasses.  None of
    those types have any IO method.  This is the structural
    guarantee: the foundation's *type surface* is closed.
    """

    def test_planner_signature_returns_only_plan(self):
        sig = Planner.plan
        # The first declared positional return annotation is Plan.
        hints = sig.__annotations__ if hasattr(sig, "__annotations__") else {}
        # Not every Protocol method has runtime annotations; check
        # the textual signature.
        import inspect
        source = inspect.getsource(Planner)
        assert "-> Plan" in source, (
            "Planner.plan must declare `-> Plan`; "
            "an open return type is an R-21 hazard"
        )

    def test_executor_signature_returns_only_execution_context(self):
        import inspect
        source = inspect.getsource(PlanExecutor)
        assert "-> ExecutionContext" in source, (
            "PlanExecutor.execute must declare `-> ExecutionContext`; "
            "an open return type is an R-21 hazard"
        )

    def test_orchestrator_signature_returns_only_execution_context(self):
        import inspect
        source = inspect.getsource(Orchestrator)
        assert "-> ExecutionContext" in source, (
            "Orchestrator.handle_user_input must return ExecutionContext; "
            "an open return type is an R-21 hazard"
        )

    def test_static_planner_returns_frozen_plan(self):
        goal = Goal(goal_id="g1", description="x")
        plan = _StaticPlanner().plan(goal)
        assert plan.plan_id == "p_static"
        assert plan.step_count == 1
        # A Plan is frozen: mutation is impossible.
        with pytest.raises(Exception):
            plan.plan_id = "tampered"  # type: ignore[misc]

    def test_static_executor_returns_frozen_context(self):
        plan = Plan(
            plan_id="p", goal_id="g",
            steps=(PlanStep(step_id="s1", description="x", capability_name="x"),),
        )
        ctx = ExecutionContext(execution_id="e", goal=Goal(goal_id="g", description="x"), plan=plan)
        out = _StaticExecutor().execute(ctx)
        assert isinstance(out, ExecutionContext)
        # Frozen: cannot mutate.
        with pytest.raises(Exception):
            out.execution_id = "tampered"  # type: ignore[misc]

    def test_static_orchestrator_does_not_touch_io(self):
        ctx = _StaticOrchestrator().handle_user_input("hello")
        assert isinstance(ctx, ExecutionContext)
        # No new attribute "subprocess_handle" or similar was created.
        for attr in ("subprocess_handle", "popen_obj", "window_handle"):
            assert not hasattr(ctx, attr), (
                f"ExecutionContext must not carry {attr}; R-21 violation"
            )


# ---------------------------------------------------------------------------
# 4. A Planner that *tries* to do IO cannot even name a capability
#    that is not in the closed set.
# ---------------------------------------------------------------------------

class TestPlannerCannotBypassThroughCapabilityName:
    """A Planner can only name a *capability* on a step; the
    capability name itself is a string and so cannot be a shell
    command.  Combined with the static shell-token check on
    ``PlanStep.__post_init__``, a Planner cannot smuggle a command
    through.
    """

    def test_planstep_rejects_capability_name_looking_like_shell(self):
        with pytest.raises(ValueError):
            PlanStep(
                step_id="s1",
                description="evil",
                capability_name="x; rm -rf /",
                parameters={},
            )

    def test_action_request_rejects_capability_name_looking_like_shell(self):
        with pytest.raises(ValueError):
            ActionRequest(
                capability_name="x; rm -rf /",
                parameters={},
            )

    def test_action_request_with_synthetic_params_via_planner(self):
        """Simulate a Planner producing an ActionRequest with
        seemingly valid params but a forbidden capability name.
        The construction must still fail.
        """
        with pytest.raises(ValueError):
            ActionRequest(
                capability_name="subprocess; rm -rf /",
                parameters={"args": ["ls"]},
            )

    def test_executor_cannot_construct_action_request_with_shell_token(self):
        """Even an Executor, when it converts a PlanStep into an
        ActionRequest, cannot smuggle shell tokens in.
        """
        # The PlanStep itself rejects the construction.
        with pytest.raises(ValueError):
            PlanStep(
                step_id="s1",
                description="x",
                capability_name="application.open",
                parameters={"app_name": "open || shutdown"},
            )
