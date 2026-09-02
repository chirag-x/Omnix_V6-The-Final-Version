#!/usr/bin/env python3

import sys
sys.path.insert(0, 'E:\\Coding\\Omnix\\Omnix_V6- The final version')

from core.grounding.perception_bridge import PerceptionToGroundingBridge
from core.grounding.target_resolver import TargetResolver
from vision.observations.targets import TargetCandidate
from vision.router.perception_router import PerceptionRouter
from core.orchestration.models import ObservationSource
from vision.grounded_element import GroundedElement, GroundedElementStatus
import time
import uuid

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

router = RouterWithStrategies([
    MockStrategy("vision", ObservationSource.VISION, vision_candidate),
    MockStrategy("ocr", ObservationSource.OCR, ocr_candidate),
    MockStrategy("uia", ObservationSource.UIA, uia_candidate),
])

resolver = TargetResolver()
bridge = PerceptionToGroundingBridge(router=router, resolver=resolver)

result = bridge.ground_target("Button")

print(f"Result status: {result.status}")
print(f"Result reason: {result.reason}")
if result.target:
    print(f"Result target source: {result.target.source}")
    print(f"Result target text: {result.target.identifier}")
    print(f"Result target confidence: {result.target.confidence}")
else:
    print("No target")