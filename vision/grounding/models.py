from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, List, Dict, Any

from vision.observations.targets import TargetCandidate, GroundedTarget

class TargetKind(Enum):
    ELEMENT = "element"
    TEXT_REGION = "text_region"
    WINDOW = "window"
    APPLICATION = "application"
    GENERIC = "generic"

class GroundingStatus(Enum):
    RESOLVED = "resolved"
    NOT_FOUND = "not_found"
    AMBIGUOUS = "ambiguous"
    LOW_CONFIDENCE = "low_confidence"
    STALE = "stale"
    INVALID = "invalid"
    UNSUPPORTED = "unsupported"

@dataclass(frozen=True)
class SpatialRelationship:
    relation_type: str  # "above", "below", "left_of", "right_of", "near", "inside"
    target_spec: 'TargetSpec'

@dataclass(frozen=True)
class TargetSpec:
    """Generic semantic description of a target for grounding."""
    semantic_name: Optional[str] = None
    target_kind: TargetKind = TargetKind.GENERIC
    role: Optional[str] = None  # e.g., "button", "link", "input"
    text: Optional[str] = None
    application: Optional[str] = None
    window: Optional[str] = None
    relationship: Optional[SpatialRelationship] = None
    ordinal: Optional[int] = None  # e.g., 1 for "first", 8 for "eighth"
    state: Dict[str, Any] = field(default_factory=dict) # e.g. {"enabled": True}
    constraints: Dict[str, Any] = field(default_factory=dict)
    
@dataclass(frozen=True)
class GroundingCandidate:
    """A scored candidate during the grounding process."""
    candidate: TargetCandidate
    score: float
    confidence: float
    factors: Dict[str, float] = field(default_factory=dict) # breakdown of the score (e.g. semantic=0.8, role=1.0)
    
@dataclass(frozen=True)
class GroundingResult:
    """The outcome of a grounding request."""
    status: GroundingStatus
    target: Optional[GroundedTarget] = None
    confidence: float = 0.0
    candidates: List[GroundingCandidate] = field(default_factory=list)
    reason: Optional[str] = None
    observation_id: Optional[str] = None
    diagnostics: Dict[str, Any] = field(default_factory=dict)
