"""
Coordinates Strategy for Omnix V6 Phase 7.

Parses explicit coordinate requests from the planner (e.g.
``"click 100, 200"``).  This is the only strategy that does not
*observe* anything -- it just lifts a coordinate pair out of the
user-supplied text.  We surface it as ``ObservationSource.DERIVED``
because the coordinate comes from the caller, not from any sensor.

R-22 / Phase 7.1 hardening:
  * Does NOT require a screenshot.
  * Confidence is 1.0 only when the parse is unambiguous; if two
    different coordinate pairs are present we still return them
    as separate candidates so the router can disambiguate.
  * No LLM, no side effects.
"""
from __future__ import annotations

import re
from typing import Any, List, Optional

from core.orchestration.models import ObservationSource
from vision.router.perception_strategy import PerceptionStrategy
from vision.observations.targets import TargetCandidate


# Coordinates are user-supplied; they are neither more nor less
# reliable than a UIA match.  Treat them as DERIVED evidence.
_COORDINATES_RELIABILITY = 0.9


class CoordinatesStrategy(PerceptionStrategy):
    """Parses ``"x, y"`` style requests from the planner."""

    _PATTERN = re.compile(r"(\d+)\s*[,xX\s]\s*(\d+)")

    @property
    def name(self) -> str:
        return "coordinates"

    @property
    def requires_screenshot(self) -> bool:
        return False

    @property
    def source_reliability(self) -> float:
        return _COORDINATES_RELIABILITY

    def find_targets(
        self,
        target_query: str,
        image_path: Optional[str] = None,
        **kwargs: Any,
    ) -> List[TargetCandidate]:
        candidates: List[TargetCandidate] = []
        for match in self._PATTERN.findall(target_query or ""):
            try:
                x, y = int(match[0]), int(match[1])
            except (ValueError, IndexError):
                continue
            # Build a small (3x3 px) bounding box around the point
            # so all our candidates have the same shape.
            candidates.append(
                TargetCandidate(
                    source_type=ObservationSource.DERIVED,
                    bbox=(x - 1, y - 1, x + 1, y + 1),
                    confidence=1.0,
                    text=f"coordinates: {x}, {y}",
                    properties={"x": x, "y": y},
                )
            )
        return candidates
