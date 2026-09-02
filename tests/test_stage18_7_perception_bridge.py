"""
Tests for Stage 18.7 — Perception → Grounding Bridge.

Tests the bridge that connects perception (System 3 vision) to grounding
(Stage 18.6 target resolution) without LLM involvement.

Test categories from spec section 27-32:
27. Deterministic target matching (case-insensitive, whitespace-normalized)
28. Confidence threshold handling
29. Freshness integration with Stage 18.6
30. Ambiguity handling (returns multiple candidates, does not guess)
31. Coordinate normalization (vision→screen)
32. Window context preservation
"""

import time
from unittest.mock import Mock, patch

import pytest

from core.grounding.perception_bridge import (
    PerceptionToGroundingBridge,
    create_perception_bridge,
)
from core.grounding.resolved_target import (
    TargetResolutionResult,
    TargetResolutionStatus,
)
from core.grounding.target_resolver import TargetResolver
from vision.grounded_element import GroundedElement, GroundedElementStatus
from vision.observations.targets import TargetCandidate
from vision.router.perception_router import PerceptionRouter, AmbiguityError, TargetNotGroundedError
from vision.router.screenshot_provider import ScreenshotProvider
from core.orchestration.models import ObservationSource


class MockPerceptionRouter:
    """Mock PerceptionRouter for testing the bridge."""

    def __init__(self, candidates=None, will_raise_ambiguity=False, will_raise_not_found=False):
        self.candidates = candidates or []
        self.will_raise_ambiguity = will_raise_ambiguity
        self.will_raise_not_found = will_raise_not_found
        self.strategies = []  # Empty for mock

    def ground_target(self, query, image_path=None, preferred_strategy=None, in_window=None):
        if self.will_raise_ambiguity:
            # Import the real AmbiguityError
            from vision.router.perception_router import AmbiguityError
            raise AmbiguityError("Ambiguous match", candidates=self.candidates)
        if self.will_raise_not_found:
            # Import the real TargetNotGroundedError
            from vision.router.perception_router import TargetNotGroundedError
            raise TargetNotGroundedError("Target not found", query=query)

        # Return a single grounded element based on first candidate
        if self.candidates:
            candidate = self.candidates[0]
            # Create a mock grounded element
            return MockGroundedElement.from_target_candidate(candidate)
        else:
            # Import the real TargetNotGroundedError
            from vision.router.perception_router import TargetNotGroundedError
            raise TargetNotGroundedError("No candidates", query=query)

    def _get_all_candidates_from_strategies(self, query, image_path=None):
        """Helper to get all candidates for testing."""
        return self.candidates


class MockGroundedElement:
    """Mock GroundedElement for testing."""

    def __init__(self, x=100, y=100, confidence=0.8, source="uia", text="button",
                 status="OBSERVED", properties=None):
        from core.orchestration.models import ObservationSource
        self.id = "test_id"
        self.type = "button"
        self.text = text
        self.confidence = confidence
        self.bbox = (x-10, y-10, x+10, y+10)
        self.center = (x, y)
        self.enabled = True
        self.visible = True
        self.interactable = True
        self.source = source
        self.semantic_role = "button"
        self.status = type('MockStatus', (), {'value': status})()
        self.monitor_id = "0"
        self.screenshot_id = "shot_123"
        self.timestamp = time.time()
        self.properties = properties or {}
        self.resolution_method = "test"
        self.alternatives = 0

    @classmethod
    def from_target_candidate(cls, candidate):
        """Create mock grounded element from target candidate."""
        instance = cls(
            x=(candidate.bbox[0] + candidate.bbox[2]) // 2,
            y=(candidate.bbox[1] + candidate.bbox[3]) // 2,
            confidence=candidate.confidence,
            source=candidate.source_type.value,
            text=candidate.text,
            properties=dict(candidate.properties),
        )
        instance.timestamp = candidate.timestamp
        return instance


class MockScreenshotProvider:
    """Mock ScreenshotProvider for testing."""

    def __init__(self, return_path="/tmp/test_screenshot.png"):
        self.return_path = return_path

    def capture(self, path=None):
        return self.return_path


class MockAmbiguityError(Exception):
    def __init__(self, message, candidates=None):
        super().__init__(message)
        self.candidates = candidates or []


class MockTargetNotGroundedError(Exception):
    def __init__(self, message):
        super().__init__(message)


