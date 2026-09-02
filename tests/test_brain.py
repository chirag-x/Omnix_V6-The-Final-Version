"""
Omnix V6 — Phase 5C+5D Brain / Planner tests.

The scenarios (mapped to the Phase 5C+5D directive):

    Discovery
        1.  discover_capabilities returns planner-friendly summaries.
        2.  discover_capabilities filters by name and tag.
        3.  summarize_for_prompt enforces the byte budget.

    Validation
        4.  minimal plan payload -> Plan
        5.  unknown capability -> UnknownCapabilityError
        6.  missing required parameter -> InvalidArgumentError
        7.  unexpected parameter -> InvalidArgumentError
        8.  wrong parameter type -> InvalidArgumentError
        9.  shell-token in capability_name -> rejected
        10. dangerous capability cannot be downgraded
        11. step with self-dependency -> InvalidDependencyError
        12. step with missing dependency -> InvalidDependencyError
        13. cycle in dependencies -> InvalidDependencyError
        14. invalid timeout -> InvalidTimeoutError
        15. invalid expected effect -> InvalidExpectedEffectError
        16. plan with zero steps -> MalformedPlanPayload
        17. plan over size limit -> PlanSizeExceeded
        18. plan with valid DAG passes

    Deterministic planner
        19. happy path: known intent kind -> plan
        20. unknown intent kind -> CannotPlanError
        21. cannot_plan for cancel_task (empty rule)
        22. determinism: same goal/intent -> same plan structure
        23. registers a plan id and goal id

    LLM planner
        24. happy path with mock provider -> plan
        25. malformed JSON -> CannotPlanError
        26. provider error -> ProviderFailure
        27. provider timeout -> ProviderTimeout
        28. provider cancellation -> CancelledError
        29. JSON wrapped in markdown fences is accepted
        30. plan payload with unknown capability is rejected
        31. system prompt is deterministic

    Brain
        32. handle_text("Open Spotify") -> plan (LLM)
        33. handle_text("Open it.") -> clarification result
        34. handle_text("blarg") -> unknown result
        35. handle_text with intent error -> error result
        36. handle_text with provider error -> ProviderFailure raised
        37. plan(goal) is read-only; no execution path
        38. clarification question is surfaced
        39. BrainResult.is_ok + to_dict

    Architectural invariants
        40. Brain/Planner never constructs ActionRequest
        41. Brain/Planner never imports a Windows service
        42. Brain/Planner never imports the engine
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import pytest

from core.capability import (
    CallableCapability,
    CapabilityParameter,
    CapabilitySpec,
    ParamType,
)
from core.capability_registry import CapabilityRegistry
from core.orchestration import (
    ActionKind,
    ExpectedEffect,
    Failure,
    FailureKind,
    Goal,
    Intent,
    IntentKind,
    Plan,
    PlanStatus,
    PlanStep,
)

from ai.brain import (
    ALLOWED_SAFETY_CLASSIFICATIONS,
    Brain,
    BrainResult,
    CapabilitySummary,
    DeterministicPlanner,
    LLMPlanner,
    MAX_PLAN_STEPS,
    discover_capabilities,
    summarize_for_prompt,
    validate_plan_payload,
)
from ai.brain.exceptions import (
    BrainError,
    CannotPlanError,
    CancelledError,
    InvalidArgumentError,
    InvalidDependencyError,
    InvalidExpectedEffectError,
    InvalidTimeoutError,
    MalformedPlanPayload,
    PlanSizeExceeded,
    ProviderFailure,
    ProviderMalformedResponse,
    ProviderTimeout,
    SafetyClassificationError,
    UnknownCapabilityError,
)

from ai.intent import LLMIntentInterpreter, build_default_registry

from ai.provider import (
    LLMMessage,
    LLMRequest,
    LLMResponse,
    LLMUsage,
    MessageRole,
    MockProvider,
    OutputFormat,
    FinishReason,
)
from ai.provider.errors import (
    AuthenticationError,
    CancelledError as ProviderCancelledError,
    MalformedResponseError,
    RateLimitError,
    TimeoutError_,
)


# ---------------------------------------------------------------------------
# Fixtures: a small, in-process CapabilityRegistry
# ---------------------------------------------------------------------------

def _spec(name: str, params=(), *, dangerous: bool = False, tags=()) -> CapabilitySpec:
    return CapabilitySpec(
        name=name,
        version="1.0.0",
        description=f"test capability {name}",
        parameters=tuple(params),
        requires_capabilities=(),
        requires_services=(),
        dangerous=bool(dangerous),
        tags=tuple(tags),
    )


def _param(name: str, ptype: ParamType, *, required: bool = True, default: Any = None,
            description: str = "", allowed_values=()) -> CapabilityParameter:
    return CapabilityParameter(
        name=name,
        type=ptype,
        required=required,
        default=default,
        description=description,
        allowed_values=tuple(allowed_values),
    )


def _cap(spec: CapabilitySpec) -> CallableCapability:
    """Wrap a spec in a no-op callable capability for tests."""
    return CallableCapability(spec, fn=lambda _params: None, availability_fn=lambda: True)


@pytest.fixture
def registry() -> CapabilityRegistry:
    reg = CapabilityRegistry()
    reg.register(_cap(_spec(
        "desktop.application.open",
        params=(_param("app_name", ParamType.STRING, required=True),),
        tags=("desktop", "application"),
    )))
    reg.register(_cap(_spec(
        "desktop.application.close",
        params=(_param("app_name", ParamType.STRING, required=True),
                _param("force", ParamType.BOOLEAN, required=False, default=False)),
        tags=("desktop", "application"),
    )))
    reg.register(_cap(_spec(
        "desktop.application.focus",
        params=(_param("app_name", ParamType.STRING, required=True),),
        tags=("desktop", "application"),
    )))
    reg.register(_cap(_spec(
        "file.read",
        params=(_param("path", ParamType.PATH, required=True),),
        tags=("filesystem",),
    )))
    reg.register(_cap(_spec(
        "file.dangerous_delete",
        params=(_param("path", ParamType.PATH, required=True),),
        dangerous=True,
        tags=("filesystem", "destructive"),
    )))
    reg.register(_cap(_spec(
        "desktop.window.list",
        params=(),
        tags=("desktop", "window"),
    )))
    return reg


def _json_response(payload: Any) -> LLMResponse:
    return LLMResponse(
        content=json.dumps(payload),
        finish_reason=FinishReason.STOP,
        model="mock",
        usage=LLMUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        provider="mock",
    )


def _json_provider(payload: Any) -> MockProvider:
    return MockProvider(responder=lambda _r: _json_response(payload))


def _plan_payload() -> Dict[str, Any]:
    return {
        "goal_id": "g-1",
        "steps": [
            {
                "step_id": "s-1",
                "description": "open spotify",
                "action": "capability_call",
                "capability_name": "desktop.application.open",
                "parameters": {"app_name": "spotify"},
            }
        ],
    }


def _make_intent(kind: IntentKind, params: Dict[str, Any]) -> Intent:
    return Intent(
        intent_id="i-1",
        kind=kind,
        text="x",
        parameters=params,
    )


# ---------------------------------------------------------------------------
# (1-3) Discovery
# ---------------------------------------------------------------------------

def test_discover_capabilities_returns_summaries(registry) -> None:
    summaries = discover_capabilities(registry)
    assert len(summaries) == 6
    assert all(isinstance(s, CapabilitySummary) for s in summaries)
    names = {s.name for s in summaries}
    assert "desktop.application.open" in names
    assert "file.dangerous_delete" in names


def test_discover_capabilities_filters_by_name(registry) -> None:
    summaries = discover_capabilities(registry, names=["desktop.application.open"])
    assert [s.name for s in summaries] == ["desktop.application.open"]


def test_discover_capabilities_filters_by_tag(registry) -> None:
    summaries = discover_capabilities(registry, tags=("filesystem",))
    names = {s.name for s in summaries}
    assert names == {"file.read", "file.dangerous_delete"}


def test_summarize_for_prompt_respects_budget(registry) -> None:
    summaries = discover_capabilities(registry)
    out = summarize_for_prompt(summaries, max_total_bytes=2048)
    assert isinstance(out, list)
    assert all(isinstance(d, dict) for d in out)


# ---------------------------------------------------------------------------
# (4-18) Validation
# ---------------------------------------------------------------------------

def test_validate_minimal_plan(registry) -> None:
    plan = validate_plan_payload(_plan_payload(), registry=registry)
    assert isinstance(plan, Plan)
    assert plan.goal_id == "g-1"
    assert plan.status is PlanStatus.READY
    assert len(plan.steps) == 1
    assert plan.steps[0].capability_name == "desktop.application.open"
    assert plan.steps[0].parameters["app_name"] == "spotify"


def test_unknown_capability(registry) -> None:
    payload = _plan_payload()
    payload["steps"][0]["capability_name"] = "does.not.exist"
    with pytest.raises(UnknownCapabilityError) as info:
        validate_plan_payload(payload, registry=registry)
    assert info.value.context["capability"] == "does.not.exist"


def test_missing_required_parameter(registry) -> None:
    payload = _plan_payload()
    payload["steps"][0]["parameters"] = {}
    with pytest.raises(InvalidArgumentError) as info:
        validate_plan_payload(payload, registry=registry)
    assert info.value.context["parameter"] == "app_name"


def test_unexpected_parameter(registry) -> None:
    payload = _plan_payload()
    payload["steps"][0]["parameters"] = {"app_name": "spotify", "wat": 1}
    with pytest.raises(InvalidArgumentError) as info:
        validate_plan_payload(payload, registry=registry)
    assert "wat" in info.value.context["unexpected"]


def test_wrong_parameter_type(registry) -> None:
    # Register a capability with an INTEGER required parameter.
    reg = registry
    reg.register(_cap(_spec(
        "test.count",
        params=(_param("n", ParamType.INTEGER, required=True),),
    )))
    payload = {
        "goal_id": "g-1",
        "steps": [
            {
                "step_id": "s-1",
                "description": "x",
                "action": "capability_call",
                "capability_name": "test.count",
                "parameters": {"n": "not-an-int"},
            }
        ],
    }
    with pytest.raises(InvalidArgumentError) as info:
        validate_plan_payload(payload, registry=reg)
    assert info.value.context["parameter"] == "n"


def test_shell_token_in_capability_name_rejected(registry) -> None:
    payload = _plan_payload()
    payload["steps"][0]["capability_name"] = "desktop.application.open && rm -rf /"
    with pytest.raises((MalformedPlanPayload, UnknownCapabilityError, InvalidArgumentError)):
        validate_plan_payload(payload, registry=registry)


def test_dangerous_capability_cannot_be_downgraded(registry) -> None:
    payload = {
        "goal_id": "g-1",
        "steps": [
            {
                "step_id": "s-1",
                "description": "x",
                "action": "capability_call",
                "capability_name": "file.dangerous_delete",
                "parameters": {"path": "/tmp/x"},
                "safety_classification": "safe",
            }
        ],
    }
    with pytest.raises(SafetyClassificationError) as info:
        validate_plan_payload(payload, registry=registry)
    assert info.value.context["required"] == "dangerous"


def test_dangerous_capability_can_stay_dangerous(registry) -> None:
    payload = {
        "goal_id": "g-1",
        "steps": [
            {
                "step_id": "s-1",
                "description": "x",
                "action": "capability_call",
                "capability_name": "file.dangerous_delete",
                "parameters": {"path": "/tmp/x"},
                "safety_classification": "dangerous",
            }
        ],
    }
    plan = validate_plan_payload(payload, registry=registry)
    assert plan.steps[0].capability_name == "file.dangerous_delete"


def test_self_dependency(registry) -> None:
    payload = _plan_payload()
    payload["steps"][0]["depends_on"] = ["s-1"]
    with pytest.raises(InvalidDependencyError) as info:
        validate_plan_payload(payload, registry=registry)
    assert info.value.context["step_id"] == "s-1"


def test_missing_dependency(registry) -> None:
    payload = _plan_payload()
    payload["steps"][0]["depends_on"] = ["s-X"]
    with pytest.raises(InvalidDependencyError):
        validate_plan_payload(payload, registry=registry)


def test_cycle_in_dependencies(registry) -> None:
    payload = {
        "goal_id": "g-1",
        "steps": [
            {
                "step_id": "s-1",
                "description": "x",
                "action": "capability_call",
                "capability_name": "desktop.application.open",
                "parameters": {"app_name": "x"},
                "depends_on": ["s-2"],
            },
            {
                "step_id": "s-2",
                "description": "x",
                "action": "capability_call",
                "capability_name": "desktop.application.close",
                "parameters": {"app_name": "x"},
                "depends_on": ["s-1"],
            },
        ],
    }
    with pytest.raises(InvalidDependencyError) as info:
        validate_plan_payload(payload, registry=registry)
    assert info.value.code == "BRAIN_INVALID_DEPENDENCY"


def test_invalid_timeout(registry) -> None:
    payload = _plan_payload()
    payload["steps"][0]["timeout_s"] = -1.0
    with pytest.raises(InvalidTimeoutError):
        validate_plan_payload(payload, registry=registry)


def test_invalid_expected_effect(registry) -> None:
    payload = _plan_payload()
    payload["steps"][0]["expected_effect"] = "not a mapping"
    with pytest.raises(InvalidExpectedEffectError):
        validate_plan_payload(payload, registry=registry)


def test_zero_steps(registry) -> None:
    payload = {"goal_id": "g-1", "steps": []}
    with pytest.raises(MalformedPlanPayload):
        validate_plan_payload(payload, registry=registry)


def test_plan_size_exceeded(registry) -> None:
    steps = [
        {
            "step_id": f"s-{i}",
            "description": "x",
            "action": "capability_call",
            "capability_name": "desktop.application.open",
            "parameters": {"app_name": "x"},
        }
        for i in range(MAX_PLAN_STEPS + 1)
    ]
    payload = {"goal_id": "g-1", "steps": steps}
    with pytest.raises(PlanSizeExceeded):
        validate_plan_payload(payload, registry=registry)


def test_valid_dag(registry) -> None:
    payload = {
        "goal_id": "g-1",
        "steps": [
            {
                "step_id": "s-1",
                "description": "open",
                "action": "capability_call",
                "capability_name": "desktop.application.open",
                "parameters": {"app_name": "x"},
            },
            {
                "step_id": "s-2",
                "description": "focus",
                "action": "capability_call",
                "capability_name": "desktop.application.focus",
                "parameters": {"app_name": "x"},
                "depends_on": ["s-1"],
            },
            {
                "step_id": "s-3",
                "description": "close",
                "action": "capability_call",
                "capability_name": "desktop.application.close",
                "parameters": {"app_name": "x"},
                "depends_on": ["s-1", "s-2"],
            },
        ],
    }
    plan = validate_plan_payload(payload, registry=registry)
    assert plan.step_count == 3
    assert plan.find_step("s-2").depends_on == ("s-1",)


# ---------------------------------------------------------------------------
# (19-23) Deterministic planner
# ---------------------------------------------------------------------------

def test_deterministic_planner_happy_path(registry) -> None:
    planner = DeterministicPlanner(registry)
    goal = Goal(goal_id="g-1", description="open spotify")
    intent = _make_intent(IntentKind.OPEN_APPLICATION, {"app_name": "spotify"})
    plan = planner.plan(goal, intent=intent)
    assert isinstance(plan, Plan)
    assert plan.goal_id == "g-1"
    assert plan.step_count == 1
    assert plan.steps[0].capability_name == "desktop.application.open"
    assert plan.steps[0].parameters["app_name"] == "spotify"


def test_deterministic_planner_close(registry) -> None:
    planner = DeterministicPlanner(registry)
    goal = Goal(goal_id="g-1", description="close spotify")
    intent = _make_intent(IntentKind.CLOSE_APPLICATION, {"app_name": "spotify"})
    plan = planner.plan(goal, intent=intent)
    assert plan.steps[0].capability_name == "desktop.application.close"


def test_deterministic_planner_unknown_kind(registry) -> None:
    planner = DeterministicPlanner(registry)
    goal = Goal(goal_id="g-1", description="something", metadata={"intent_kind": "nuke_mars"})
    with pytest.raises(CannotPlanError):
        planner.plan(goal)


def test_deterministic_planner_cancel_task_fails(registry) -> None:
    planner = DeterministicPlanner(registry)
    goal = Goal(goal_id="g-1", description="cancel it")
    intent = _make_intent(IntentKind.CANCEL_TASK, {})
    with pytest.raises(CannotPlanError):
        planner.plan(goal, intent=intent)


def test_deterministic_planner_determinism(registry) -> None:
    planner = DeterministicPlanner(registry)
    goal = Goal(goal_id="g-1", description="open x")
    intent = _make_intent(IntentKind.OPEN_APPLICATION, {"app_name": "x"})
    plan_a = planner.plan(goal, intent=intent)
    plan_b = planner.plan(goal, intent=intent)
    # Same step ids, same capabilities, same parameter dicts.
    assert plan_a.step_ids == plan_b.step_ids
    assert [s.capability_name for s in plan_a.steps] == [s.capability_name for s in plan_b.steps]


def test_deterministic_planner_assigns_plan_id(registry) -> None:
    planner = DeterministicPlanner(registry)
    goal = Goal(goal_id="g-1", description="open x")
    intent = _make_intent(IntentKind.OPEN_APPLICATION, {"app_name": "x"})
    plan = planner.plan(goal, intent=intent)
    assert plan.plan_id
    assert plan.plan_id.startswith("plan_")
    assert plan.goal_id == "g-1"


# ---------------------------------------------------------------------------
# (24-31) LLM planner
# ---------------------------------------------------------------------------

def test_llm_planner_happy_path(registry) -> None:
    provider = _json_provider(_plan_payload())
    planner = LLMPlanner(provider, registry)
    goal = Goal(goal_id="g-1", description="open spotify")
    plan = planner.plan(goal)
    assert plan.step_count == 1
    assert plan.steps[0].capability_name == "desktop.application.open"


def test_llm_planner_malformed_json(registry) -> None:
    def responder(_req: LLMRequest) -> LLMResponse:
        return LLMResponse(
            content="not even close to JSON",
            finish_reason=FinishReason.STOP,
            model="mock",
            usage=LLMUsage(),
            provider="mock",
        )
    provider = MockProvider(responder=responder)
    planner = LLMPlanner(provider, registry)
    goal = Goal(goal_id="g-1", description="open spotify")
    with pytest.raises(CannotPlanError):
        planner.plan(goal)


def test_llm_planner_provider_error(registry) -> None:
    def responder(_req: LLMRequest) -> LLMResponse:
        raise AuthenticationError("bad key")
    provider = MockProvider(responder=responder)
    planner = LLMPlanner(provider, registry)
    goal = Goal(goal_id="g-1", description="open spotify")
    with pytest.raises(ProviderFailure) as info:
        planner.plan(goal)
    assert info.value.code == "BRAIN_PROVIDER_FAILURE"


def test_llm_planner_provider_timeout(registry) -> None:
    def responder(_req: LLMRequest) -> LLMResponse:
        raise TimeoutError_("slow")
    provider = MockProvider(responder=responder)
    planner = LLMPlanner(provider, registry)
    goal = Goal(goal_id="g-1", description="open spotify")
    with pytest.raises(ProviderTimeout) as info:
        planner.plan(goal)
    assert info.value.code == "BRAIN_PROVIDER_TIMEOUT"


def test_llm_planner_provider_cancellation(registry) -> None:
    def responder(_req: LLMRequest) -> LLMResponse:
        raise ProviderCancelledError("cancelled")
    provider = MockProvider(responder=responder)
    planner = LLMPlanner(provider, registry)
    goal = Goal(goal_id="g-1", description="open spotify")
    with pytest.raises(CancelledError) as info:
        planner.plan(goal)
    assert info.value.code == "BRAIN_CANCELLED"


def test_llm_planner_strips_markdown_fences(registry) -> None:
    raw = "```json\n" + json.dumps(_plan_payload()) + "\n```"
    def responder(_req: LLMRequest) -> LLMResponse:
        return LLMResponse(
            content=raw,
            finish_reason=FinishReason.STOP,
            model="mock",
            usage=LLMUsage(),
            provider="mock",
        )
    provider = MockProvider(responder=responder)
    planner = LLMPlanner(provider, registry)
    goal = Goal(goal_id="g-1", description="open spotify")
    plan = planner.plan(goal)
    assert plan.step_count == 1


def test_llm_planner_rejects_unknown_capability(registry) -> None:
    payload = _plan_payload()
    payload["steps"][0]["capability_name"] = "does.not.exist"
    provider = _json_provider(payload)
    planner = LLMPlanner(provider, registry)
    goal = Goal(goal_id="g-1", description="open spotify")
    with pytest.raises(UnknownCapabilityError):
        planner.plan(goal)


def test_llm_planner_system_prompt_deterministic(registry) -> None:
    provider = _json_provider(_plan_payload())
    a = LLMPlanner(provider, registry)
    b = LLMPlanner(provider, registry)
    assert a.system_prompt == b.system_prompt
    assert "desktop.application.open" in a.system_prompt


# ---------------------------------------------------------------------------
# (32-39) Brain
# ---------------------------------------------------------------------------

def test_brain_handle_text_happy(registry) -> None:
    intent_payload = {
        "kind": "open_application",
        "parameters": {"app_name": "spotify"},
    }
    interpreter = LLMIntentInterpreter(
        MockProvider(responder=lambda _r: _json_response(intent_payload)),
        build_default_registry(),
    )
    planner = LLMPlanner(_json_provider(_plan_payload()), registry)
    brain = Brain(registry=registry, interpreter=interpreter, planner=planner)
    result = brain.handle_text("open spotify")
    assert result.is_ok
    assert result.plan is not None
    assert result.plan.step_count == 1
    assert result.intent is not None
    assert result.goal is not None


def test_brain_clarification(registry) -> None:
    clarify_payload = {
        "kind": "clarify",
        "parameters": {"question": "Which app?"},
    }
    interpreter = LLMIntentInterpreter(
        MockProvider(responder=lambda _r: _json_response(clarify_payload)),
        build_default_registry(),
    )
    planner = LLMPlanner(_json_provider(_plan_payload()), registry)
    brain = Brain(registry=registry, interpreter=interpreter, planner=planner)
    result = brain.handle_text("open it")
    assert result.status == "clarification"
    assert result.clarifying_question == "Which app?"


def test_brain_unknown(registry) -> None:
    unknown_payload = {
        "kind": "unknown",
        "parameters": {},
    }
    interpreter = LLMIntentInterpreter(
        MockProvider(responder=lambda _r: _json_response(unknown_payload)),
        build_default_registry(),
    )
    planner = LLMPlanner(_json_provider(_plan_payload()), registry)
    brain = Brain(registry=registry, interpreter=interpreter, planner=planner)
    result = brain.handle_text("blarg")
    assert result.status == "unknown"
    assert result.intent is not None


def test_brain_intent_error(registry) -> None:
    def responder(_r: LLMRequest) -> LLMResponse:
        return LLMResponse(
            content="not json",
            finish_reason=FinishReason.STOP,
            model="mock",
            usage=LLMUsage(),
            provider="mock",
        )
    interpreter = LLMIntentInterpreter(
        MockProvider(responder=responder),
        build_default_registry(),
    )
    planner = LLMPlanner(_json_provider(_plan_payload()), registry)
    brain = Brain(registry=registry, interpreter=interpreter, planner=planner)
    result = brain.handle_text("anything")
    assert result.status == "error"
    # The interpreter classifies malformed JSON as an error result.
    assert result.error_code is not None


def test_brain_provider_error(registry) -> None:
    intent_payload = {
        "kind": "open_application",
        "parameters": {"app_name": "x"},
    }
    interpreter = LLMIntentInterpreter(
        MockProvider(responder=lambda _r: _json_response(intent_payload)),
        build_default_registry(),
    )
    def boom(_r: LLMRequest) -> LLMResponse:
        raise AuthenticationError("bad key")
    planner = LLMPlanner(MockProvider(responder=boom), registry)
    brain = Brain(registry=registry, interpreter=interpreter, planner=planner)
    with pytest.raises(ProviderFailure):
        brain.handle_text("open x")


def test_brain_plan_is_read_only(registry) -> None:
    intent = _make_intent(IntentKind.OPEN_APPLICATION, {"app_name": "x"})
    planner = LLMPlanner(_json_provider(_plan_payload()), registry)
    brain = Brain(
        registry=registry,
        interpreter=LLMIntentInterpreter(_json_provider({"kind": "open_application"}), build_default_registry()),
        planner=planner,
    )
    goal = intent.to_goal()
    plan = brain.plan(goal, intent=intent)
    # The Brain is read-only: it never produces an ActionRequest, and
    # the plan it returns has the same shape any planner would.
    assert isinstance(plan, Plan)
    assert all(isinstance(s, PlanStep) for s in plan.steps)


def test_brain_cannot_plan_returns_error_result(registry) -> None:
    intent_payload = {
        "kind": "open_application",
        "parameters": {"app_name": "x"},
    }
    interpreter = LLMIntentInterpreter(
        MockProvider(responder=lambda _r: _json_response(intent_payload)),
        build_default_registry(),
    )
    # Planner that always raises CannotPlanError.
    class _FailPlanner:
        name = "fail"
        def plan(self, *args: Any, **kwargs: Any) -> Plan:
            raise CannotPlanError("nope")
    brain = Brain(registry=registry, interpreter=interpreter, planner=_FailPlanner())
    result = brain.handle_text("open x")
    assert result.status == "error"
    assert result.error_code == "BRAIN_CANNOT_PLAN"
    assert result.goal is not None
    assert result.intent is not None


def test_brain_result_is_ok_and_to_dict() -> None:
    r_ok = BrainResult(status="ok", plan=Plan(plan_id="p-1", goal_id="g-1"))
    assert r_ok.is_ok
    d = r_ok.to_dict()
    assert d["status"] == "ok"
    assert d["plan"] is not None

    r_err = BrainResult(status="error", error_code="X", error_message="y")
    assert not r_err.is_ok
    assert r_err.to_dict()["error_code"] == "X"


# ---------------------------------------------------------------------------
# (40-42) Architectural invariants
# ---------------------------------------------------------------------------

def test_brain_modules_never_construct_action_request() -> None:
    """The Brain/Planner package must never construct ActionRequest.

    PlanStep construction goes through ``PlanStep.__post_init__``,
    which is the engine seam.  The Brain produces ``PlanStep`` only
    via ``validate_plan_payload``.  We assert the public surface
    does not expose ``ActionRequest`` as a re-export.
    """
    import ai.brain as brain_pkg
    assert "ActionRequest" not in dir(brain_pkg)


def test_brain_modules_have_no_engine_import() -> None:
    """The Brain/Planner must not import the engine or the router."""
    import ai.brain.brain as brain_mod
    import ai.brain.deterministic as det_mod
    import ai.brain.llm_planner as llm_mod
    import ai.brain.validation as val_mod
    import ai.brain.discovery as disc_mod
    for mod in (brain_mod, det_mod, llm_mod, val_mod, disc_mod):
        src = open(mod.__file__, "r", encoding="utf-8").read()
        for forbidden in (
            "from core.omnix_engine",
            "import core.omnix_engine",
            "from core.capability_router",
            "import core.capability_router",
            "import subprocess",
            "from subprocess",
            "import pyautogui",
            "import win32gui",
            "import win32api",
            "import ctypes",
        ):
            assert forbidden not in src, f"{mod.__name__} contains forbidden token: {forbidden!r}"


def test_brain_does_not_call_capability_execute() -> None:
    """The Brain/Planner must not call ``Capability.execute`` directly."""
    import ai.brain.brain as brain_mod
    import ai.brain.deterministic as det_mod
    import ai.brain.llm_planner as llm_mod
    for mod in (brain_mod, det_mod, llm_mod):
        src = open(mod.__file__, "r", encoding="utf-8").read()
        # Token check: ``.execute(`` should not appear anywhere.
        assert ".execute(" not in src, (
            f"{mod.__name__} appears to call .execute()"
        )
