"""
Omnix V6 — TargetResolver for Stage 18.6.

Implements the target resolution logic that converts various target inputs
(coordinate, bbox, window, element, etc.) into validated ResolvedTarget
instances with freshness, confidence, and bounds checking.

The resolver follows the Stage 18.6 specification:
- Input: Various target formats (ResolvedTarget passthrough, GroundedElement,
         TargetCandidate, coordinate dicts, bbox dicts, window hints)
- Output: TargetResolutionResult with status RESOLVED/INVALID/STALE/etc.
- Does NOT call LLM - pure deterministic validation
- Does NOT perform actions - only resolution and validation
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional, Tuple, Union

from .resolved_target import (
    ResolvedTarget,
    TargetResolutionResult,
    TargetResolutionStatus,
)
from vision.grounded_element import GroundedElement
from vision.observations.targets import TargetCandidate


# Closed set of known sources (matches vision.safety.coordinates.KNOWN_SOURCES
# plus "coordinate" for raw coordinate input)
KNOWN_SOURCES = frozenset({
    "uia", "ocr", "derived", "vision", "screen", "coordinate"
})


class TargetResolver:
    """
    Resolves target inputs to validated ResolvedTarget instances.

    The resolver accepts multiple input formats and applies validation
    rules for freshness, confidence, screen bounds, and basic sanity.
    It does NOT perform actions - only resolution and validation.

    Example usage:
        resolver = TargetResolver(screen_width=1920, screen_height=1080)
        result = resolver.resolve({"x": 500, "y": 300})
        if result.status == TargetResolutionStatus.RESOLVED:
            click_x = result.target.center_x
            click_y = result.target.center_y
    """

    def __init__(
        self,
        *,
        screen_width: Optional[int] = None,
        screen_height: Optional[int] = None,
        max_target_age_s: float = 5.0,
        minimum_confidence: float = 0.5,
    ):
        """
        Initialize the TargetResolver.

        Args:
            screen_width: Screen width in pixels (for bounds checking)
            screen_height: Screen height in pixels (for bounds checking)
            max_target_age_s: Maximum age in seconds for target freshness
            minimum_confidence: Minimum confidence threshold (0.0-1.0)
        """
        self.screen_width = screen_width
        self.screen_height = screen_height
        self.max_target_age_s = max_target_age_s
        self.minimum_confidence = minimum_confidence

    def resolve(
        self,
        target_input: Any,
        *,
        screen_width: Optional[int] = None,
        screen_height: Optional[int] = None,
        now: Optional[float] = None,
    ) -> TargetResolutionResult:
        """
        Resolve a target input to a TargetResolutionResult.

        Accepts:
        - ResolvedTarget: passthrough with validation only
        - GroundedElement: adapt + validate
        - TargetCandidate: adapt + validate
        - dict with "x", "y": coordinate input
        - dict with "bbox": bounding box input (left,top,right,bottom)
        - dict with "target_window_hwnd": window target
        - None: NOT_FOUND

        Args:
            target_input: The target to resolve
            now: Current timestamp (defaults to time.time())

        Returns:
            TargetResolutionResult with status and optional resolved target
        """
        if now is None:
            now = time.time()

        # Handle None input
        if target_input is None:
            return TargetResolutionResult(
                status=TargetResolutionStatus.NOT_FOUND,
                reason="Target input is None",
            )

        # Handle ResolvedTarget passthrough (validate only)
        if isinstance(target_input, ResolvedTarget):
            return self._validate_resolved_target(target_input, now)

        # Handle GroundedElement
        if isinstance(target_input, GroundedElement):
            resolved_target = ResolvedTarget.from_grounded_element(target_input)
            return self._validate_resolved_target(resolved_target, now)

        # Handle TargetCandidate
        if isinstance(target_input, TargetCandidate):
            resolved_target = ResolvedTarget.from_target_candidate(target_input)
            return self._validate_resolved_target(resolved_target, now)

        # Handle dict-like inputs
        if isinstance(target_input, dict):
            return self._resolve_dict_input(target_input, now)

        # Unsupported input type
        return TargetResolutionResult(
            status=TargetResolutionStatus.UNSUPPORTED,
            reason=f"Unsupported target input type: {type(target_input).__name__}",
        )

    def _validate_resolved_target(
        self,
        target: ResolvedTarget,
        now: float,
    ) -> TargetResolutionResult:
        """
        Validate a ResolvedTarget instance for freshness, confidence, and bounds.
        """
        # Check freshness if timestamp is present
        if target.timestamp is not None:
            try:
                age = max(0.0, now - target.timestamp)
                if age > self.max_target_age_s:
                    return TargetResolutionResult(
                        status=TargetResolutionStatus.STALE,
                        reason=f"Target is stale (age={age:.3f}s > max_age_s={self.max_target_age_s:.3f}s)",
                        target=target,
                    )
            except (TypeError, ValueError):
                return TargetResolutionResult(
                    status=TargetResolutionStatus.INVALID,
                    reason="Target has invalid timestamp",
                    target=target,
                )

        # Check confidence if present
        if target.confidence is not None:
            if not (0.0 <= target.confidence <= 1.0):
                return TargetResolutionResult(
                    status=TargetResolutionStatus.INVALID,
                    reason=f"Target confidence {target.confidence} not in [0.0, 1.0]",
                    target=target,
                )
            if target.confidence < self.minimum_confidence:
                return TargetResolutionResult(
                    status=TargetResolutionStatus.LOW_CONFIDENCE,
                    reason=f"Target confidence {target.confidence} < minimum {self.minimum_confidence}",
                    target=target,
                )

        # Check source is known
        if target.source not in KNOWN_SOURCES:
            return TargetResolutionResult(
                status=TargetResolutionStatus.UNSUPPORTED,
                reason=f"Target source {target.source!r} is not in known sources",
                target=target,
            )

        # Check screen bounds if we have screen dimensions and target has coordinates
        if (self.screen_width is not None and self.screen_height is not None):
            coords_to_check = []
            if target.is_coordinate_kind() and target.x is not None and target.y is not None:
                coords_to_check.append((target.x, target.y))
            elif target.is_bbox_kind() and target.width is not None and target.height is not None:
                if target.x is not None and target.y is not None:
                    # target.x, target.y is the center; convert to corners
                    half_w = target.width // 2
                    half_h = target.height // 2
                    coords_to_check.append((target.x - half_w, target.y - half_h))
                    coords_to_check.append((target.x + half_w, target.y + half_h))

            for x, y in coords_to_check:
                if not (0 <= x < self.screen_width and 0 <= y < self.screen_height):
                    return TargetResolutionResult(
                        status=TargetResolutionStatus.OUT_OF_BOUNDS,
                        reason=f"Coordinate ({x}, {y}) is outside screen bounds "
                               f"({self.screen_width}x{self.screen_height})",
                        target=target,
                    )

        # All validations passed
        return TargetResolutionResult(
            status=TargetResolutionStatus.RESOLVED,
            target=target,
            reason="Target successfully resolved and validated",
        )

    def _resolve_dict_input(
        self,
        target_dict: Dict[str, Any],
        now: float,
    ) -> TargetResolutionResult:
        """
        Resolve dictionary-style target inputs.

        Handles:
        - {"x": int, "y": int} -> coordinate
        - {"bbox": (l, t, r, b)} -> bounding box
        - {"target_window_hwnd": int, ...} -> window target
        """
        # Coordinate input
        if "x" in target_dict and "y" in target_dict:
            try:
                x = int(target_dict["x"])
                y = int(target_dict["y"])
            except (ValueError, TypeError) as e:
                return TargetResolutionResult(
                    status=TargetResolutionStatus.INVALID,
                    reason=f"Invalid coordinate input: {str(e)}",
                )
            return self._validate_resolved_target(
                ResolvedTarget.coordinate(
                    x=x,
                    y=y,
                    source=target_dict.get("source", "coordinate"),
                    confidence=target_dict.get("confidence"),
                    timestamp=target_dict.get("timestamp", now),
                    window_hwnd=target_dict.get("target_window_hwnd"),
                    window_title=target_dict.get("target_window_title"),
                    application=target_dict.get("application"),
                    identifier=target_dict.get("identifier"),
                    metadata=target_dict.get("metadata", {}),
                ),
                now
            )

        # Bounding box input
        if "bbox" in target_dict:
            bbox = target_dict["bbox"]
            if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
                return TargetResolutionResult(
                    status=TargetResolutionStatus.INVALID,
                    reason="bbox must be a 4-tuple (left, top, right, bottom)",
                )

            try:
                left, top, right, bottom = (int(v) for v in bbox)
            except (ValueError, TypeError) as e:
                return TargetResolutionResult(
                    status=TargetResolutionStatus.INVALID,
                    reason=f"Invalid bbox values: {str(e)}",
                )

            if right <= left or bottom <= top:
                return TargetResolutionResult(
                    status=TargetResolutionStatus.INVALID,
                    reason=f"Invalid bbox: right ({right}) must be > left ({left}) "
                           f"and bottom ({bottom}) must be > top ({top})",
                )

            return self._validate_resolved_target(
                ResolvedTarget.bbox(
                    left=left,
                    top=top,
                    right=right,
                    bottom=bottom,
                    source=target_dict.get("source", "vision"),
                    confidence=target_dict.get("confidence"),
                    timestamp=target_dict.get("timestamp", now),
                    window_hwnd=target_dict.get("target_window_hwnd"),
                    window_title=target_dict.get("target_window_title"),
                    application=target_dict.get("application"),
                    identifier=target_dict.get("identifier"),
                    metadata=target_dict.get("metadata", {}),
                ),
                now
            )

        # Window target input
        if "target_window_hwnd" in target_dict:
            try:
                hwnd = int(target_dict["target_window_hwnd"])
            except (ValueError, TypeError) as e:
                return TargetResolutionResult(
                    status=TargetResolutionStatus.INVALID,
                    reason=f"Invalid window target input: {str(e)}",
                )
            return self._validate_resolved_target(
                ResolvedTarget.window(
                    hwnd=hwnd,
                    title=target_dict.get("target_window_title"),
                    application=target_dict.get("application"),
                    source=target_dict.get("source", "vision"),
                    confidence=target_dict.get("confidence"),
                    timestamp=target_dict.get("timestamp", now),
                    metadata=target_dict.get("metadata", {}),
                ),
                now
            )

        # Unsupported dict format
        return TargetResolutionResult(
            status=TargetResolutionStatus.UNSUPPORTED,
            reason=f"Unsupported dict target format. Keys: {list(target_dict.keys())}",
        )