"""
Tests for VisionService (Phase 7.1).

These tests are deterministic and do NOT touch the real screen.
They cover:

  * the new constructor (ScreenshotProvider, not OmnixEngine)
  * lazy screenshot acquisition (no screenshot for UIA/coords)
  * observation status is OBSERVED, never verified
  * diff_observations classifies state changes correctly
  * router's AmbiguityError is surfaced as AMBIGUOUS
"""
from __future__ import annotations

from typing import Any, List, Optional
from unittest.mock import Mock

import pytest

from core.orchestration.models import ObservationSource
from core.services.vision_service import VisionResult, VisionService
from vision.observations.targets import TargetCandidate
from vision.router.perception_router import (
    AmbiguityError,
    PerceptionRouter,
    TargetNotGroundedError,
)
from vision.router.perception_strategy import PerceptionStrategy
from vision.router.screenshot_provider import (
    NullScreenshotProvider,
    ScreenshotProvider,
)


class FakeStrategy(PerceptionStrategy):
    """A deterministic strategy we can drive from tests."""

    def __init__(
        self,
        name: str,
        requires_screenshot: bool = False,
        reliability: float = 0.5,
        candidates_by_query: Optional[dict] = None,
    ) -> None:
        self._name = name
        self._requires_screenshot = requires_screenshot
        self._reliability = reliability
        self._candidates = candidates_by_query or {}
        self.calls: List[dict] = []

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
        self.calls.append(
            {"query": target_query, "image_path": image_path}
        )
        return list(self._candidates.get(target_query, []))


class _RecordingProvider(ScreenshotProvider):
    """A provider that records every call and returns a fake path."""

    name = "recording"

    def __init__(self) -> None:
        self.calls: List[Optional[str]] = []

    def capture(self, *, path: Optional[str] = None) -> Optional[str]:
        self.calls.append(path)
        return "/tmp/fake.png"


# --------------------------------------------------------------- constructor

def test_vision_service_accepts_screenshot_provider():
    """VisionService takes a ScreenshotProvider, NOT an OmnixEngine."""
    vs = VisionService(NullScreenshotProvider())
    assert vs is not None


def test_vision_service_default_strategies():
    """Default strategies are UIA, OCR, Visual, Coordinates."""
    vs = VisionService(NullScreenshotProvider())
    names = sorted(s.name for s in vs._router.strategies)
    assert names == ["coordinates", "ocr", "uia", "yolo"]


# ---------------------------------------------------- lazy screenshot

def test_no_screenshot_for_uia_only_query():
    """If only UIA needs it, the provider is NOT called."""
    provider = _RecordingProvider()
    cand = TargetCandidate(
        ObservationSource.UIA, (0, 0, 10, 10), 0.9, "my button", {}
    )
    uia = FakeStrategy(
        "uia",
        requires_screenshot=False,
        reliability=0.95,
        candidates_by_query={"my button": [cand]},
    )
    vs = VisionService(provider, strategies=[uia])
    result = vs.ground_target("my button")
    assert result.status == "OBSERVED"
    # Screenshot provider was never invoked.
    assert provider.calls == []


def test_screenshot_acquired_only_when_needed():
    """If at least one strategy requires a screenshot, the provider IS called."""
    provider = _RecordingProvider()
    uia = FakeStrategy("uia", requires_screenshot=False, reliability=0.95)
    yolo = FakeStrategy(
        "yolo",
        requires_screenshot=True,
        reliability=0.4,
        candidates_by_query={
            "cup": [
                TargetCandidate(
                    source_type=ObservationSource.VISION,
                    bbox=(1, 1, 10, 10),
                    confidence=0.7,
                    text="cup",
                    properties={},
                )
            ]
        },
    )
    vs = VisionService(provider, strategies=[uia, yolo])
    # Force the yolo path by using a query that includes the yolo hint.
    result = vs.ground_target("an icon on the cup")
    # The router prefers yolo when 'icon' is in the query.
    assert provider.calls, "screenshot should have been acquired"
    assert result.screenshot_used is True


def test_no_screenshot_for_coordinates_only_query():
    """Coordinates don't need a screenshot — the provider is NOT called."""
    provider = _RecordingProvider()
    coords = FakeStrategy(
        "coordinates",
        requires_screenshot=False,
        reliability=0.9,
        candidates_by_query={
            "100, 200": [
                TargetCandidate(
                    source_type=ObservationSource.DERIVED,
                    bbox=(99, 199, 101, 201),
                    confidence=1.0,
                    text="coordinates: 100, 200",
                    properties={"x": 100, "y": 200},
                )
            ]
        },
    )
    vs = VisionService(provider, strategies=[coords])
    result = vs.ground_target("100, 200")
    assert result.status == "OBSERVED"
    assert provider.calls == []


