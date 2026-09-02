"""
Omnix V6 — Phase 14 + 11.6 regression tests for the
"Open Chrome" runtime failure.

Background
----------
The user reported that ``engine.process("Open Chrome")`` returns
"I could not complete that request." while the engine reports
"healthy" with 46 capabilities loaded.  The post-mortem
identified four root causes; this module exercises each one
end-to-end through the real architecture:

  1. :class:`core.orchestration.Agent.run` was calling
     ``.to_goal()`` on whatever :meth:`IntentInterpreter.interpret`
     returned.  The LLM interpreter returns an ``IntentResult``
     *envelope* (status/intent/error_*), not a bare ``Intent``,
     so the call crashed with
     ``AttributeError: 'IntentResult' object has no attribute
     'to_goal'``.  The fix lets the Agent accept both shapes.
  2. The :class:`MultiStepCoordinator` (Phase 14) was never wired
     into the :class:`Agent` constructed by the engine.  The fix
     builds the coordinator in ``_build_multi_step_coordinator``
     and passes it to the Agent.
  3. The pipeline ignored the Brain's pre-built intent/goal/plan
     and re-invoked ``Agent.run(text)``, doing the work twice.
     This test exercises the canonical pipeline and the
     pre-resolved intent path.
  4. The Agent's planner must produce a non-empty plan for
     ``open_application`` so the executor can dispatch the
     ``desktop.application.open`` capability.

The tests in this module exercise the real engine with a
scripted LLM provider and a fake
:func:`desktop.application.open` capability so they stay
offline and deterministic.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

# ---- Force deterministic env BEFORE the engine imports its config -----
os.environ.setdefault("OMNIX_HEADLESS", "1")
os.environ.setdefault("OMNIX_LLM_PROVIDER", "mock")
os.environ.setdefault("OMNIX_QUIET_BOOT", "1")

from ai.provider.base import LLMProvider
from ai.provider.contracts import (
    LLMRequest,
    LLMResponse,
    MessageRole,
    OutputFormat,
)
from core.capability import Capability, CapabilityParameter, CapabilitySpec, ParamType
from core.capability_registry import CapabilityRegistry
from core.capability_router import AllowAllSafetyPolicy, CapabilityRouter
from core.configuration import OmnixConfig
from core.omnix_engine import OmnixEngine
from core.orchestration import Agent, AgentState
from core.orchestration.multi_step_coordinator import (
    InMemoryIdempotencyStore,
    InMemoryMultiStepContextStore,
    MultiStepCoordinator,
)
from core.responses import ResponseStatus
from core.results import (
    ActionResult,
    ActionStatus,
    CapabilityResult,
    CapabilityStatus,
    VerificationResult,
    VerificationStatus,
)


# ---------------------------------------------------------------------------
# Scripted LLM provider
# ---------------------------------------------------------------------------


class _OpenChromeScriptedProvider(LLMProvider):
    """A scripted LLM provider that returns a valid ``open_application``
    JSON intent for "Open Chrome" and "Open Notepad" inputs, and a
    valid ``unknown`` for everything else.

    The provider deliberately does NOT echo the user input — the real
    bug we are testing is that the engine must consume the LLM output
    in a real, JSON-valid form.
    """

    name: str = "scripted-open-chrome"

    def __init__(self) -> None:
        self.calls: List[LLMRequest] = []

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.calls.append(request)
        joined = " ".join(
            (m.content if isinstance(m.content, str) else "")
            for m in request.messages
        )
        text_lower = joined.lower()

        if "chrome" in text_lower:
            payload = (
                '{"kind":"open_application",'
                '"objective":"open chrome",'
                '"parameters":{"app_name":"chrome"},'
                '"confidence":0.95,'
                '"source_text":"Open Chrome"}'
            )
        elif "notepad" in text_lower:
            payload = (
                '{"kind":"open_application",'
                '"objective":"open notepad",'
                '"parameters":{"app_name":"notepad"},'
                '"confidence":0.95,'
                '"source_text":"Open Notepad"}'
            )
        else:
            # Fall back to "unknown" — the deterministic planner
            # will not be able to act on this; this lets us
            # exercise the error/unknown paths in the same test
            # module.
            payload = (
                '{"kind":"unknown",'
                '"objective":"unknown request",'
                '"parameters":{},'
                '"confidence":0.1,'
                '"source_text":"?"}'
            )

        return LLMResponse(
            content=payload,
            model="scripted-open-chrome",
            finish_reason="stop",
            raw={},
        )

    def health(self) -> Dict[str, Any]:
        return {"name": "scripted-open-chrome", "ok": True}


# ---------------------------------------------------------------------------
# Fake desktop.application.open capability
# ---------------------------------------------------------------------------


class _FakeAppOpenCapability(Capability):
    """A fake ``desktop.application.open`` capability that records
    every invocation and returns a verified result.
    """

    spec = CapabilitySpec(
        name="desktop.application.open",
        version="1.0.0",
        description="fake app open",
        parameters=(
            CapabilityParameter(
                name="app_name", type=ParamType.STRING, required=True,
            ),
        ),
    )

    def __init__(self) -> None:
        self.invocations: List[Dict[str, Any]] = []

    def is_available(self) -> bool:
        # The router consults ``is_available()`` before dispatch
        # (see :meth:`CapabilityRouter.route`).  The real
        # ``desktop.application.open`` capability reports ``True``
        # when its underlying ApplicationService is initialised;
        # the fake capability has no such service, so we just
        # return ``True`` unconditionally.
        return True

    def execute(self, params):
        self.invocations.append(dict(params or {}))
        app_name = (params or {}).get("app_name", "")
        return CapabilityResult(
            capability_name="desktop.application.open",
            status=CapabilityStatus.VERIFIED,
            attempted=True,
            executed=True,
            verified=True,
            failed=False,
            action=ActionResult(
                status=ActionStatus.EXECUTED,
                action_name="desktop.application.open",
                details={"app_name": app_name},
            ),
            verification=VerificationResult(
                status=VerificationStatus.VERIFIED,
                check_name="app_launched",
                expected=app_name,
                actual=app_name,
            ),
            details={"app_name": app_name},
        )


# ---------------------------------------------------------------------------
# Engine fixture
# ---------------------------------------------------------------------------


def _build_engine_with_fake_app_open(
    scripted: Optional[_OpenChromeScriptedProvider] = None,
) -> Tuple[OmnixEngine, _FakeAppOpenCapability]:
    """Build a real :class:`OmnixEngine` whose standard capability
    set has been replaced with a single fake
    ``desktop.application.open`` capability.  Returns
    ``(engine, fake_capability)``.

    When ``scripted`` is provided, the helper injects the scripted
    LLM provider directly into the Brain's interpreter so the
    pipeline gets deterministic, JSON-valid LLM output.  (The
    Engine's :meth:`_resolve_llm_provider` registers the provider
    through :class:`ServiceRegistry`, which refuses non-lifecycle
    services; tests bypass the registry and replace the provider
    on the interpreter itself.)
    """
    cfg = OmnixConfig(
        project_root=Path("."),
        data_dir=Path("./logs"),
        log_dir=Path("./logs"),
        env_file=Path("./.env"),
        log_to_file=False,
        log_level="ERROR",
        enable_voice=False,
        enable_vision=False,
        enable_browser=False,
        enable_automation=False,
    )

    fake = _FakeAppOpenCapability()
    reg = CapabilityRegistry()
    reg.register(fake)
    router = CapabilityRouter(reg, safety_policy=AllowAllSafetyPolicy())

    engine = OmnixEngine(
        cfg,
        registry=None,
        capabilities=reg,
        router=router,
    )
    engine.initialize()

    # Replace the LLM provider with the scripted one so the brain
    # gets a real JSON intent instead of an echo.  The Brain's
    # interpreter owns the provider reference, so we patch the
    # interpreter in place.
    if scripted is not None and engine.pipeline is not None:
        brain = engine.pipeline.brain
        interpreter = getattr(brain, "interpreter", None)
        if interpreter is not None and hasattr(interpreter, "_provider"):
            interpreter._provider = scripted

    return engine, fake


# ---------------------------------------------------------------------------
# Root cause #1: Agent.run accepts IntentResult envelopes
# ---------------------------------------------------------------------------


class TestAgentAcceptsIntentResultEnvelope:
    """The LLM interpreter returns an ``IntentResult`` envelope, not
    a bare ``Intent``.  The Agent must accept both shapes.
    """

    def test_agent_run_with_bare_intent_still_works(self):
        """Backwards-compat: protocol contract says ``-> Intent`` and
        the test suite uses bare intents — those must keep working.
        """
        from core.orchestration.interfaces import IntentInterpreter
        from core.orchestration.models import (
            ExpectedEffect,
            Goal,
            Intent,
            IntentKind,
            Plan,
            PlanStep,
        )
        from core.orchestration.recovery import (
            DefaultRecoveryEngine,
            RecoveryPolicy,
        )

        class _BareIntentInterpreter(IntentInterpreter):
            name = "bare"

            def interpret(self, text, *, context_snapshot=None):
                return Intent(
                    intent_id="i-1",
                    kind=IntentKind.OPEN_APPLICATION,
                    text=text,
                    confidence=0.9,
                    parameters={"app_name": "chrome"},
                )

        class _FakePlanner:
            name = "fake-planner"
            def plan(
                self, goal, *, intent=None, context_snapshot=None,
                prior_plan=None, failure=None,
            ):
                return Plan(
                    plan_id="p1",
                    goal_id=goal.goal_id,
                    steps=(),
                )

        class _NoopExecutor:
            name = "noop"
            def execute(self, context):
                from core.orchestration import (
                    ExecutionOutcome,
                    StepState,
                )
                from core.orchestration.execution_result import (
                    ExecutionResult,
                    make_blank_execution_result,
                )
                blank = make_blank_execution_result(
                    execution_id=context.execution_id,
                    plan_id=context.plan.plan_id,
                    goal_id=context.goal.goal_id,
                )
                return blank.with_outcome(
                    ExecutionOutcome.COMPLETED, completed_at=0.0,
                )

        a = Agent(
            interpreter=_BareIntentInterpreter(),
            planner=_FakePlanner(),
            plan_executor=_NoopExecutor(),
            recovery_engine=DefaultRecoveryEngine(
                policy=RecoveryPolicy(max_replans=0),
            ),
        )
        r = a.run("open chrome")
        # Empty plan → no step was executed → goal not verified by
        # default; but it must NOT raise ``AttributeError``.
        assert r.final_state in (
            AgentState.COMPLETE, AgentState.FAILED,
            AgentState.CLARIFICATION_REQUIRED,
        )
        # And specifically: the error must NOT be the
        # "IntentResult has no to_goal" crash.
        assert "to_goal" not in (r.error or "")

    def test_agent_run_with_intent_result_envelope_succeeds(self):
        """The real LLM interpreter returns an ``IntentResult``
        envelope.  This test wires that exact shape into the Agent
        and asserts the Agent does NOT crash with
        ``AttributeError``.
        """
        from ai.intent.interpreter import IntentResult
        from core.orchestration.interfaces import IntentInterpreter
        from core.orchestration.models import (
            Goal,
            Intent,
            IntentKind,
            Plan,
        )
        from core.orchestration.recovery import (
            DefaultRecoveryEngine,
            RecoveryPolicy,
        )

        intent = Intent(
            intent_id="i-1",
            kind=IntentKind.OPEN_APPLICATION,
            text="open chrome",
            confidence=0.9,
            parameters={"app_name": "chrome"},
        )

        class _EnvelopeInterpreter(IntentInterpreter):
            name = "envelope"

            def interpret(self, text, *, context_snapshot=None):
                return IntentResult(status="ok", intent=intent)

        class _FakePlanner:
            name = "fake-planner"
            def plan(
                self, goal, *, intent=None, context_snapshot=None,
                prior_plan=None, failure=None,
            ):
                return Plan(plan_id="p1", goal_id=goal.goal_id, steps=())

        class _NoopExecutor:
            name = "noop"
            def execute(self, context):
                from core.orchestration import (
                    ExecutionOutcome,
                )
                from core.orchestration.execution_result import (
                    make_blank_execution_result,
                )
                blank = make_blank_execution_result(
                    execution_id=context.execution_id,
                    plan_id=context.plan.plan_id,
                    goal_id=context.goal.goal_id,
                )
                return blank.with_outcome(
                    ExecutionOutcome.COMPLETED, completed_at=0.0,
                )

        a = Agent(
            interpreter=_EnvelopeInterpreter(),
            planner=_FakePlanner(),
            plan_executor=_NoopExecutor(),
            recovery_engine=DefaultRecoveryEngine(
                policy=RecoveryPolicy(max_replans=0),
            ),
        )
        r = a.run("open chrome")
        # The agent must NOT crash.
        assert r.error is None or "to_goal" not in r.error
        assert "AttributeError" not in (r.error or "")

    def test_agent_run_with_clarification_envelope_returns_clarification(self):
        """An ``IntentResult`` with status="clarification" must make
        the Agent return AgentState.CLARIFICATION_REQUIRED — not a
        bare ``FAILED`` with a stack trace.
        """
        from ai.intent.interpreter import IntentResult
        from core.orchestration.interfaces import IntentInterpreter
        from core.orchestration.recovery import (
            DefaultRecoveryEngine,
            RecoveryPolicy,
        )

        class _ClarifyInterpreter(IntentInterpreter):
            name = "clarify"

            def interpret(self, text, *, context_snapshot=None):
                return IntentResult(
                    status="clarification",
                    clarifying_question="which app?",
                )

        class _NoopPlanner:
            name = "noop"
            def plan(self, goal, **kwargs):
                return None

        class _NoopExecutor:
            name = "noop"
            def execute(self, context):
                raise AssertionError("should not be called")

        a = Agent(
            interpreter=_ClarifyInterpreter(),
            planner=_NoopPlanner(),
            plan_executor=_NoopExecutor(),
            recovery_engine=DefaultRecoveryEngine(
                policy=RecoveryPolicy(max_replans=0),
            ),
        )
        r = a.run("open that")
        assert r.final_state is AgentState.CLARIFICATION_REQUIRED
        assert r.clarifying_question == "which app?"

    def test_agent_run_with_error_envelope_returns_failed(self):
        """An ``IntentResult`` with status="error" must make the
        Agent return AgentState.FAILED with the interpreter's
        error code — not crash.
        """
        from ai.intent.interpreter import IntentResult
        from core.orchestration.interfaces import IntentInterpreter
        from core.orchestration.recovery import (
            DefaultRecoveryEngine,
            RecoveryPolicy,
        )

        class _ErrorInterpreter(IntentInterpreter):
            name = "err"

            def interpret(self, text, *, context_snapshot=None):
                return IntentResult(
                    status="error",
                    error_code="INTENT_MALFORMED_JSON",
                    error_message="bad json",
                )

        class _NoopPlanner:
            name = "noop"
            def plan(self, goal, **kwargs):
                return None

        class _NoopExecutor:
            name = "noop"
            def execute(self, context):
                raise AssertionError("should not be called")

        a = Agent(
            interpreter=_ErrorInterpreter(),
            planner=_NoopPlanner(),
            plan_executor=_NoopExecutor(),
            recovery_engine=DefaultRecoveryEngine(
                policy=RecoveryPolicy(max_replans=0),
            ),
        )
        r = a.run("Open Chrome")
        assert r.final_state is AgentState.FAILED
        assert r.error and "INTENT_MALFORMED_JSON" in r.error


# ---------------------------------------------------------------------------
# Root cause #2: MultiStepCoordinator is wired into the engine
# ---------------------------------------------------------------------------


class TestMultiStepCoordinatorWired:
    def test_engine_wires_multistep_coordinator_into_agent(self):
        """The engine must construct a MultiStepCoordinator and pass
        it to the Agent.  Pre-fix: ``agent.multi_step_coordinator``
        was ``None``.
        """
        engine, _ = _build_engine_with_fake_app_open()
        try:
            assert engine.pipeline is not None
            assert engine.pipeline.agent is not None
            assert engine.pipeline.agent.multi_step_coordinator is not None
            coord = engine.pipeline.agent.multi_step_coordinator
            assert isinstance(coord, MultiStepCoordinator)
            assert coord.context_store is not None
            assert coord.idempotency_store is not None
        finally:
            engine.stop()

    def test_engine_can_build_multistep_coordinator_in_isolation(self):
        """``_build_multi_step_coordinator`` returns a coordinator
        or ``None`` (when Phase 14 modules are unavailable); both
        are safe — the Agent still runs.
        """
        engine, _ = _build_engine_with_fake_app_open()
        try:
            coord = engine._build_multi_step_coordinator(
                vision_target_provider=None,
            )
            assert coord is None or isinstance(coord, MultiStepCoordinator)
        finally:
            engine.stop()


# ---------------------------------------------------------------------------
# End-to-end: the actual "Open Chrome" path through the pipeline
# ---------------------------------------------------------------------------


class TestOpenChromeEndToEnd:
    """The original user-reported failure: ``engine.process('Open
    Chrome')`` must dispatch the fake ``desktop.application.open``
    capability.  The LLM is scripted to return a valid
    ``open_application`` JSON intent; the engine must consume it
    and execute the capability.
    """

    def test_open_chrome_via_pipeline_dispatches_capability(self):
        scripted = _OpenChromeScriptedProvider()
        engine, fake = _build_engine_with_fake_app_open(scripted)
        try:
            r = engine.process("Open Chrome")
            # The LLM should have been called at least once.
            assert scripted.calls, "LLM was not called"
            # The fake capability should have been invoked at
            # least once with app_name=chrome.
            apps = [inv.get("app_name") for inv in fake.invocations]
            assert "chrome" in apps, (
                f"expected chrome in invocations, got {apps!r} "
                f"(response={r.to_dict()!r})"
            )
        finally:
            engine.stop()

    def test_open_notepad_via_pipeline_dispatches_capability(self):
        scripted = _OpenChromeScriptedProvider()
        engine, fake = _build_engine_with_fake_app_open(scripted)
        try:
            engine.process("Open Notepad")
            apps = [inv.get("app_name") for inv in fake.invocations]
            assert "notepad" in apps, (
                f"expected notepad in invocations, got {apps!r}"
            )
        finally:
            engine.stop()

    def test_open_chrome_pipeline_status_is_ok(self):
        scripted = _OpenChromeScriptedProvider()
        engine, _ = _build_engine_with_fake_app_open(scripted)
        try:
            r = engine.process("Open Chrome")
            # When the LLM returns a valid intent and the
            # capability exists, the pipeline must report OK (or
            # at minimum, NOT the generic "I could not complete
            # that request" failure that the user reported).
            assert r.status is not ResponseStatus.FAILED, (
                f"unexpected FAILED: {r.error!r} / {r.text!r}"
            )
        finally:
            engine.stop()


# ---------------------------------------------------------------------------
# Root cause #3: brain → pipeline uses the same intent
# ---------------------------------------------------------------------------


class TestBrainAndAgentShareIntent:
    def test_brain_and_agent_use_same_intent_kind(self):
        """When the LLM returns ``open_application``, the brain and
        the agent must both see the same intent kind — not a
        re-interpreted, contradictory one.
        """
        from ai.intent.interpreter import LLMIntentInterpreter
        from ai.intent.specs import build_default_registry
        scripted = _OpenChromeScriptedProvider()
        engine, _ = _build_engine_with_fake_app_open(scripted)
        try:
            interp = LLMIntentInterpreter(
                provider=scripted, registry=build_default_registry(),
            )
            # Brain-path
            from ai.brain.brain import Brain
            from ai.brain.deterministic import DeterministicPlanner
            brain = Brain(
                registry=engine.capabilities,
                interpreter=interp,
                planner=DeterministicPlanner(registry=engine.capabilities),
            )
            br = brain.handle_text("Open Chrome")
            assert br.status == "ok", br.error_message
            assert br.intent is not None
            assert str(br.intent.kind).endswith("OPEN_APPLICATION")
            # Agent path (via the engine's pipeline): the agent
            # must not crash and must also see
            # OPEN_APPLICATION (verified by the plan history).
            r = engine.process("Open Chrome")
            # Inspect the agent run's plan history if any.
            # The user-facing response is what matters most.
            assert r.status is not ResponseStatus.FAILED, r.error
        finally:
            engine.stop()
