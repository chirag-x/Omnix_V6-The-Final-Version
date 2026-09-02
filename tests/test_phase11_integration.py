"""
Omnix V6 — Phase 11 end-to-end integration tests.

These tests cover the canonical pipeline:

    user text → engine.process()
        → Brain (intent + plan with memory context)
        → Agent (closed loop with capability router)
        → response (safe text + structured metadata)

The pipeline is exercised through the public :class:`OmnixEngine`
boundary; every test uses a deterministic in-test LLM provider and a
small fake capability so the suite stays offline and reproducible.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

from ai.provider.base import LLMProvider
from ai.provider.contracts import (
    LLMMessage,
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
# Deterministic LLM provider for the tests.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _scrub_provider_env_after_test():
    """Several helpers in this file seed ``OMNIX_LLM_PROVIDER=mock`` /
    ``OMNIX_HEADLESS=1`` / ``OMNIX_QUIET_BOOT=1`` via ``os.environ.setdefault``
    so the engine under test uses the deterministic provider.  Those
    mutations must NOT leak into later tests in the same pytest process
    (notably Phase 6D end-to-end which resolves the provider from
    configuration alone).  We snapshot the relevant keys before the
    test runs and restore them on teardown, regardless of how they were
    set inside the test body.
    """
    import os
    snapshot = {k: os.environ.get(k) for k in
                ("OMNIX_HEADLESS", "OMNIX_LLM_PROVIDER", "OMNIX_QUIET_BOOT")}
    yield
    for k, prior in snapshot.items():
        if prior is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = prior


class _ScriptedProvider(LLMProvider):
    """A scripted LLM provider that returns pre-canned JSON responses.

    The tests below register a list of ``(input_substring, response)``
    pairs; the first match wins.  If no match, the provider returns a
    plain "ok" intent — enough to drive the Brain's planner.
    """

    def __init__(self) -> None:
        self.name = "scripted"
        self.calls: List[LLMRequest] = []
        self._rules: List[Tuple[str, str]] = []

    def add_rule(self, input_substring: str, json_response: str) -> None:
        self._rules.append((str(input_substring), str(json_response)))

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.calls.append(request)
        joined = " ".join(
            (m.content if isinstance(m.content, str) else "") for m in request.messages
        )
        for needle, body in self._rules:
            if needle in joined:
                return LLMResponse(
                    text=body,
                    model="scripted",
                    finish_reason="stop",
                    raw=None,
                )
        # Default: a CONTROL_APPLICATION intent with a known app
        default = (
            '{"kind":"CONTROL_APPLICATION","objective":"echo hello",'
            '"parameters":{"application_name":"echo","action":"launch"},'
            '"confidence":0.9,"source_text":"default"}'
        )
        return LLMResponse(
            text=default,
            model="scripted",
            finish_reason="stop",
            raw=None,
        )

    def health(self) -> Dict[str, Any]:
        return {"name": "scripted", "ok": True}


# ---------------------------------------------------------------------------
# Test helpers: build a tiny engine with one fake capability.
# ---------------------------------------------------------------------------


class _EchoCapability(Capability):
    """A minimal in-test capability that echoes its input back."""

    spec = CapabilitySpec(
        name="echo",
        version="1.0.0",
        description="echoes a string back",
        parameters=(
            CapabilityParameter(
                name="text", type=ParamType.STRING, required=False, default="",
            ),
        ),
    )

    def execute(self, params):
        text = (params or {}).get("text", "")
        return CapabilityResult(
            capability_name="echo",
            status=CapabilityStatus.VERIFIED,
            attempted=True,
            executed=True,
            verified=True,
            failed=False,
            action=ActionResult(
                status=ActionStatus.EXECUTED,
                action_name="echo",
                details={"echoed": text},
            ),
            verification=VerificationResult(
                status=VerificationStatus.VERIFIED,
                check_name="echo_ok",
                expected=text,
                actual=text,
            ),
            details={"echoed": text},
        )


def _build_test_engine(
    scripted: Optional[_ScriptedProvider] = None,
) -> OmnixEngine:
    """Construct a real :class:`OmnixEngine` with one fake capability.

    The engine boots the real Phase 11 pipeline.  A scripted LLM
    provider and a tiny in-process capability keep every test
    deterministic and offline.
    """
    # Configuration: headless, mock-llm
    import os
    os.environ.setdefault("OMNIX_HEADLESS", "1")
    os.environ.setdefault("OMNIX_LLM_PROVIDER", "mock")
    os.environ.setdefault("OMNIX_QUIET_BOOT", "1")

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

    # Pre-build a registry with a single fake capability so the
    # engine's standard bootstrap doesn't depend on system services.
    reg = CapabilityRegistry()
    reg.register(_EchoCapability())

    # Bypass standard capability seeding
    router = CapabilityRouter(reg, safety_policy=AllowAllSafetyPolicy())

    engine = OmnixEngine(
        cfg,
        registry=None,
        capabilities=reg,
        router=router,
    )
    engine.initialize()
    # Replace the LLM provider with the scripted one
    if scripted is not None:
        # Remove the default LLM provider that the engine registered
        try:
            engine.services._services.pop("llm_provider", None)
        except Exception:  # noqa: BLE001
            pass
        try:
            engine.services.register(scripted, name="llm_provider", priority=70)
        except Exception:  # noqa: BLE001
            pass
        # Force the pipeline to be rebuilt with the scripted provider
        try:
            engine.pipeline = engine._build_pipeline()
        except Exception:  # noqa: BLE001
            pass

    return engine


# ---------------------------------------------------------------------------
# 1. Engine boots with the pipeline and reports it as available.
# ---------------------------------------------------------------------------

def test_engine_initializes_with_pipeline():
    engine = _build_test_engine()
    assert engine.pipeline is not None
    stats = engine.statistics()
    assert stats["pipeline_available"] is True
    assert stats["request_count"] == 0


# ---------------------------------------------------------------------------
# 2. process() returns an OmnixResponse with a non-empty safe text.
# ---------------------------------------------------------------------------

def test_process_returns_omnix_response():
    engine = _build_test_engine()
    r = engine.process("hello")
    assert r.text
    assert isinstance(r.correlation_id, str) and len(r.correlation_id) == 16
    assert r.duration_ms >= 0
    # Status is one of the allowed ResponseStatus values
    assert r.status in (
        ResponseStatus.OK,
        ResponseStatus.CLARIFICATION,
        ResponseStatus.FAILED,
        ResponseStatus.TIMEOUT,
        ResponseStatus.CANCELLED,
        ResponseStatus.REJECTED,
    )


# ---------------------------------------------------------------------------
# 3. process() never leaks forbidden tokens in the response text.
# ---------------------------------------------------------------------------

def test_process_does_not_leak_secrets():
    engine = _build_test_engine()
    r = engine.process("here is my api_key=secret123")
    forbidden = ("api_key=", "sk-", "password=", "token=", "bearer ")
    low = r.text.lower()
    for tok in forbidden:
        assert tok not in low, f"forbidden token {tok!r} leaked into response: {r.text!r}"


# ---------------------------------------------------------------------------
# 4. process() with empty input returns a structured FAILED response.
# ---------------------------------------------------------------------------

def test_process_empty_input_returns_failed():
    engine = _build_test_engine()
    r = engine.process("")
    assert r.status is ResponseStatus.FAILED
    assert r.text
    assert r.error is not None


# ---------------------------------------------------------------------------
# 5. process() with whitespace-only input returns a structured FAILED response.
# ---------------------------------------------------------------------------

def test_process_whitespace_input_returns_failed():
    engine = _build_test_engine()
    r = engine.process("    \n  \t  ")
    assert r.status is ResponseStatus.FAILED
    assert r.text


# ---------------------------------------------------------------------------
# 6. Explicit correlation_id is preserved end-to-end.
# ---------------------------------------------------------------------------

def test_process_preserves_correlation_id():
    engine = _build_test_engine()
    r = engine.process("hello", correlation_id="abcd1234abcd1234")
    assert r.correlation_id == "abcd1234abcd1234"


# ---------------------------------------------------------------------------
# 7. process() emits REQUEST_RECEIVED and REQUEST_COMPLETED on the bus.
# ---------------------------------------------------------------------------

def test_process_emits_request_events():
    scripted = _ScriptedProvider()
    engine = _build_test_engine(scripted=scripted)

    received_events: list = []
    completed_events: list = []

    def on_event(evt):
        if getattr(evt, "name", "") == "request.event":
            if getattr(evt, "stage", "") == "received":
                received_events.append(evt)
            elif getattr(evt, "stage", "") == "completed":
                completed_events.append(evt)

    engine.bus.subscribe("request.*", on_event)
    r = engine.process("hello")

    assert len(received_events) >= 1, "REQUEST_RECEIVED was not emitted"
    assert received_events[0].correlation_id == r.correlation_id
    # There should be at least one terminal event (REQUEST_COMPLETED or
    # a timeout/cancelled/rejected).  The Brain + Agent emit a flurry
    # of stage events; we only care that the terminal is present.
    stages = {e.stage for e in completed_events}
    assert any(
        s in stages
        for s in ("completed", "timed_out", "cancelled", "rejected")
    ), f"no terminal stage emitted: {stages}"


# ---------------------------------------------------------------------------
# 8. statistics() increments request_count after process() is called.
# ---------------------------------------------------------------------------

def test_request_count_increments():
    engine = _build_test_engine()
    assert engine.statistics()["request_count"] == 0
    engine.process("first")
    engine.process("second")
    assert engine.statistics()["request_count"] == 2


# ---------------------------------------------------------------------------
# 9. process() with a known intent drives the Agent to a structured result.
# ---------------------------------------------------------------------------

def test_process_with_known_intent():
    scripted = _ScriptedProvider()
    # Make the scripted provider return a CONTROL_APPLICATION intent
    scripted.add_rule(
        "echo",
        '{"kind":"CONTROL_APPLICATION","objective":"launch echo",'
        '"parameters":{"application_name":"echo","action":"launch"},'
        '"confidence":0.95,"source_text":"open echo"}',
    )
    engine = _build_test_engine(scripted=scripted)
    r = engine.process("open echo")
    # The response must be safe and structured — no exception leaks
    assert r.text
    assert r.status in (
        ResponseStatus.OK,
        ResponseStatus.FAILED,
        ResponseStatus.CLARIFICATION,
    )
    # Agent may not find a matching capability for "echo" — that's
    # fine; the response is still a structured FAILED, not a crash.


# ---------------------------------------------------------------------------
# 10. process() never raises even when the LLM provider misbehaves.
# ---------------------------------------------------------------------------

class _RaisingProvider(LLMProvider):
    name = "raising"

    def generate(self, request: LLMRequest) -> LLMResponse:
        raise RuntimeError("simulated LLM outage")

    def health(self) -> Dict[str, Any]:
        return {"name": "raising", "ok": False}

    # Service-registry protocol surface
    def initialize(self) -> bool:  # noqa: D401
        return True

    def shutdown(self) -> None:
        return None

    def statistics(self) -> Dict[str, Any]:
        return {"name": "raising"}


def test_process_never_raises():
    import os
    os.environ.setdefault("OMNIX_HEADLESS", "1")
    os.environ.setdefault("OMNIX_LLM_PROVIDER", "mock")
    os.environ.setdefault("OMNIX_QUIET_BOOT", "1")

    cfg = OmnixConfig(
        project_root=Path("."),
        data_dir=Path("./logs"),
        log_dir=Path("./logs"),
        env_file=Path("./.env"),
        log_to_file=False,
        log_level="ERROR",
    )
    reg = CapabilityRegistry()
    router = CapabilityRouter(reg, safety_policy=AllowAllSafetyPolicy())
    engine = OmnixEngine(
        cfg, capabilities=reg, router=router,
    )
    engine.initialize()
    # Force the engine to use the raising provider
    try:
        engine.services._services.pop("llm_provider", None)
    except Exception:  # noqa: BLE001
        pass
    engine.services.register(_RaisingProvider(), name="llm_provider", priority=70)
    try:
        engine.pipeline = engine._build_pipeline()
    except Exception:  # noqa: BLE001
        pass
    # Must not raise
    r = engine.process("hello")
    assert r.status is ResponseStatus.FAILED
    assert r.text


# ---------------------------------------------------------------------------
# 11. OmnixResponse.to_dict() is JSON-serializable and round-trips.
# ---------------------------------------------------------------------------

def test_omnix_response_to_dict_round_trip():
    import json
    engine = _build_test_engine()
    r = engine.process("hello")
    d = r.to_dict()
    s = json.dumps(d)
    out = json.loads(s)
    assert out["correlation_id"] == r.correlation_id
    assert out["status"] == r.status.value
    assert "text" in out
    assert "metadata" in out


# ---------------------------------------------------------------------------
# 12. RequestEvent has the canonical stage constants.
# ---------------------------------------------------------------------------

def test_request_event_stage_constants():
    from core.events.event_types import (
        REQUEST_RECEIVED,
        REQUEST_INTENT_RESOLVED,
        REQUEST_PLAN_CREATED,
        REQUEST_EXECUTION_STARTED,
        REQUEST_OBSERVATION_CAPTURED,
        REQUEST_VERIFICATION_COMPLETED,
        REQUEST_RECOVERY_STARTED,
        REQUEST_REPLAN_STARTED,
        REQUEST_COMPLETED,
        REQUEST_CANCELLED,
        REQUEST_TIMED_OUT,
        REQUEST_REJECTED,
    )
    assert REQUEST_RECEIVED == "received"
    assert REQUEST_INTENT_RESOLVED == "intent_resolved"
    assert REQUEST_PLAN_CREATED == "plan_created"
    assert REQUEST_EXECUTION_STARTED == "execution_started"
    assert REQUEST_COMPLETED == "completed"


# ---------------------------------------------------------------------------
# 13. The pipeline does not bypass the bus when no bus is provided.
# ---------------------------------------------------------------------------

def test_pipeline_works_without_event_bus():
    """If the engine constructs a pipeline with no bus, it must still
    complete — observability is best-effort, not a hard dependency."""
    from ai.brain.brain import Brain
    from core.pipeline import RequestPipeline
    from core.orchestration import (
        Agent, AgentPolicy, PlanExecutorImpl,
        DefaultStepVerifier, DefaultGoalVerifier, DefaultRecoveryEngine,
    )
    from core.orchestration.observation import CapabilityResultObservationProvider
    from ai.brain.deterministic import DeterministicPlanner
    from ai.intent.specs import build_default_registry

    reg = CapabilityRegistry()
    planner = DeterministicPlanner(registry=reg)
    scripted = _ScriptedProvider()
    from ai.intent.interpreter import LLMIntentInterpreter
    interp = LLMIntentInterpreter(provider=scripted, registry=build_default_registry())
    brain = Brain(registry=reg, interpreter=interp, planner=planner)
    pe = PlanExecutorImpl(router=CapabilityRouter(reg))
    agent = Agent(
        interpreter=interp,
        planner=planner,
        plan_executor=pe,
        recovery_engine=DefaultRecoveryEngine(),
        step_verifier=DefaultStepVerifier(),
        goal_verifier=DefaultGoalVerifier(),
        observation_provider=CapabilityResultObservationProvider(),
        policy=AgentPolicy(),
    )
    pipeline = RequestPipeline(brain=brain, agent=agent, event_bus=None)
    r = pipeline.process("hello")
    assert r.text
    assert r.status in (
        ResponseStatus.OK, ResponseStatus.FAILED, ResponseStatus.CLARIFICATION,
    )


# ---------------------------------------------------------------------------
# 14. process() refuses to run when the engine is not READY/RUNNING.
# ---------------------------------------------------------------------------

def test_process_refused_before_initialize():
    import os
    os.environ.setdefault("OMNIX_HEADLESS", "1")
    os.environ.setdefault("OMNIX_LLM_PROVIDER", "mock")

    cfg = OmnixConfig(
        project_root=Path("."),
        data_dir=Path("./logs"),
        log_dir=Path("./logs"),
        env_file=Path("./.env"),
        log_to_file=False,
        log_level="ERROR",
    )
    # Do NOT call initialize()
    engine = OmnixEngine(cfg)
    r = engine.process("hello")
    assert r.status is ResponseStatus.FAILED
    assert r.error is not None


# ---------------------------------------------------------------------------
# 15. process() does not mutate the engine's statistics counters when
#     the request is rejected before the pipeline runs.
# ---------------------------------------------------------------------------

def test_request_count_does_not_increment_for_empty_input():
    engine = _build_test_engine()
    before = engine.statistics()["request_count"]
    engine.process("")
    after = engine.statistics()["request_count"]
    # Empty input is rejected at the boundary, BEFORE the request
    # counter is incremented.  Both before and after must equal 0.
    assert before == 0
    assert after == 0


# ---------------------------------------------------------------------------
# 16. safe_default_text produces non-empty strings for every status.
# ---------------------------------------------------------------------------

def test_safe_default_text_covers_all_statuses():
    from core.responses import safe_default_text
    for st in ResponseStatus:
        txt = safe_default_text(st)
        assert isinstance(txt, str) and txt


# ---------------------------------------------------------------------------
# 17. new_correlation_id returns 16-char hex strings.
# ---------------------------------------------------------------------------

def test_new_correlation_id_format():
    from core.responses import new_correlation_id
    cid = new_correlation_id()
    assert isinstance(cid, str)
    assert len(cid) == 16
    int(cid, 16)  # parses as hex


# ---------------------------------------------------------------------------
# 18. health report names all Phase 11 subsystems.
# ---------------------------------------------------------------------------

def test_health_report_includes_pipeline_subsystems():
    engine = _build_test_engine()
    h = engine.health.report()
    subsystems = h.get("subsystems", {})
    for name in ("pipeline", "brain", "agent", "llm_provider"):
        assert name in subsystems, f"health report missing subsystem: {name}"


# ---------------------------------------------------------------------------
# 19. process() handles a long input without crashing or hanging.
# ---------------------------------------------------------------------------

def test_process_long_input():
    engine = _build_test_engine()
    long_text = "tell me a story " * 200
    t0 = time.time()
    r = engine.process(long_text)
    elapsed = time.time() - t0
    # Even with a long input the response must be safe and bounded.
    assert len(r.text) <= 2200  # the pipeline trims to 2000 + "..."
    assert r.status in (
        ResponseStatus.OK, ResponseStatus.FAILED, ResponseStatus.CLARIFICATION,
    )
    # Sanity: even with a long input, the pipeline should finish in a
    # bounded amount of time (10s is a comfortable upper bound for
    # tests that don't actually execute capabilities).
    assert elapsed < 10.0, f"process() took {elapsed:.1f}s for long input"


# ---------------------------------------------------------------------------
# 20. process() returns the same correlation_id passed in.
# ---------------------------------------------------------------------------

def test_process_correlation_id_preserved_in_metadata():
    engine = _build_test_engine()
    r = engine.process("hi", correlation_id="zzzzzzzzzzzzzzzz")
    assert r.correlation_id == "zzzzzzzzzzzzzzzz"
    assert r.metadata.get("correlation_id") == "zzzzzzzzzzzzzzzz"


# ---------------------------------------------------------------------------
# 21. process() with explicit correlation_id still emits matching events.
# ---------------------------------------------------------------------------

def test_process_event_correlation_id_matches_response():
    engine = _build_test_engine()
    captured = []
    engine.bus.subscribe(
        "request.*",
        lambda e: captured.append(e)
        if getattr(e, "name", "") == "request.event" else None,
    )
    r = engine.process("hi", correlation_id="qqqqqqqqqqqqqqqq")
    for evt in captured:
        assert evt.correlation_id == r.correlation_id == "qqqqqqqqqqqqqqqq"


# ---------------------------------------------------------------------------
# 22. RequestPipeline is importable and is a thin class (no business logic).
# ---------------------------------------------------------------------------

def test_request_pipeline_is_thin():
    import inspect
    from core.pipeline import RequestPipeline
    src = inspect.getsource(RequestPipeline)
    # Sanity: the class should not contain UI / OS code.
    forbidden = (
        "subprocess", "pyautogui", "win32gui", "ctypes", "keyboard.",
        "pydirectinput", "mss.",
    )
    for tok in forbidden:
        assert tok not in src, f"forbidden token {tok!r} in RequestPipeline source"


# ---------------------------------------------------------------------------
# 23. OmnixEngine.process signature is keyword-only for correlation_id.
# ---------------------------------------------------------------------------

def test_process_signature_keyword_only_correlation_id():
    import inspect
    sig = inspect.signature(OmnixEngine.process)
    cid_param = sig.parameters["correlation_id"]
    assert cid_param.kind is inspect.Parameter.KEYWORD_ONLY


# ---------------------------------------------------------------------------
# 24. process() never exposes a raw exception in the response.
# ---------------------------------------------------------------------------

def test_process_does_not_leak_exception_in_text():
    engine = _build_test_engine()
    r = engine.process("x" * 5000)
    # The text must not contain Python exception tokens
    low = r.text.lower()
    for tok in ("traceback", "exception:", "raise ", "stack trace"):
        assert tok not in low, f"exception token {tok!r} leaked into text: {r.text!r}"


# ---------------------------------------------------------------------------
# 25. Multiple sequential process() calls do not corrupt shared state.
# ---------------------------------------------------------------------------

def test_process_multiple_calls_no_state_corruption():
    engine = _build_test_engine()
    responses = [engine.process(f"call {i}") for i in range(5)]
    assert len({r.correlation_id for r in responses}) == 5
    assert engine.statistics()["request_count"] == 5