# ---------------------------------------------------- observation semantics

def test_observation_never_claims_verified():
    """VisionService MUST NOT claim 'verified' on a single observation."""
    provider = _RecordingProvider()
    coords = FakeStrategy(
        "coordinates",
        requires_screenshot=False,
        reliability=0.9,
        candidates_by_query={
            "x": [
                TargetCandidate(
                    source_type=ObservationSource.DERIVED,
                    bbox=(0, 0, 2, 2),
                    confidence=1.0,
                    text="x",
                    properties={},
                )
            ]
        },
    )
    vs = VisionService(provider, strategies=[coords])
    result = vs.ground_target("x")
    assert result.status == "OBSERVED"
    # The result must NOT carry a 'verified' field.
    assert not hasattr(result, "verified")
    # And it must not be a VerificationVerdict-shaped object.
    assert not isinstance(result, bool)


def test_ambiguity_surfaced_as_status():
    """When the router raises AmbiguityError, status=AMBIGUOUS."""
    provider = _RecordingProvider()
    uia = FakeStrategy(
        "uia",
        requires_screenshot=False,
        reliability=0.95,
        candidates_by_query={
            "Save": [
                TargetCandidate(
                    source_type=ObservationSource.UIA,
                    bbox=(0, 0, 10, 10),
                    confidence=0.9,
                    text="Save",
                    properties={},
                ),
                TargetCandidate(
                    source_type=ObservationSource.UIA,
                    bbox=(0, 0, 10, 10),
                    confidence=0.9,
                    text="Save",
                    properties={},
                ),
            ]
        },
    )
    vs = VisionService(provider, strategies=[uia])
    result = vs.ground_target("Save")
    assert result.status == "AMBIGUOUS"
    assert result.observation is not None
    assert "candidates" in result.observation


def test_not_found_when_no_strategy_produces_candidates():
    provider = _RecordingProvider()
    uia = FakeStrategy("uia", requires_screenshot=False, reliability=0.95)
    vs = VisionService(provider, strategies=[uia])
    result = vs.ground_target("nothing-matches")
    assert result.status == "NOT_FOUND"
    assert result.error is not None


# ------------------------------------------------------- diff_observations

def test_diff_target_appeared():
    provider = _RecordingProvider()
    vs = VisionService(provider)
    before = VisionResult(status="NOT_FOUND", target_query="x")
    after = VisionResult(
        status="OBSERVED",
        target_query="x",
        observation={"source": "uia", "bbox": (0, 0, 2, 2)},
    )
    diff = vs.diff_observations(before, after)
    assert diff["changed"] is True
    assert diff["reason"] == "target appeared"


def test_diff_target_disappeared():
    provider = _RecordingProvider()
    vs = VisionService(provider)
    before = VisionResult(
        status="OBSERVED",
        target_query="x",
        observation={"source": "uia", "bbox": (0, 0, 2, 2)},
    )
    after = VisionResult(status="NOT_FOUND", target_query="x")
    diff = vs.diff_observations(before, after)
    assert diff["changed"] is True
    assert diff["reason"] == "target disappeared"


def test_diff_no_change():
    provider = _RecordingProvider()
    vs = VisionService(provider)
    same = VisionResult(
        status="OBSERVED",
        target_query="x",
        observation={"source": "uia", "bbox": (0, 0, 2, 2)},
    )
    diff = vs.diff_observations(same, same)
    assert diff["changed"] is False


def test_diff_missing_observation():
    provider = _RecordingProvider()
    vs = VisionService(provider)
    diff = vs.diff_observations(None, None)
    assert diff["changed"] is None


# ----------------------------------------------------- observe_state hook

def test_observe_state_not_verified():
    """observe_state returns OBSERVATION, never 'verified'."""
    provider = _RecordingProvider()
    coords = FakeStrategy(
        "coordinates",
        requires_screenshot=False,
        reliability=0.9,
        candidates_by_query={
            "x": [
                TargetCandidate(
                    source_type=ObservationSource.DERIVED,
                    bbox=(0, 0, 2, 2),
                    confidence=1.0,
                    text="x",
                    properties={},
                )
            ]
        },
    )
    vs = VisionService(provider, strategies=[coords])
    result = vs.observe_state("x")
    assert result.status == "OBSERVED"
    # The result is observation, not verification.
    assert "verified" not in (result.observation or {})