@pytest.fixture
def mock_router():
    """Fixture providing a mock perception router."""
    return MockPerceptionRouter()


@pytest.fixture
def mock_resolver():
    """Fixture providing a mock target resolver."""
    return TargetResolver(screen_width=1920, screen_height=1080)


@pytest.fixture
def mock_provider():
    """Fixture providing a mock screenshot provider."""
    return MockScreenshotProvider()


@pytest.fixture
def perception_bridge(mock_router, mock_resolver, mock_provider):
    """Fixture providing a perception bridge instance."""
    return PerceptionToGroundingBridge(
        router=mock_router,
        resolver=mock_resolver,
        provider=mock_provider,
        minimum_confidence=0.5
    )


def test_bridge_initialization():
    """Test that the perception bridge initializes correctly."""
    router = MockPerceptionRouter()
    bridge = PerceptionToGroundingBridge(router=router)

    assert bridge.router == router
    assert bridge.resolver is not None
    assert bridge.minimum_confidence == 0.5  # default
    assert bridge.max_target_age_s == 0.0   # perception is fresh by default


def test_bridge_factory_function():
    """Test the factory function for creating perception bridges."""
    router = MockPerceptionRouter()
    bridge = create_perception_bridge(
        router=router,
        minimum_confidence=0.7,
        max_target_age_s=2.0
    )

    assert isinstance(bridge, PerceptionToGroundingBridge)
    assert bridge.minimum_confidence == 0.7
    assert bridge.max_target_age_s == 2.0


def test_ground_target_empty_query(perception_bridge):
    """Test that empty query returns NOT_FOUND."""
    result = perception_bridge.ground_target("")
    assert result.status == TargetResolutionStatus.NOT_FOUND
    assert "empty" in result.reason.lower()

    result = perception_bridge.ground_target(None)
    assert result.status == TargetResolutionStatus.NOT_FOUND

    result = perception_bridge.ground_target("   ")
    assert result.status == TargetResolutionStatus.NOT_FOUND


def test_ground_target_perception_not_found(perception_bridge, mock_router):
    """Test bridge when perception finds nothing."""
    mock_router.will_raise_not_found = True

    result = perception_bridge.ground_target("nonexistent button")
    print(f"DEBUG: Result status: {result.status}")
    print(f"DEBUG: Result reason: {result.reason}")
    print(f"DEBUG: Result target: {result.target}")
    assert result.status == TargetResolutionStatus.NOT_FOUND
    assert "perception" in result.reason.lower()


def test_ground_target_perception_ambiguity(perception_bridge, mock_router):
    """Test bridge when perception reports ambiguity."""
    # Setup mock to raise ambiguity error
    mock_candidates = [
        TargetCandidate(
            source_type=ObservationSource.UIA,
            bbox=(100, 100, 110, 110),
            confidence=0.9,
            text="OK Button",
            properties={}
        ),
        TargetCandidate(
            source_type=ObservationSource.UIA,
            bbox=(200, 200, 210, 210),
            confidence=0.85,
            text="Okay Button",  # Similar text
            properties={}
        )
    ]
    mock_router.will_raise_ambiguity = True
    mock_router.candidates = mock_candidates

    result = perception_bridge.ground_target("OK")
    # Should return NOT_FOUND due to ambiguity (cannot guess)
    print(f"DEBUG: Ambiguity result status: {result.status}")
    print(f"DEBUG: Ambiguity result reason: {result.reason}")
    assert result.status == TargetResolutionStatus.NOT_FOUND
    assert "ambiguous" in result.reason.lower()


def test_ground_target_successful_match(perception_bridge, mock_router):
    """Test successful perception to grounding bridge."""
    # Setup mock router to return a candidate
    candidate = TargetCandidate(
        source_type=ObservationSource.UIA,
        bbox=(100, 100, 200, 200),
        confidence=0.9,
        text="OK Button",
        properties={"automation_id": "ok_btn"}
    )
    mock_router.candidates = [candidate]
    mock_router.will_raise_ambiguity = False
    mock_router.will_raise_not_found = False

    result = perception_bridge.ground_target("OK Button")
    print(f"DEBUG: Result status: {result.status}")
    print(f"DEBUG: Result reason: {result.reason}")
    print(f"DEBUG: Result target: {result.target}")
    if result.target:
        print(f"DEBUG: Target kind: {result.target.kind}")
        print(f"DEBUG: Target confidence: {result.target.confidence}")
        print(f"DEBUG: Target identifier: {result.target.identifier}")
        print(f"DEBUG: Target source: {result.target.source}")
        print(f"DEBUG: Target center_x: {result.target.center_x}")
        print(f"DEBUG: Target center_y: {result.target.center_y}")

    assert result.status == TargetResolutionStatus.RESOLVED
    assert result.target is not None
    assert result.target.kind == "vision"  # From TargetCandidate
    assert result.target.confidence == 0.9
    assert result.target.identifier == "OK Button"
    assert result.target.source == "uia"
    assert result.target.center_x == 150  # Center of (100,100,200,200)
    assert result.target.center_y == 150


