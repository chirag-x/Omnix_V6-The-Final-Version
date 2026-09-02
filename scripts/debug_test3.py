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
        # Return MULTIPLE_TARGETS to trigger deterministic matching
        from vision.grounded_element import GroundedElement
        from vision.observations.targets import GroundedTarget
        # Pick the first candidate as primary
        primary = self.strategies[-1].candidate  # uia candidate
        return GroundedTarget(
            candidate=primary,
            resolution_method="multiple_targets",
            alternatives=len(self.strategies) - 1
        )

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
else:
    print("No target")