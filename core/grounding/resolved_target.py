"""
Omnix V6 — Canonical ResolvedTarget contract for Stage 18.6.

Defines the universal target representation that bridges perception
(System 3 vision) to generic action (mouse/keyboard/etc.) without
requiring LLM involvement in the physical action loop.

The ResolvedTarget is a domain-specific wrapper that composes
existing perception output types (TargetCandidate, GroundedElement)
while adding validation helpers and canonical factories.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional, Tuple, Union

from vision.grounded_element import GroundedElement, GroundedElementStatus
from vision.observations.targets import TargetCandidate


class TargetResolutionStatus(str, Enum):
    """Status of target resolution - matches Stage 18.6 spec exactly."""

    RESOLVED = "RESOLVED"
    INVALID = "INVALID"
    STALE = "STALE"
    LOW_CONFIDENCE = "LOW_CONFIDENCE"
    OUT_OF_BOUNDS = "OUT_OF_BOUNDS"
    WINDOW_MISMATCH = "WINDOW_MISMATCH"
    UNSUPPORTED = "UNSUPPORTED"
    NOT_FOUND = "NOT_FOUND"


@dataclass(frozen=True)
class TargetResolutionResult:
    """Result of target resolution - contains status and optional resolved target."""

    status: TargetResolutionStatus
    target: Optional["ResolvedTarget"] = None
    reason: str = ""
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ResolvedTarget:
    """
    Canonical target representation for Stage 18.6 grounding layer.

    This is the universal contract that perception outputs (TargetCandidate,
    GroundedElement) and raw inputs (coordinate dicts, bbox dicts) adapt to.
    Mouse capabilities consume this to determine where to act.

    The design principle: Perception → ResolvedTarget → Generic Action.
    The LLM is NOT responsible for physically clicking - it only reasons
    about targets at this abstract level.
    """

    # Target kind - closed set for validation
    kind: str  # "coordinate" | "bbox" | "window" | "element" | "ocr" | "vision"

    # Coordinate fields (present for coordinate/bbox/element kinds)
    x: Optional[int] = None
    y: Optional[int] = None

    # Bounding box fields (present for bbox kind)
    width: Optional[int] = None
    height: Optional[int] = None

    # Derived center point (always safe to use for actions)
    center_x: Optional[int] = None
    center_y: Optional[int] = None

    # Source attribution - closed set from vision.safety.coordinates.KNOWN_SOURCES
    source: str = "screen"  # "uia" | "ocr" | "vision" | "screen" | "derived" | "coordinate"

    # Confidence and freshness
    confidence: Optional[float] = None  # None = UNKNOWN
    timestamp: Optional[float] = None   # Unix seconds; None = UNKNOWN

    # Window context
    window_hwnd: Optional[int] = None
    window_title: Optional[str] = None
    application: Optional[str] = None

    # Element identification
    identifier: Optional[str] = None  # element text / ocr text / etc.

    # Extension point for strategy-specific data
    metadata: Dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------------
    # Factory methods - lossless adapters from perception types
    # ------------------------------------------------------------------------

    @classmethod
    def coordinate(
        cls,
        x: int,
        y: int,
        *,
        source: str = "coordinate",
        confidence: Optional[float] = None,
        timestamp: Optional[float] = None,
        window_hwnd: Optional[int] = None,
        window_title: Optional[str] = None,
        application: Optional[str] = None,
        identifier: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> "ResolvedTarget":
        """Factory for direct coordinate input."""
        return cls(
            kind="coordinate",
            x=x,
            y=y,
            source=source,
            confidence=confidence,
            timestamp=timestamp,
            window_hwnd=window_hwnd,
            window_title=window_title,
            application=application,
            identifier=identifier,
            metadata=metadata or {},
            center_x=x,  # coordinate is its own center
            center_y=y,
        )

    @classmethod
    def bbox(
        cls,
        left: int,
        top: int,
        right: int,
        bottom: int,
        *,
        source: str,
        confidence: Optional[float] = None,
        timestamp: Optional[float] = None,
        window_hwnd: Optional[int] = None,
        window_title: Optional[str] = None,
        application: Optional[str] = None,
        identifier: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> "ResolvedTarget":
        """Factory for bounding box input - validates and computes center."""
        if right <= left:
            raise ValueError(f"bbox right ({right}) must be > left ({left})")
        if bottom <= top:
            raise ValueError(f"bbox bottom ({bottom}) must be > top ({top})")

        center_x = int(round((left + right) / 2))
        center_y = int(round((top + bottom) / 2))

        return cls(
            kind="bbox",
            x=center_x,
            y=center_y,
            width=right - left,
            height=bottom - top,
            source=source,
            confidence=confidence,
            timestamp=timestamp,
            window_hwnd=window_hwnd,
            window_title=window_title,
            application=application,
            identifier=identifier,
            metadata=metadata or {},
            center_x=center_x,
            center_y=center_y,
        )

    @classmethod
    def window(
        cls,
        hwnd: int,
        *,
        title: Optional[str] = None,
        application: Optional[str] = None,
        source: str = "vision",
        confidence: Optional[float] = None,
        timestamp: Optional[float] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> "ResolvedTarget":
        """Factory for window target (no coordinate yet - for window-level actions)."""
        return cls(
            kind="window",
            source=source,
            confidence=confidence,
            timestamp=timestamp,
            window_hwnd=hwnd,
            window_title=title,
            application=application,
            metadata=metadata or {},
        )

    @classmethod
    def from_grounded_element(cls, el: GroundedElement) -> "ResolvedTarget":
        """Lossless adapter from vision.grounded_element.GroundedElement."""
        # Map GroundedElementStatus to our resolution status conceptually
        # (the resolver will handle status -> resolution mapping)
        return cls(
            kind="element",
            x=el.center[0],
            y=el.center[1],
            source=el.source,
            confidence=el.confidence,
            timestamp=el.timestamp,
            window_hwnd=None,  # GroundedElement doesn't carry HWND
            window_title=None,  # GroundedElement doesn't carry window title
            application=None,   # GroundedElement doesn't carry app name
            identifier=el.text,
            metadata={
                **el.properties,
                "element_type": el.type,
                "element_id": el.id,
                "element_status": el.status.value,
                "monitor_id": el.monitor_id,
                "screenshot_id": el.screenshot_id,
                "enabled": el.enabled,
                "visible": el.visible,
                "interactable": el.interactable,
                "semantic_role": el.semantic_role,
            },
            center_x=el.center[0],
            center_y=el.center[1],
        )

    @classmethod
    def from_target_candidate(cls, cand: TargetCandidate) -> "ResolvedTarget":
        """Lossless adapter from vision.observations.targets.TargetCandidate."""
        # Map ObservationSource to our source string
        source_map = {
            "uia": "uia",
            "ocr": "ocr",
            "derived": "derived",
            "vision": "vision",
            "screen": "screen"
        }
        source_str = source_map.get(cand.source_type.value, "vision")

        return cls(
            kind="vision",  # TargetCandidate comes from vision strategies
            x=int(round((cand.bbox[0] + cand.bbox[2]) / 2)),
            y=int(round((cand.bbox[1] + cand.bbox[3]) / 2)),
            source=source_str,
            confidence=cand.confidence,
            timestamp=getattr(cand, 'timestamp', time.time()),
            window_hwnd=None,
            window_title=None,
            application=None,
            identifier=cand.text,
            metadata=dict(cand.properties),
            center_x=int(round((cand.bbox[0] + cand.bbox[2]) / 2)),
            center_y=int(round((cand.bbox[1] + cand.bbox[3]) / 2)),
        )

    # ------------------------------------------------------------------------
    # Validation helpers
    # ------------------------------------------------------------------------

    def is_valid_bbox(self) -> bool:
        """True if this target has a valid bounding box."""
        return self.kind == "bbox" and \
               self.width is not None and self.height is not None and \
               self.width > 0 and self.height > 0

    def is_coordinate_kind(self) -> bool:
        """True if this target represents a direct coordinate."""
        return self.kind == "coordinate"

    def is_bbox_kind(self) -> bool:
        """True if this target represents a bounding box."""
        return self.kind == "bbox"

    def is_element_kind(self) -> bool:
        """True if this target represents a UI element."""
        return self.kind == "element"

    def is_window_kind(self) -> bool:
        """True if this target represents a window."""
        return self.kind == "window"

    def as_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for debugging/serialization."""
        return {
            "kind": self.kind,
            "x": self.x,
            "y": self.y,
            "width": self.width,
            "height": self.height,
            "center_x": self.center_x,
            "center_y": self.center_y,
            "source": self.source,
            "confidence": self.confidence,
            "timestamp": self.timestamp,
            "window_hwnd": self.window_hwnd,
            "window_title": self.window_title,
            "application": self.application,
            "identifier": self.identifier,
            "metadata": self.metadata,
        }