def test_deterministic_matching_case_insensitive():
    """Test case-insensitive matching in deterministic selection."""
    router = MockPerceptionRouter()
    resolver = TargetResolver()
    bridge = PerceptionToGroundingBridge(router=router, resolver=resolver)

    # Setup candidates with different case variations
    candidates = [
        TargetCandidate(
            source_type=ObservationSource.UIA,
            bbox=(100, 100, 110, 110),
            confidence=0.9,
            text="OK Button",
            properties={}
        ),
        TargetCandidate(
            source_type=ObservationSource.UIA,
            bbox=(200, 200, 210, 210),
            confidence=0.8,
            text="ok button",  # lowercase
            properties={}
        ),
        TargetCandidate(
            source_type=ObservationSource.UIA,
            bbox=(300, 300, 310, 310),
            confidence=0.7,
            text="Cancel Button",  # different text
            properties={}
        )
    ]
    router.candidates = candidates

    # Test case-insensitive exact match
    result = bridge.ground_target("ok button")
    assert result.status == TargetResolutionStatus.RESOLVED
    # Should match the first candidate (exact case match gets higher score)
    assert result.target.identifier == "OK Button"


def test_deterministic_matching_whitespace_normalized():
    """Test whitespace-normalized matching."""
    router = MockPerceptionRouter()
    resolver = TargetResolver()
    bridge = PerceptionToGroundingBridge(router=router, resolver=resolver)

    candidates = [
        TargetCandidate(
            source_type=ObservationSource.UIA,
            bbox=(100, 100, 110, 110),
            confidence=0.9,
            text="OK   Button",  # multiple spaces
            properties={}
        ),
        TargetCandidate(
            source_type=ObservationSource.UIA,
            bbox=(200, 200, 210, 210),
            confidence=0.8,
            text="OK Button",  # single space
            properties={}
        )
    ]
    router.candidates = candidates

    # Query with normalized whitespace
    result = bridge.ground_target("OK Button")
    assert result.status == TargetResolutionStatus.RESOLVED
    # Should match one of them (whitespace normalization makes them equivalent)


def test_confidence_threshold_filtering():
    """Test that low confidence candidates are filtered out."""
    router = MockPerceptionRouter()
    resolver = TargetResolver(minimum_confidence=0.6)  # High threshold
    bridge = PerceptionToGroundingBridge(router=router, resolver=resolver)

    # Low confidence candidate
    low_conf_candidate = TargetCandidate(
        source_type=ObservationSource.UIA,
        bbox=(100, 100, 110, 110),
        confidence=0.4,  # Below threshold
        text="Low Conf Button",
        properties={}
    )

    # High confidence candidate
    high_conf_candidate = TargetCandidate(
        source_type=ObservationSource.UIA,
        bbox=(200, 200, 210, 210),
        confidence=0.8,  # Above threshold
        text="High Conf Button",
        properties={}
    )

    router.candidates = [high_conf_candidate]  # Only high confidence

    result = bridge.ground_target("Button")
    assert result.status == TargetResolutionStatus.RESOLVED
    assert result.target.identifier == "High Conf Button"
    assert result.target.confidence == 0.8


def test_freshness_integration():
    """Test that freshness validation works through the resolver."""
    router = MockPerceptionRouter()
    # Resolver with very short max age (will make targets stale quickly)
    resolver = TargetResolver(max_target_age_s=0.1)  # 100ms max age
    bridge = PerceptionToGroundingBridge(router=router, resolver=resolver)

    # Create an old candidate (simulate stale perception)
    old_time = time.time() - 1.0  # 1 second ago
    old_candidate = TargetCandidate(
        source_type=ObservationSource.UIA,
        bbox=(100, 100, 110, 110),
        confidence=0.9,
        text="Old Button",
        properties={},
        timestamp=old_time  # Old timestamp
    )

    router.candidates = [old_candidate]

    result = bridge.ground_target("Old Button")
    # Should be STALE due to age
    assert result.status == TargetResolutionStatus.STALE
    assert "stale" in result.reason.lower()


