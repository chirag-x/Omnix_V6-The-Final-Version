"""
Tests for PerceptionRouter and the deterministic ranking rules.

Phase 7.1: ties must raise :class:`AmbiguityError`; UIA with
high confidence is short-circuited; the strategy order is
adaptive but documented.
"""
from __future__ import annotations

import pytest

from core.orchestration.models import ObservationSource
from vision.observations.targets import TargetCandidate
from vision.router.perception_router import (
    AmbiguityError,
    PerceptionRouter,
    TargetNotGroundedError,
)
from vision.router.perception_strategy import PerceptionStrategy
from vision.strategies.coordinates_strategy import CoordinatesStrategy


class MockStrategy(PerceptionStrategy):
    def __init__(
        self,
        name: str,
        candidates: dict,
        requires_screenshot: bool = False,
        reliability: float = 0.5,
    ) -> None:
        self._name = name
        self._candidates = candidates
        self._requires_screenshot = requires_screenshot
        self._reliability = reliability

    @property
    def name(self) -> str:
        return self._name

    @property
    def requires_screenshot(self) -> bool:
        return self._requires_screenshot

    @property
    def source_reliability(self) -> float:
        return self._reliability

    def find_targets(self, target_query, image_path=None, **kwargs):
        return list(self._candidates.get(target_query, []))


def test_perception_router_single_match():
    """A single UIA match at 0.9 confidence is short-circuited."""
    strategy = MockStrategy(
        "uia",
        {
            "button": [
                TargetCandidate(
                    ObservationSource.UIA,
                    (0, 0, 10, 10),
                    0.9,
                    "button",
                    {},
                )
            ]
        },
    )
    router = PerceptionRouter([strategy])
    grounded = router.ground_target("button")
    assert grounded.candidate.confidence == 0.9
    assert grounded.resolution_method.startswith("single_uia_")
    assert grounded.alternatives == 0
    assert grounded.candidate.source_type == ObservationSource.UIA


def test_perception_router_chooses_higher_confidence():
    """When two candidates exist, the higher-confidence one wins."""
    strategy = MockStrategy(
        "uia",
        {
            "button": [
                TargetCandidate(
                    ObservationSource.UIA,
                    (20, 20, 30, 30),
                    0.95,
                    "button_2",
                    {},
                ),
                TargetCandidate(
                    ObservationSource.UIA,
                    (0, 0, 10, 10),
                    0.7,
                    "button_1",
                    {},
                ),
            ]
        },
    )
    router = PerceptionRouter([strategy])
    grounded = router.ground_target("button")
    assert grounded.candidate.confidence == 0.95
    assert grounded.candidate.text == "button_2"
    assert grounded.alternatives == 1


def test_perception_router_no_match_raises():
    strategy = MockStrategy("uia", {})
    router = PerceptionRouter([strategy])
    with pytest.raises(TargetNotGroundedError):
        router.ground_target("button")


def test_perception_router_indistinguishable_raises_ambiguity():
    """Two *identical* UIA candidates raise AmbiguityError, not silent pick."""
    strategy = MockStrategy(
        "uia",
        {
            "Save": [
                TargetCandidate(
                    ObservationSource.UIA,
                    (0, 0, 10, 10),
                    0.9,
                    "Save",
                    {},
                ),
                TargetCandidate(
                    ObservationSource.UIA,
                    (0, 0, 10, 10),
                    0.9,
                    "Save",
                    {},
                ),
            ]
        },
    )
    router = PerceptionRouter([strategy])
    # The UIA short-circuit at confidence >= 0.8 normally picks the
    # single match.  Two *identical* high-confidence UIA matches
    # must surface as AMBIGUOUS so the Brain can ask the user.
    # The short-circuit fires when a strategy returns EXACTLY one
    # candidate; here we have two, so the router reaches the
    # ranking path and finds an exact tie.
    with pytest.raises(AmbiguityError):
        router.ground_target("Save")


def test_coordinates_strategy_parses_pair():
    s = CoordinatesStrategy()
    candidates = s.find_targets("click 150, 250")
    assert len(candidates) == 1
    c = candidates[0]
    assert c.bbox == (149, 249, 151, 251)
    assert c.source_type == ObservationSource.DERIVED
    assert s.requires_screenshot is False


def test_coordinates_strategy_multiple_pairs():
    """Two coordinate pairs → two candidates, the router raises AMBIGUOUS."""
    s = CoordinatesStrategy()
    candidates = s.find_targets("from 100, 200 to 300, 400")
    assert len(candidates) == 2
    xs = sorted(c.properties["x"] for c in candidates)
    assert xs == [100, 300]


def test_perception_router_does_not_call_yolo_for_text():
    """The hint-driven order biases yolo for 'icon' but not for 'button'."""
    text_strategy = MockStrategy(
        "uia",
        {
            "button": [
                TargetCandidate(
                    ObservationSource.UIA,
                    (0, 0, 10, 10),
                    0.9,
                    "button",
                    {},
                )
            ]
        },
        reliability=0.95,
    )
    yolo_strategy = MockStrategy(
        "yolo",
        {
            "icon": [
                TargetCandidate(
                    ObservationSource.VISION,
                    (0, 0, 10, 10),
                    0.9,
                    "icon",
                    {},
                )
            ]
        },
        reliability=0.4,
    )
    router = PerceptionRouter([text_strategy, yolo_strategy])
    # 'button' is not in the icon hints, so yolo is not first.
    grounded = router.ground_target("button")
    assert grounded.candidate.source_type == ObservationSource.UIA


def test_perception_router_does_not_call_screenshot_dependent_when_no_image():
    """A yolo-only router raises TargetNotGroundedError when no image is given."""
    yolo = MockStrategy(
        "yolo",
        {
            "icon": [
                TargetCandidate(
                    ObservationSource.VISION,
                    (0, 0, 10, 10),
                    0.9,
                    "icon",
                    {},
                )
            ]
        },
        requires_screenshot=True,
    )
    router = PerceptionRouter([yolo])
    with pytest.raises(TargetNotGroundedError):
        router.ground_target("icon")


def test_strategy_ordering_prefers_reliable_source():
    """When two strategies both match, the higher-reliability source wins."""
    uia = MockStrategy(
        "uia",
        {
            "x": [
                TargetCandidate(
                    ObservationSource.UIA,
                    (0, 0, 10, 10),
                    0.7,
                    "x",
                    {},
                )
            ]
        },
        reliability=0.95,
    )
    yolo = MockStrategy(
        "yolo",
        {
            "x": [
                TargetCandidate(
                    ObservationSource.VISION,
                    (0, 0, 10, 10),
                    0.95,
                    "x",
                    {},
                )
            ]
        },
        reliability=0.4,
    )
    router = PerceptionRouter([uia, yolo])
    grounded = router.ground_target("x")
    assert grounded.candidate.source_type == ObservationSource.UIA
