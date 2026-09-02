"""
Target bounding boxes and matched candidates for Phase 7 perception.
"""
import time
from dataclasses import dataclass, field
from typing import Dict, Any, Optional, Tuple, List
from core.orchestration.models import ObservationSource

@dataclass(frozen=True)
class TargetCandidate:
    """A single matched candidate found by a strategy."""
    source_type: ObservationSource  # e.g., ObservationSource.UIA
    bbox: Tuple[int, int, int, int] # (left, top, right, bottom)
    confidence: float            # 0.0 to 1.0
    text: Optional[str] = None   # extracted text if any
    properties: Dict[str, Any] = field(default_factory=dict) # UIA attributes, yolo class, etc.
    timestamp: float = field(default_factory=time.time)   # timestamp when the candidate was observed

@dataclass(frozen=True)
class GroundedTarget:
    """The final resolved target after ambiguity resolution."""
    candidate: TargetCandidate
    resolution_method: str       # how ambiguity was broken (e.g., 'highest_confidence')
    alternatives: int            # number of other candidates that were rejected
