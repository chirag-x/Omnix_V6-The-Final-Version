"""
Phase 7.2 — Final Vision + Agent integration tests.

These tests are **deterministic** (no real screenshots, no real
mouse, no real keyboard).  They cover the contract that the Agent
honours vision grounding results *before* dispatching a click /
type / pre-action capability, and that the closed capability
set is preserved end-to-end.

The tests are organized as a single alphabet of A–S scenarios
(per the Phase 7.2 directive) so a missing test is easy to
spot.  See the matching report at
``docs/V6_PHASE_7_2_FINAL_VISION_INTEGRATION_REPORT.md``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import pytest

from core.orchestration.agent import Agent, _vision_result_to_contract
from core.orchestration.grounding import (
    DEFAULT_CONFIDENCE_THRESHOLD,
    GroundingStatus,
    TargetGroundingContract,
)
from core.orchestration.vision_adapter import (
    AdaptedAction,
    GroundingNotGroundableError,
    adapt_click,
    adapt_double_click,
    adapt_focus,
    adapt_pre_action,
    adapt_right_click,
    adapt_type_into,
    is_known_capability,
)


# ----------------------------------------------------------- fakes


@dataclass(frozen=True)
class FakeVisionResult:
    """A duck-typed stand-in for ``core.services.vision_service.VisionResult``."""

    status: str = "OBSERVED"
    observation: Optional[Dict[str, Any]] = None
    resolution_method: str = "uia_synthetic"
    error: Optional[str] = None


class FakeVisionService:
    """A duck-typed vision service that returns scripted results.

    The Agent must NOT call any method other than
    :meth:`ground_target`.  This fake only implements that one
    method, plus ``__call_count__`` for tests.
    """

    def __init__(self, scripted: List[FakeVisionResult]) -> None:
        self._scripted = list(scripted)
        self.call_count_ = 0
        self.last_query_ = None

    def ground_target(
        self,
        target_query: str,
        *,
        preferred_strategy: Optional[str] = None,
    ) -> FakeVisionResult:
        self.call_count_ += 1
        self.last_query_ = target_query
        if not self._scripted:
            raise AssertionError("FakeVisionService ran out of scripted results")
        return self._scripted.pop(0)


@dataclass(frozen=True)
class FakeStep:
    """A duck-typed :class:`PlanStep` for unit tests.

    Only the fields the Agent reads for pre-action grounding are
    populated.  This is enough to drive :meth:`Agent._apply_pre_action_grounding`
    end-to-end without depending on the real :class:`PlanStep`.
    """

    step_id: str = "s1"
    subject: str = "Save button"
    capability_name: str = "desktop.mouse.click"
    parameters: Dict[str, Any] = field(default_factory=lambda: {"x": 0, "y": 0})
    expected_effect: Any = None
    metadata: Dict[str, Any] = field(default_factory=dict)


def _grounded_result(
    *,
    bbox: Tuple[int, int, int, int] = (100, 200, 200, 240),
    confidence: float = 0.95,
    source: str = "uia",
) -> FakeVisionResult:
    l, t, r, b = bbox
    return FakeVisionResult(
        status="OBSERVED",
        observation={
            "source": source,
            "bbox": [l, t, r, b],
            "confidence": confidence,
            "text": "Save",
        },
        resolution_method="uia_synthetic",
    )


# ============================================================== A


def test_A_vision_result_to_contract_grounded():
    """A. ``_vision_result_to_contract`` translates ``OBSERVED`` to ``GROUNDED``."""
    vr = _grounded_result(confidence=0.9)
    contract = _vision_result_to_contract(vr, target_query="Save button")
    assert contract.status is GroundingStatus.GROUNDED
    assert contract.is_grounded is True
    assert contract.center == (150, 220)
    assert contract.bbox == (100, 200, 200, 240)
    assert contract.confidence == pytest.approx(0.9)
    assert contract.resolution_method == "uia_synthetic"


# ============================================================== B


def test_B_vision_result_to_contract_not_found():
    """B. ``NOT_FOUND`` translates to ``GroundingStatus.NOT_FOUND`` (blocking)."""
    vr = FakeVisionResult(status="NOT_FOUND", error="nothing matched")
    contract = _vision_result_to_contract(vr, target_query="ghost")
    assert contract.status is GroundingStatus.NOT_FOUND
    assert contract.is_blocking is True
    assert contract.error == "nothing matched"
    assert contract.center is None
    assert contract.confidence == 0.0


# ============================================================== C


def test_C_vision_result_to_contract_ambiguous():
    """C. ``AMBIGUOUS`` carries the candidates list through the contract."""
    vr = FakeVisionResult(
        status="AMBIGUOUS",
        observation={
            "candidates": [
                {"source": "uia", "bbox": [0, 0, 10, 10], "confidence": 0.8},
                {"source": "uia", "bbox": [20, 0, 30, 10], "confidence": 0.8},
            ]
        },
    )
    contract = _vision_result_to_contract(vr, target_query="two buttons")
    assert contract.status is GroundingStatus.AMBIGUOUS
    assert contract.is_blocking is True
    assert len(contract.candidates) == 2


# ============================================================== D


def test_D_apply_pre_action_grounding_skips_without_metadata():
    """D. Steps without ``vision_pre_action`` are passed through unchanged."""
    # We need an Agent instance, but only to call a method that
    # does not touch its collaborators.  Bypass __init__ for
    # this unit test.
    agent = Agent.__new__(Agent)
    agent.vision_service = FakeVisionService([])
    agent.confidence_threshold = DEFAULT_CONFIDENCE_THRESHOLD

    step = FakeStep(metadata={})  # no vision_pre_action
    contract, failure = agent._apply_pre_action_grounding(step, "p1")
    assert failure is None
    assert contract is not None
    assert contract.status is GroundingStatus.SKIPPED
    assert agent.vision_service.call_count_ == 0  # not consulted


# ============================================================== E


def test_E_apply_pre_action_grounding_grounded_succeeds():
    """E. A grounded contract with high confidence returns ``(contract, None)``."""
    agent = Agent.__new__(Agent)
    agent.vision_service = FakeVisionService([_grounded_result()])
    agent.confidence_threshold = DEFAULT_CONFIDENCE_THRESHOLD

    step = FakeStep(metadata={"vision_pre_action": "click"})
    contract, failure = agent._apply_pre_action_grounding(step, "p1")
    assert failure is None
    assert contract is not None
    assert contract.is_grounded
    assert contract.center == (150, 220)
    assert agent.vision_service.call_count_ == 1


# ============================================================== F


def test_F_apply_pre_action_grounding_below_threshold_safety_failure():
    """F. Confidence < threshold returns a SAFETY failure (vision refused)."""
    agent = Agent.__new__(Agent)
    agent.vision_service = FakeVisionService(
        [_grounded_result(confidence=0.1)]
    )
    agent.confidence_threshold = 0.5

    step = FakeStep(metadata={"vision_pre_action": "click"})
    contract, failure = agent._apply_pre_action_grounding(step, "p1")
    assert contract is None
    assert failure is not None
    assert failure.kind.value == "safety"
    assert "0.10" in failure.message and "0.50" in failure.message


# ============================================================== G


def test_G_apply_pre_action_grounding_not_found_verification_failure():
    """G. ``NOT_FOUND`` returns a VERIFICATION failure (retryable)."""
    agent = Agent.__new__(Agent)
    agent.vision_service = FakeVisionService(
        [FakeVisionResult(status="NOT_FOUND", error="no candidate")]
    )
    agent.confidence_threshold = 0.5

    step = FakeStep(metadata={"vision_pre_action": "click"})
    contract, failure = agent._apply_pre_action_grounding(step, "p1")
    assert contract is None
    assert failure is not None
    assert failure.kind.value == "verification"
    assert failure.is_retryable is True


# ============================================================== H


def test_H_apply_pre_action_grounding_no_vision_service_safety_failure():
    """H. Step with ``vision_pre_action`` but no ``vision_service`` is a SAFETY failure."""
    agent = Agent.__new__(Agent)
    agent.vision_service = None
    agent.confidence_threshold = 0.5

    step = FakeStep(metadata={"vision_pre_action": "click"})
    contract, failure = agent._apply_pre_action_grounding(step, "p1")
    assert contract is None
    assert failure is not None
    assert failure.kind.value == "safety"
    assert "no vision_service" in failure.message


# ============================================================== I


def test_I_adapt_click_produces_action_request():
    """I. ``adapt_click`` produces a closed-set ``desktop.mouse.click`` request."""
    contract = TargetGroundingContract(
        status=GroundingStatus.GROUNDED,
        target_query="Save",
        bbox=(10, 20, 30, 40),
        center=(20, 30),
        confidence=0.9,
        resolution_method="uia",
    )
    out = adapt_click(contract)
    assert isinstance(out, AdaptedAction)
    assert out.capability_name == "desktop.mouse.click"
    assert out.request.parameters == {"x": 20, "y": 30}
    assert out.request.expected_effect.check_name == "vision_target_clicked"
    assert is_known_capability(out.capability_name)


# ============================================================== J


def test_J_adapt_pre_action_dispatches_by_kind():
    """J. ``adapt_pre_action`` dispatches correctly for all kinds."""
    contract = TargetGroundingContract(
        status=GroundingStatus.GROUNDED,
        target_query="X",
        bbox=(0, 0, 10, 10),
        center=(5, 5),
        confidence=0.9,
    )
    assert adapt_pre_action(contract, kind="click").capability_name == "desktop.mouse.click"
    assert adapt_pre_action(contract, kind="double_click").capability_name == "desktop.mouse.double_click"
    assert adapt_pre_action(contract, kind="right_click").capability_name == "desktop.mouse.right_click"
    assert adapt_pre_action(contract, kind="focus").capability_name == "desktop.mouse.move"


# ============================================================== K


def test_K_adapt_pre_action_rejects_unknown_kind():
    """K. ``adapt_pre_action`` rejects unknown kinds without inventing names."""
    contract = TargetGroundingContract(
        status=GroundingStatus.GROUNDED,
        target_query="X",
        bbox=(0, 0, 10, 10),
        center=(5, 5),
        confidence=0.9,
    )
    with pytest.raises(ValueError):
        adapt_pre_action(contract, kind="teleport")


# ============================================================== L


def test_L_adapt_click_rejects_non_grounded_contract():
    """L. Adapter refuses to dispatch a non-GROUNDED contract (R-21)."""
    contract = TargetGroundingContract(
        status=GroundingStatus.NOT_FOUND,
        target_query="ghost",
    )
    with pytest.raises(GroundingNotGroundableError):
        adapt_click(contract)


# ============================================================== M


def test_M_adapt_type_into_requires_text():
    """M. ``adapt_type_into`` rejects empty text (the adapter is strict)."""
    contract = TargetGroundingContract(
        status=GroundingStatus.GROUNDED,
        target_query="X",
        bbox=(0, 0, 10, 10),
        center=(5, 5),
        confidence=0.9,
    )
    with pytest.raises(ValueError):
        adapt_type_into(contract, text="")
    with pytest.raises(ValueError):
        adapt_type_into(contract, text=None)  # type: ignore[arg-type]


# ============================================================== N


def test_N_adapt_double_click_and_right_click_are_distinct():
    """N. Double-click and right-click produce *different* closed capabilities."""
    contract = TargetGroundingContract(
        status=GroundingStatus.GROUNDED,
        target_query="X",
        bbox=(0, 0, 10, 10),
        center=(5, 5),
        confidence=0.9,
    )
    d = adapt_double_click(contract)
    r = adapt_right_click(contract)
    assert d.capability_name != r.capability_name
    assert d.capability_name == "desktop.mouse.double_click"
    assert r.capability_name == "desktop.mouse.right_click"


# ============================================================== O


def test_O_adapt_focus_uses_mouse_move():
    """O. Focus is a ``mouse.move`` (no side-effects on the UI)."""
    contract = TargetGroundingContract(
        status=GroundingStatus.GROUNDED,
        target_query="X",
        bbox=(0, 0, 10, 10),
        center=(5, 5),
        confidence=0.9,
    )
    out = adapt_focus(contract)
    assert out.capability_name == "desktop.mouse.move"
    assert out.request.parameters == {"x": 5, "y": 5}


# ============================================================== P


def test_P_apply_pre_action_grounding_internal_failure_on_vision_exception():
    """P. A vision service that raises is reported as INTERNAL, not SAFETY."""
    class ExplodingVision:
        def ground_target(self, target_query, **kwargs):
            raise RuntimeError("camera unplugged")

    agent = Agent.__new__(Agent)
    agent.vision_service = ExplodingVision()
    agent.confidence_threshold = 0.5

    step = FakeStep(metadata={"vision_pre_action": "click"})
    contract, failure = agent._apply_pre_action_grounding(step, "p1")
    assert contract is None
    assert failure is not None
    assert failure.kind.value == "internal"
    assert "camera unplugged" in failure.message


# ============================================================== Q


def test_Q_vision_result_to_contract_center_is_precomputed():
    """Q. The contract's center is pre-computed (adapter does not recompute)."""
    vr = _grounded_result(bbox=(0, 0, 100, 50))
    contract = _vision_result_to_contract(vr, target_query="X")
    assert contract.center == (50, 25)
    # The adapter MUST use contract.center, not recompute.
    out = adapt_click(contract)
    assert out.request.parameters == {"x": 50, "y": 25}


