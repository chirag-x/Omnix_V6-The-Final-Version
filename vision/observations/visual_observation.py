"""
Omnix V6 — Visual observation contract (Phase 13).

The :class:`VisualObservation` dataclass is the *typed* description
of a single post-action visual observation.  It is intentionally
distinct from :class:`core.orchestration.grounding.
TargetGroundingContract`:

  * ``TargetGroundingContract`` is a *pre-action* assertion
    ("the Save button is at (x, y) with confidence 0.92") that the
    Agent uses to decide where to dispatch a click.
  * ``VisualObservation`` is a *post-action* observation
    ("after the click, the Save dialog is visible at (x, y) with
    confidence 0.83") that the Verifier compares against the
    :class:`core.orchestration.models.ExpectedEffect`.

Keeping the two contracts distinct prevents the Agent from
confusing "where I plan to click" with "what I see after I
clicked".  Both carry a :class:`ScreenshotMetadata` so the
coordinate-safety gate can reject stale captures.

R-8: a :class:`VisualObservation` does NOT claim ``verified``;
the verifier decides that.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional, Tuple

from .screenshot_metadata import ScreenshotMetadata


class VisualObservationStatus(str, Enum):
    """The status of a visual observation.

    The status values mirror the existing
    :class:`core.orchestration.grounding.GroundingStatus` so the
    verifier can use the same dispatch table.
    """

    OBSERVED = "OBSERVED"        # a matching target is visible
    AMBIGUOUS = "AMBIGUOUS"      # multiple indistinguishable candidates
    NOT_FOUND = "NOT_FOUND"      # no candidate matched
    ERROR = "ERROR"              # vision pipeline failed


@dataclass(frozen=True)
class VisualObservation:
    """A typed post-action visual observation.

    Attributes
    ----------
    subject:
        What the observer was looking for (e.g. "the Save
        dialog").  The verifier compares this against the
        :class:`ExpectedEffect`'s free-form description.
    status:
        One of :class:`VisualObservationStatus`.
    bbox:
        The bounding box ``(l, t, r, b)`` of the resolved
        subject, when ``status == OBSERVED``.
    center:
        The ``(x, y)`` center of ``bbox``, pre-computed.  ``None``
        when the subject was not located.
    confidence:
        The routing confidence in ``[0, 1]``.
    source:
        The sensor that produced the candidate (``"uia"``,
        ``"ocr"``, ``"vision"``, ``"derived"``, ``"screen"``).
    resolution_method:
        A short label the router stamped.
    screenshot_metadata:
        The :class:`ScreenshotMetadata` this observation was
        drawn from.  ``None`` when the observation was made
        without a screenshot.
    candidates:
        When ``status == AMBIGUOUS``, the list of indistinguishable
        candidates.
    error:
        Human-readable reason when ``status`` is ``ERROR`` /
        ``NOT_FOUND`` / ``AMBIGUOUS``.
    metadata:
        Free-form additional metadata.
    timestamp:
        Wall-clock time (Unix seconds) when the observation was
        made.  The Verifier may use this to compute staleness.
    """

    subject: str
    status: VisualObservationStatus = VisualObservationStatus.OBSERVED
    bbox: Optional[Tuple[int, int, int, int]] = None
    center: Optional[Tuple[int, int]] = None
    confidence: float = 0.0
    source: str = ""
    resolution_method: str = ""
    screenshot_metadata: Optional[ScreenshotMetadata] = None
    candidates: Tuple[Dict[str, Any], ...] = ()
    error: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = 0.0

    def __post_init__(self) -> None:
        if not isinstance(self.subject, str):
            raise ValueError(
                "VisualObservation.subject must be a string "
                f"(got {type(self.subject).__name__})"
            )
        if not isinstance(self.confidence, (int, float)):
            raise ValueError(
                "VisualObservation.confidence must be a number "
                f"(got {type(self.confidence).__name__})"
            )
        if not (0.0 <= float(self.confidence) <= 1.0):
            raise ValueError(
                "VisualObservation.confidence must be in [0, 1] "
                f"(got {self.confidence!r})"
            )
        if self.timestamp and not isinstance(self.timestamp, (int, float)):
            raise ValueError(
                "VisualObservation.timestamp must be a number when provided "
                f"(got {type(self.timestamp).__name__})"
            )

    @property
    def is_observed(self) -> bool:
        return self.status is VisualObservationStatus.OBSERVED

    @property
    def is_blocking(self) -> bool:
        """``True`` when the observation must NOT be treated as
        evidence that the action succeeded.
        """
        return self.status in (
            VisualObservationStatus.AMBIGUOUS,
            VisualObservationStatus.NOT_FOUND,
            VisualObservationStatus.ERROR,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": "VisualObservation",
            "subject": self.subject,
            "status": self.status.value,
            "bbox": self.bbox,
            "center": self.center,
            "confidence": self.confidence,
            "source": self.source,
            "resolution_method": self.resolution_method,
            "screenshot_metadata": (
                self.screenshot_metadata.to_dict()
                if self.screenshot_metadata is not None
                else None
            ),
            "candidates": list(self.candidates),
            "error": self.error,
            "metadata": dict(self.metadata),
            "timestamp": self.timestamp,
        }


__all__ = [
    "VisualObservation",
    "VisualObservationStatus",
]
