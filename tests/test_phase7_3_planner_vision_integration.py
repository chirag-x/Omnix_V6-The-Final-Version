"""
Phase 7.3 — Planner → Vision contract integration tests.

These tests are **deterministic** (no real screenshots, no real
mouse, no real keyboard, no real LLM).  They cover the typed
contract between the planner layer and the vision layer, end-to-end:

  * :func:`validate_plan_payload` accepts / rejects the new
    vision metadata fields in a stable way.
  * :class:`DeterministicPlanner` emits the contract for
    target-bearing intents.
  * A planner-shaped payload reaches the Agent as a
    :class:`TargetGroundingContract` and stops dispatch when vision
    is missing / blocked.

The tests are organized as an alphabet A–L (per the Phase 7.3
directive) so a missing test is easy to spot.  See the matching
report at ``docs/V6_PHASE_7_3_PLANNER_VISION_INTEGRATION_REPORT.md``.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from ai.brain.deterministic import DeterministicPlanner
from ai.brain.exceptions import (
    CannotPlanError,
    InvalidVisionMetadataError,
)
from ai.brain.validation import (
    ALLOWED_VISION_PRE_ACTIONS,
    VISION_GROUNDED_CAPABILITIES,
    validate_plan_payload,
)
from core.capability import (
    CallableCapability,
    CapabilityParameter,
    CapabilitySpec,
    ParamType,
)
from core.capability_registry import CapabilityRegistry
from core.orchestration.agent import Agent
from core.orchestration.grounding import (
    DEFAULT_CONFIDENCE_THRESHOLD,
    GroundingStatus,
    TargetGroundingContract,
)
from core.orchestration.models import ActionKind


# ----------------------------------------------------------- fakes


class _FakeVisionResult:
    """A duck-typed stand-in for ``core.services.vision_service.VisionResult``."""

    def __init__(
        self,
        status: str = "OBSERVED",
        observation: Optional[Dict[str, Any]] = None,
        resolution_method: str = "uia_synthetic",
        error: Optional[str] = None,
    ) -> None:
        self.status = status
        self.observation = observation
        self.resolution_method = resolution_method
        self.error = error


class _FakeVisionService:
    """A duck-typed vision service.  Same contract as Phase 7.2."""

    def __init__(self, scripted: List[_FakeVisionResult]) -> None:
        self._scripted = list(scripted)
        self.call_count_ = 0
        self.last_query_: Optional[str] = None

    def ground_target(
        self,
        target_query: str,
        *,
        preferred_strategy: Optional[str] = None,
    ) -> _FakeVisionResult:
        self.call_count_ += 1
        self.last_query_ = target_query
        if not self._scripted:
            raise AssertionError("FakeVisionService ran out of scripted results")
        return self._scripted.pop(0)


class _StubInterpreter:
    """An interpreter stub.  Not exercised in these tests; the Agent
    only needs *something* to hold the reference for ``__init__``."""

    async def interpret(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise NotImplementedError


class _StubPlanner:
    """A planner stub.  Same purpose as ``_StubInterpreter``."""

    async def plan(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise NotImplementedError


class _StubPlanExecutor:
    """A plan executor stub.  Not exercised; the Agent's
    ``_apply_pre_action_grounding`` is the only method under test."""

    async def execute(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise NotImplementedError


def _make_agent(vision_service: Any) -> Agent:
    """Build an Agent with the minimum collaborators required for
    :meth:`Agent._apply_pre_action_grounding` to be exercised.
    """
    return Agent(
        interpreter=_StubInterpreter(),  # type: ignore[arg-type]
        planner=_StubPlanner(),  # type: ignore[arg-type]
        plan_executor=_StubPlanExecutor(),  # type: ignore[arg-type]
        vision_service=vision_service,
        confidence_threshold=DEFAULT_CONFIDENCE_THRESHOLD,
    )


# ----------------------------------------------------------- fixtures


def _spec(
    name: str,
    params: tuple = (),
    *,
    dangerous: bool = False,
    tags: tuple = (),
) -> CapabilitySpec:
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


def _param(
    name: str,
    ptype: ParamType,
    *,
    required: bool = True,
    default: Any = None,
    description: str = "",
) -> CapabilityParameter:
    return CapabilityParameter(
        name=name,
        type=ptype,
        required=required,
        default=default,
        description=description,
    )


def _cap(spec: CapabilitySpec) -> CallableCapability:
    return CallableCapability(
        spec,
        fn=lambda _params: None,
        availability_fn=lambda: True,
    )


@pytest.fixture
def registry_fixture() -> CapabilityRegistry:
    """A small, in-process registry with the mouse click family.

    The deterministic planner and the validator both need to look up
    capabilities by name; we register only what the Phase 7.3
    contract exercises.  This keeps the tests independent of any
    global bootstrap.
    """
    reg = CapabilityRegistry()
    reg.register(_cap(_spec(
        "desktop.mouse.click",
        params=(
            _param("x", ParamType.INTEGER, required=False),
            _param("y", ParamType.INTEGER, required=False),
            _param("button", ParamType.STRING, required=False, default="left"),
        ),
        tags=("desktop", "mouse"),
    )))
    reg.register(_cap(_spec(
        "desktop.mouse.double_click",
        params=(
            _param("x", ParamType.INTEGER, required=False),
            _param("y", ParamType.INTEGER, required=False),
        ),
        tags=("desktop", "mouse"),
    )))
    reg.register(_cap(_spec(
        "desktop.mouse.right_click",
        params=(
            _param("x", ParamType.INTEGER, required=False),
            _param("y", ParamType.INTEGER, required=False),
        ),
        tags=("desktop", "mouse"),
    )))
    reg.register(_cap(_spec(
        "desktop.keyboard.type",
        params=(_param("text", ParamType.STRING, required=True),),
        tags=("desktop", "keyboard"),
    )))
    reg.register(_cap(_spec(
        "desktop.application.open",
        params=(_param("app_name", ParamType.STRING, required=True),),
        tags=("desktop", "application"),
    )))
    return reg


def _plan_with_vision_metadata(
    registry: CapabilityRegistry,
    *,
    capability_name: str,
    parameters: Dict[str, Any],
    metadata: Dict[str, Any],
) -> Any:
    """Build a one-step plan with the given vision metadata and run it
    through :func:`validate_plan_payload`.

    Validation is the only gate under test here; we do not invoke
    the Agent.
    """
    payload: Dict[str, Any] = {
        "goal_id": "goal_test",
        "steps": [
            {
                "step_id": "step_1",
                "description": "vision-grounded step",
                "action": ActionKind.CAPABILITY_CALL.value,
                "capability_name": capability_name,
                "parameters": parameters,
                "metadata": metadata,
            }
        ],
    }
    return validate_plan_payload(payload, registry=registry)


# ----------------------------------------------------------- tests


# A: A click with valid vision metadata validates cleanly.
def test_A_click_with_valid_vision_metadata_validates(
    registry_fixture: CapabilityRegistry,
) -> None:
    plan = _plan_with_vision_metadata(
        registry_fixture,
        capability_name="desktop.mouse.click",
        parameters={},  # x/y are not required when grounding is requested
        metadata={
            "vision_pre_action": "click",
            "vision_target_query": "the Save button",
        },
    )
    assert len(plan.steps) == 1
    step = plan.steps[0]
    assert step.metadata["vision_pre_action"] == "click"
    assert step.metadata["vision_target_query"] == "the Save button"


# B: An unknown pre_action kind is rejected.
def test_B_unknown_pre_action_is_rejected(
    registry_fixture: CapabilityRegistry,
) -> None:
    payload: Dict[str, Any] = {
        "goal_id": "goal_test",
        "steps": [
            {
                "step_id": "step_1",
                "description": "bad step",
                "action": ActionKind.CAPABILITY_CALL.value,
                "capability_name": "desktop.mouse.click",
                "parameters": {},
                "metadata": {
                    "vision_pre_action": "rocket_launch",
                    "vision_target_query": "the Save button",
                },
            }
        ],
    }
    with pytest.raises(InvalidVisionMetadataError):
        validate_plan_payload(payload, registry=registry_fixture)


# C: A click on a grounded capability without ANY vision metadata is rejected.
def test_C_click_without_vision_metadata_is_rejected(
    registry_fixture: CapabilityRegistry,
) -> None:
    payload: Dict[str, Any] = {
        "goal_id": "goal_test",
        "steps": [
            {
                "step_id": "step_1",
                "description": "unguarded click",
                "action": ActionKind.CAPABILITY_CALL.value,
                "capability_name": "desktop.mouse.click",
                "parameters": {},
                "metadata": {},
            }
        ],
    }
    with pytest.raises(InvalidVisionMetadataError):
        validate_plan_payload(payload, registry=registry_fixture)


# D: A click with vision_skip_grounding=True AND explicit x/y is accepted.
def test_D_explicit_coordinate_bypass_is_accepted(
    registry_fixture: CapabilityRegistry,
) -> None:
    plan = _plan_with_vision_metadata(
        registry_fixture,
        capability_name="desktop.mouse.click",
        parameters={"x": 100, "y": 200, "button": "left"},
        metadata={"vision_skip_grounding": True},
    )
    assert plan.steps[0].metadata["vision_skip_grounding"] is True
    assert plan.steps[0].parameters == {"x": 100, "y": 200, "button": "left"}


# E: A click with vision_skip_grounding=True but NO coordinates is rejected.
def test_E_skip_grounding_without_coordinates_is_rejected(
    registry_fixture: CapabilityRegistry,
) -> None:
    payload: Dict[str, Any] = {
        "goal_id": "goal_test",
        "steps": [
            {
                "step_id": "step_1",
                "description": "unsafe bypass",
                "action": ActionKind.CAPABILITY_CALL.value,
                "capability_name": "desktop.mouse.click",
                "parameters": {},
                "metadata": {"vision_skip_grounding": True},
            }
        ],
    }
    with pytest.raises(InvalidVisionMetadataError):
        validate_plan_payload(payload, registry=registry_fixture)


# F: Combining vision_pre_action and vision_skip_grounding is rejected.
def test_F_pre_action_and_skip_grounding_combination_rejected(
    registry_fixture: CapabilityRegistry,
) -> None:
    payload: Dict[str, Any] = {
        "goal_id": "goal_test",
        "steps": [
            {
                "step_id": "step_1",
                "description": "contradiction",
                "action": ActionKind.CAPABILITY_CALL.value,
                "capability_name": "desktop.mouse.click",
                "parameters": {"x": 1, "y": 2},
                "metadata": {
                    "vision_pre_action": "click",
                    "vision_target_query": "the Save button",
                    "vision_skip_grounding": True,
                },
            }
        ],
    }
    with pytest.raises(InvalidVisionMetadataError):
        validate_plan_payload(payload, registry=registry_fixture)


# G: A pre_action without a vision_target_query is rejected.
def test_G_pre_action_without_target_query_rejected(
    registry_fixture: CapabilityRegistry,
) -> None:
    payload: Dict[str, Any] = {
        "goal_id": "goal_test",
        "steps": [
            {
                "step_id": "step_1",
                "description": "missing target",
                "action": ActionKind.CAPABILITY_CALL.value,
                "capability_name": "desktop.mouse.click",
                "parameters": {},
                "metadata": {"vision_pre_action": "click"},
            }
        ],
    }
    with pytest.raises(InvalidVisionMetadataError):
        validate_plan_payload(payload, registry=registry_fixture)


# H: An unknown preferred_strategy is rejected.
def test_H_unknown_preferred_strategy_rejected(
    registry_fixture: CapabilityRegistry,
) -> None:
    payload: Dict[str, Any] = {
        "goal_id": "goal_test",
        "steps": [
            {
                "step_id": "step_1",
                "description": "bad strategy",
                "action": ActionKind.CAPABILITY_CALL.value,
                "capability_name": "desktop.mouse.click",
                "parameters": {},
                "metadata": {
                    "vision_pre_action": "click",
                    "vision_target_query": "the Save button",
                    "vision_preferred_strategy": "yolo",
                },
            }
        ],
    }
    with pytest.raises(InvalidVisionMetadataError):
        validate_plan_payload(payload, registry=registry_fixture)


# I: DeterministicPlanner emits the vision contract for ui_click_target.
def test_I_deterministic_planner_emits_vision_contract(
    registry_fixture: CapabilityRegistry,
) -> None:
    from core.orchestration import Goal, Intent, IntentKind

    planner = DeterministicPlanner(registry_fixture)
    goal = Goal(
        goal_id="goal_click",
        description="click the Save button",
        success_criteria=("saved",),
        constraints=(),
        priority=1,
        metadata={"intent_kind": "ui_click_target"},
    )
    intent = Intent(
        intent_id="intent_click",
        kind=IntentKind.UI_CLICK_TARGET,
        text="click the Save button",
        parameters={"target_query": "the Save button"},
    )
    plan = planner.plan(goal, intent=intent)
    assert len(plan.steps) == 1
    step = plan.steps[0]
    assert step.capability_name == "desktop.mouse.click"
    assert step.metadata.get("vision_pre_action") == "click"
    assert step.metadata.get("vision_target_query") == "the Save button"


# J: DeterministicPlanner fails when target-bearing intent has no target_query.
def test_J_deterministic_planner_fails_without_target_query(
    registry_fixture: CapabilityRegistry,
) -> None:
    from core.orchestration import Goal, Intent, IntentKind

    planner = DeterministicPlanner(registry_fixture)
    goal = Goal(
        goal_id="goal_click",
        description="click the Save button",
        success_criteria=("saved",),
        constraints=(),
        priority=1,
        metadata={"intent_kind": "ui_click_target"},
    )
    intent = Intent(
        intent_id="intent_click",
        kind=IntentKind.UI_CLICK_TARGET,
        text="click the Save button",
        parameters={},  # no target_query
    )
    with pytest.raises(CannotPlanError):
        planner.plan(goal, intent=intent)


# K: End-to-end — a planner-shaped plan reaches the Agent and grounds
# through vision.  This is the integration that closes Phase 7.3: the
# plan emitted by the planner is honoured by the Agent's pre-action
# grounding.
def test_K_planner_to_agent_vision_grounding_integration(
    registry_fixture: CapabilityRegistry,
) -> None:
    from core.orchestration import Goal, Intent, IntentKind

    planner = DeterministicPlanner(registry_fixture)
    goal = Goal(
        goal_id="goal_click",
        description="click the Save button",
        success_criteria=("saved",),
        constraints=(),
        priority=1,
        metadata={"intent_kind": "ui_click_target"},
    )
    intent = Intent(
        intent_id="intent_click",
        kind=IntentKind.UI_CLICK_TARGET,
        text="click the Save button",
        parameters={
            "target_query": "the Save button",
            "preferred_strategy": "uia",
        },
    )
    plan = planner.plan(goal, intent=intent)

    # Build a vision service that returns a high-confidence ground.
    fake_vision = _FakeVisionService(
        [
            _FakeVisionResult(
                status="OBSERVED",
                observation={
                    "source": "uia",
                    "bbox": (100, 100, 200, 150),
                    "confidence": 0.95,
                    "text": "Save",
                    "properties": {},
                },
                resolution_method="uia",
            )
        ]
    )
    agent = _make_agent(fake_vision)

    contract, failure = agent._apply_pre_action_grounding(  # type: ignore[attr-defined]
        plan.steps[0], plan.plan_id
    )
    assert failure is None
    assert contract is not None
    assert contract.status is GroundingStatus.GROUNDED
    assert contract.target_query == "the Save button"
    assert contract.center == (150, 125)
    assert fake_vision.last_query_ == "the Save button"


# L: A blocked vision result flows back as a Failure and the Agent
# refuses to dispatch.  This proves the planner contract and the
# Agent contract share the same status vocabulary.
def test_L_blocked_vision_returns_failure(
    registry_fixture: CapabilityRegistry,
) -> None:
    from core.orchestration import Goal, Intent, IntentKind

    planner = DeterministicPlanner(registry_fixture)
    goal = Goal(
        goal_id="goal_click",
        description="click the Save button",
        success_criteria=("saved",),
        constraints=(),
        priority=1,
        metadata={"intent_kind": "ui_click_target"},
    )
    intent = Intent(
        intent_id="intent_click",
        kind=IntentKind.UI_CLICK_TARGET,
        text="click the Save button",
        parameters={"target_query": "the Save button"},
    )
    plan = planner.plan(goal, intent=intent)

    fake_vision = _FakeVisionService(
        [_FakeVisionResult(status="NOT_FOUND", error="no save button here")]
    )
    agent = _make_agent(fake_vision)
    contract, failure = agent._apply_pre_action_grounding(  # type: ignore[attr-defined]
        plan.steps[0], plan.plan_id
    )
    assert contract is None
    assert failure is not None
    # The Agent routes blocking vision results through FailureKind.VERIFICATION.
    assert failure.kind.value in ("verification", "VERIFICATION")


# ----------------------------------------------------------- constants


def test_allowed_pre_actions_match_adapter() -> None:
    """The validator's closed set must match the adapter's closed set."""
    from core.orchestration.vision_adapter import adapt_pre_action

    for kind in ALLOWED_VISION_PRE_ACTIONS:
        # Build a GROUNDED contract; we just want the dispatch to
        # recognise the kind, not to build a full request (we never
        # call adapt_pre_action with text= for type_into, so we
        # provide empty text for the kind that requires it).
        contract = TargetGroundingContract(
            status=GroundingStatus.GROUNDED,
            target_query="x",
            bbox=(0, 0, 10, 10),
            center=(5, 5),
            confidence=0.9,
        )
        # The adapter must NOT raise ValueError for these kinds.
        if kind == "type_into":
            # requires text
            result = adapt_pre_action(contract, kind=kind, text="hi")
        else:
            result = adapt_pre_action(contract, kind=kind)
        assert result is not None


def test_vision_grounded_capabilities_include_mouse_clicks() -> None:
    """The closed set of capabilities that always require vision."""
    assert "desktop.mouse.click" in VISION_GROUNDED_CAPABILITIES
    assert "desktop.mouse.double_click" in VISION_GROUNDED_CAPABILITIES
    assert "desktop.mouse.right_click" in VISION_GROUNDED_CAPABILITIES