def _validate_source(source: str) -> bool:
    """Validate that source is in the known sources set."""
    known_sources = frozenset({"uia", "ocr", "derived", "vision", "screen", "coordinate"})
    return source in known_sources


# ---------------------------------------------------------------------------
# Convenience functions for creating TargetResolutionResult
# ---------------------------------------------------------------------------

def resolved(target: ResolvedTarget) -> TargetResolutionResult:
    """Helper to create a RESOLVED TargetResolutionResult."""
    return TargetResolutionResult(
        status=TargetResolutionStatus.RESOLVED,
        target=target,
        reason="Target successfully resolved",
    )


def invalid(reason: str = "Invalid target input") -> TargetResolutionResult:
    """Helper to create an INVALID TargetResolutionResult."""
    return TargetResolutionResult(
        status=TargetResolutionStatus.INVALID,
        reason=reason,
    )


def stale(reason: str = "Target is stale") -> TargetResolutionResult:
    """Helper to create a STALE TargetResolutionResult."""
    return TargetResolutionResult(
        status=TargetResolutionStatus.STALE,
        reason=reason,
    )


def low_confidence(reason: str = "Target confidence too low") -> TargetResolutionResult:
    """Helper to create a LOW_CONFIDENCE TargetResolutionResult."""
    return TargetResolutionResult(
        status=TargetResolutionStatus.LOW_CONFIDENCE,
        reason=reason,
    )


def out_of_bounds(reason: str = "Target outside screen bounds") -> TargetResolutionResult:
    """Helper to create an OUT_OF_BOUNDS TargetResolutionResult."""
    return TargetResolutionResult(
        status=TargetResolutionStatus.OUT_OF_BOUNDS,
        reason=reason,
    )


def window_mismatch(reason: str = "Target window mismatch") -> TargetResolutionResult:
    """Helper to create a WINDOW_MISMATCH TargetResolutionResult."""
    return TargetResolutionResult(
        status=TargetResolutionStatus.WINDOW_MISMATCH,
        reason=reason,
    )


def unsupported(reason: str = "Unsupported target type") -> TargetResolutionResult:
    """Helper to create an UNSUPPORTED TargetResolutionResult."""
    return TargetResolutionResult(
        status=TargetResolutionStatus.UNSUPPORTED,
        reason=reason,
    )


def not_found(reason: str = "Target not found") -> TargetResolutionResult:
    """Helper to create a NOT_FOUND TargetResolutionResult."""
    return TargetResolutionResult(
        status=TargetResolutionStatus.NOT_FOUND,
        reason=reason,
    )