# ============================================================== R


def test_R_apply_pre_action_grounding_uses_step_metadata_target():
    """R. The Agent honours ``vision_target_query`` from step metadata."""
    agent = Agent.__new__(Agent)
    vision = FakeVisionService([_grounded_result()])
    agent.vision_service = vision
    agent.confidence_threshold = 0.5

    step = FakeStep(
        metadata={
            "vision_pre_action": "click",
            "vision_target_query": "the Open button",
        }
    )
    contract, failure = agent._apply_pre_action_grounding(step, "p1")
    assert failure is None
    assert vision.last_query_ == "the Open button"
    assert contract.target_query == "the Open button"


# ============================================================== S


def test_S_agent_constructor_accepts_vision_service():
    """S. ``Agent.__init__`` accepts a vision_service without changing other behaviour."""
    # We do NOT need a working Agent here; we only need the
    # constructor to accept the new optional parameter.
    from core.orchestration.agent import Agent, AgentPolicy
    from core.orchestration.interfaces import (
        IntentInterpreter,
        Planner,
        PlanExecutor,
    )

    class StubInterpreter:
        name = "stub-interp"
        def interpret(self, text, *, context_snapshot=None):
            return None

    class StubPlanner:
        name = "stub-plan"
        def plan(self, goal, *, intent=None, prior_plan=None, failure=None):
            return None

    class StubExecutor:
        name = "stub-exec"
        def execute(self, ctx):
            return None

    # Should not raise.  Vision not configured → fine.
    agent = Agent(
        interpreter=StubInterpreter(),
        planner=StubPlanner(),
        plan_executor=StubExecutor(),
        vision_service=FakeVisionService([]),
        confidence_threshold=0.7,
    )
    assert agent.vision_service is not None
    assert agent.confidence_threshold == 0.7