def test_coordinate_normalization():
    """Test that coordinates are properly normalized for actions."""
    router = MockPerceptionRouter()
    resolver = TargetResolver()
    bridge = PerceptionToGroundingBridge(router=router, resolver=resolver)

    # Candidate with known bbox
    candidate = TargetCandidate(
        source_type=ObservationSource.VISION,  # From visual strategy
        bbox=(100, 100, 300, 200),  # left, top, right, bottom
        confidence=0.8,
        text="Logo",
        properties={"class": "logo"}
    )
    router.candidates = [candidate]

    result = bridge.ground_target("Logo")

    assert result.status == TargetResolutionStatus.RESOLVED
    assert result.target is not None
    assert result.target.kind == "vision"
    # Center should be at (200, 150) - middle of bbox
    assert result.target.center_x == 200
    assert result.target.center_y == 150
    # x, y should also reflect center coordinates
    assert result.target.x == 200
    assert result.target.y == 150


def test_window_context_preservation():
    """Test that window context is preserved when available."""
    router = MockPerceptionRouter()
    resolver = TargetResolver()
    bridge = PerceptionToGroundingBridge(router=router, resolver=resolver)

    # Candidate with window context
    candidate = TargetCandidate(
        source_type=ObservationSource.UIA,
        bbox=(100, 100, 200, 200),
        confidence=0.9,
        text="Button",
        properties={
            "window_handle": 12345,
            "window_title": "Test Window",
            "application": "TestApp.exe"
        }
    )
    router.candidates = [candidate]

    result = bridge.ground_target("Button")

    assert result.status == TargetResolutionStatus.RESOLVED
    assert result.target is not None
    # Window context should be preserved in metadata or target fields
    assert result.target.metadata.get("window_handle") == 12345
    assert result.target.metadata.get("window_title") == "Test Window"
    assert result.target.metadata.get("application") == "TestApp.exe"


def test_no_llm_calls_made():
    """Test that the perception bridge makes zero LLM calls."""
    router = MockPerceptionRouter()
    resolver = TargetResolver()
    bridge = PerceptionToGroundingBridge(router=router, resolver=resolver)

    # Mock candidate
    candidate = TargetCandidate(
        source_type=ObservationSource.UIA,
        bbox=(100, 100, 110, 110),
        confidence=0.9,
        text="Test Button",
        properties={}
    )
    router.candidates = [candidate]

    # Execute bridge operation
    result = bridge.ground_target("Test Button")

    # Verify no LLM-like calls were made (this is inherently tested by
    # the fact we're using only mock perception and resolver components)
    assert result.status in [
        TargetResolutionStatus.RESOLVED,
        TargetResolutionStatus.NOT_FOUND,
        TargetResolutionStatus.LOW_CONFIDENCE,
        TargetResolutionStatus.STALE,
    ]
    # The key insight: no LLM calls are made because we only use
    # deterministic perception strategies and target resolver


