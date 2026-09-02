#!/usr/bin/env python3

import sys
sys.path.insert(0, 'E:\\Coding\\Omnix\\Omnix_V6- The final version')

from core.grounding.perception_bridge import PerceptionToGroundingBridge
from core.grounding.target_resolver import TargetResolver
from vision.observations.targets import TargetCandidate
from vision.router.perception_router import PerceptionRouter
from core.orchestration.models import ObservationSource
import time
from unittest.mock import Mock

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

# Setup exactly like the test
router = MockPerceptionRouter()
resolver = TargetResolver()
bridge = PerceptionToGroundingBridge(router=router, resolver=resolver)

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

# Set up router to return MULTIPLE_TARGETS status to force deterministic matching
router.will_raise_ambiguity = False
router.will_raise_not_found = False
router.candidates = [vision_candidate, ocr_candidate, uia_candidate]  # vision, ocr, uia order

# Mock the grounded element to indicate multiple targets (this triggers _get_all_candidates_and_match)
original_from_target_candidate = MockGroundedElement.from_target_candidate
def mock_from_target_candidate(candidate):
    element = original_from_target_candidate(candidate)
    # Set alternatives in properties, not as attribute
    element.properties["alternatives"] = len(router.candidates) - 1  # Indicate we have alternatives
    # Set status to MULTIPLE_TARGETS to trigger the multiple-targets branch
    element.status = type('MockStatus', (), {'value': 'MULTIPLE_TARGETS'})()
    return element
MockGroundedElement.from_target_candidate = mock_from_target_candidate

print("Testing source_type values:")
print(f"UIA candidate source_type: {uia_candidate.source_type}")
print(f"UIA candidate source_type.value: {uia_candidate.source_type.value}")
print(f"OCR candidate source_type: {ocr_candidate.source_type}")
print(f"OCR candidate source_type.value: {ocr_candidate.source_type.value}")
print(f"VISION candidate source_type: {vision_candidate.source_type}")
print(f"VISION candidate source_type.value: {vision_candidate.source_type.value}")

print("\nTesting _score_candidate_match:")
normalized_query = bridge._normalize_text("Button")
print(f"Normalized query: '{normalized_query}'")

for i, candidate in enumerate([vision_candidate, ocr_candidate, uia_candidate]):
    score = bridge._score_candidate_match(candidate, normalized_query)
    print(f"Candidate {i} ({candidate.source_type.value}): score = {score}")

print("\nTesting full bridge:")
result = bridge.ground_target("Button")
print(f"Result status: {result.status}")
print(f"Result reason: {result.reason}")
if result.target:
    print(f"Result target source: {result.target.source}")
    print(f"Result target text: {result.target.identifier}")
else:
    print("No target")

# Restore original method
MockGroundedElement.from_target_candidate = original_from_target_candidate