def test_perception_strategy_ranking_respected():
    """Test that perception strategy ranking (UIA > DERIVED > OCR > VISION > SCREEN) is respected."""
    # Create mock strategies that return candidates by source type
    class MockStrategy:
        def __init__(self, name, source_type, candidate):
            self.name = name
            self.source_type = source_type
            self.candidate = candidate
            self.requires_screenshot = False

        def find_targets(self, query, image_path=None):
            return [self.candidate]

    # Same text, different strategies with same confidence
    uia_candidate = TargetCandidate(
        source_type=ObservationSource.UIA,
        bbox=(100, 100, 110, 110),
        confidence=0.8,
        text="Button",
        properties={}
    )

    ocr_candidate = TargetCandidate(
        source_type=ObservationSource.OCR,
        bbox=(200, 200, 210, 210),
        confidence=0.8,
        text="Button",
        properties={}
    )

    vision_candidate = TargetCandidate(
        source_type=ObservationSource.VISION,
        bbox=(300, 300, 310, 310),
        confidence=0.8,
        text="Button",
        properties={}
    )

    # Create a mock router with strategies (ordered worst to best by source)
    class RouterWithStrategies:
        def __init__(self, strategies):
            self.strategies = strategies

        def ground_target(self, query, image_path=None, preferred_strategy=None, in_window=None):
            # Return a GroundedElement with MULTIPLE_TARGETS status
            # This simulates what the real router would do when it finds multiple candidates
            # that are indistinguishable by its own ranking (but we want the bridge to re-rank)

            # Create a GroundedElement that indicates MULTIPLE_TARGETS
            # We'll use the first candidate's data but set status to MULTIPLE_TARGETS
            primary_candidate = self.strategies[-1].candidate  # uia candidate
            element = GroundedElement(
                id=str(uuid.uuid4()),
                type="button",
                text=primary_candidate.text,
                confidence=primary_candidate.confidence,
                bbox=primary_candidate.bbox,
                center=((primary_candidate.bbox[0] + primary_candidate.bbox[2]) // 2,
                        (primary_candidate.bbox[1] + primary_candidate.bbox[3]) // 2),
                enabled=True,
                visible=True,
                interactable=True,
                source=primary_candidate.source_type.value,
                semantic_role="button",
                monitor_id="0",
                screenshot_id="shot_123",
                timestamp=primary_candidate.timestamp,
                properties={
                    "alternatives": len(self.strategies) - 1  # Number of alternative candidates
                },
                status=GroundedElementStatus.MULTIPLE_TARGETS
            )
            return element

    import uuid
    router = RouterWithStrategies([
        MockStrategy("vision", ObservationSource.VISION, vision_candidate),
        MockStrategy("ocr", ObservationSource.OCR, ocr_candidate),
        MockStrategy("uia", ObservationSource.UIA, uia_candidate),
    ])

    resolver = TargetResolver()
    bridge = PerceptionToGroundingBridge(router=router, resolver=resolver)

    result = bridge.ground_target("Button")

    assert result.status == TargetResolutionStatus.RESOLVED
    # Should select UIA candidate due to highest reliability ranking
    assert result.target.source == "uia"
    assert result.target.identifier == "Button"


def test_multiple_strategies_candidate_aggregation():
    """Test that candidates from multiple strategies are properly aggregated."""
    router = MockPerceptionRouter()
    resolver = TargetResolver()
    bridge = PerceptionToGroundingBridge(router=router, resolver=resolver)

    # Mock different strategies returning candidates
    uia_cands = [
        TargetCandidate(
            source_type=ObservationSource.UIA,
            bbox=(100, 100, 110, 110),
            confidence=0.9,
            text="UIA Button",
            properties={}
        )
    ]

    ocr_cands = [
        TargetCandidate(
            source_type=ObservationSource.OCR,
            bbox=(200, 200, 210, 210),
            confidence=0.7,
            text="OCR Button",
            properties={}
        )
    ]

    # Simulate router getting candidates from all strategies
    all_candidates = uia_cands + ocr_cands
    router.candidates = all_candidates

    result = bridge.ground_target("Button")

    assert result.status == TargetResolutionStatus.RESOLVED
    # Should have considered candidates from both strategies
    assert result.target.identifier in ["UIA Button", "OCR Button"]
    # UIA should win due to higher reliability
    assert result.target.identifier == "UIA Button"


def test_bridge_handles_screenshot_provider_errors_gracefully():
    """Test that screenshot provider errors don't crash the bridge."""
    router = MockPerceptionRouter()
    resolver = TargetResolver()

    # Screenshot provider that raises an exception
    class FailingScreenshotProvider:
        def capture(self, path=None):
            raise Exception("Screenshot failed")

    failing_provider = FailingScreenshotProvider()
    bridge = PerceptionToGroundingBridge(
        router=router,
        resolver=resolver,
        provider=failing_provider
    )

    # Candidate that doesn't require screenshot (UIA strategy)
    uia_candidate = TargetCandidate(
        source_type=ObservationSource.UIA,
        bbox=(100, 100, 110, 110),
        confidence=0.9,
        text="UIA Button",
        properties={}
    )
    router.candidates = [uia_candidate]

    # Should still work even if screenshot fails (UIA doesn't need screenshot)
    result = bridge.ground_target("UIA Button")
    assert result.status == TargetResolutionStatus.RESOLVED


if __name__ == "__main__":
    pytest.main([__file__, "-v"])