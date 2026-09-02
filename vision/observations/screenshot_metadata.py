"""
Omnix V6 — Screenshot metadata (Phase 13).

The :class:`ScreenshotMetadata` dataclass is the *typed* description
of a single screenshot.  It is intentionally minimal: it describes
*when* and *where on the screen* the screenshot was taken, not the
screenshot itself.

Why a separate dataclass
-------------------------
The :class:`vision.router.screenshot_provider.ScreenshotProvider`
protocol historically returned ``Optional[str]`` (a filesystem path).
That is enough for the lazy-screenshot-acquisition path, but a
*typed* observer also needs:

  * the capture timestamp (so coordinate safety can reject stale
    captures — the screen may have changed since the picture was
    taken);
  * the image dimensions (so the coordinate-safety gate can reject
    a target that lives outside the picture);
  * a screenshot id (so logs and audit trails can refer to a
    specific capture without leaking the path);
  * the monitor identifier (so a multi-monitor mismatch can be
    rejected).

This module defines the dataclass and the small factory helpers
that build it from the closed ``desktop.screenshot`` capability
result.

R-22: deterministic, no LLM in the loop.  This module never
imports any vision *strategy* — it only describes an already-taken
screenshot.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


# Default source name; the closed capability set calls the
# screenshot capability ``desktop.screenshot``.  Phase 13 keeps the
# source as a string (not an enum) because the dataclass may be
# constructed from many different code paths in tests.
DEFAULT_SCREENSHOT_SOURCE = "desktop.screenshot"


@dataclass(frozen=True)
class ScreenshotMetadata:
    """A typed description of a single captured screenshot.

    Attributes
    ----------
    screenshot_id:
        A short, stable identifier (UUID4) for this specific
        capture.  Used for log correlation; never the file path.
    timestamp:
        Wall-clock time (Unix seconds, float) when the screenshot
        was captured.  Used by :func:`vision.safety.freshness.is_fresh`.
    image_width:
        Width of the captured image in pixels.  Must be > 0.
    image_height:
        Height of the captured image in pixels.  Must be > 0.
    monitor_id:
        Optional identifier of the monitor this screenshot came
        from.  ``None`` means "the primary monitor" (or that the
        capture pipeline cannot tell).
    source:
        A short label of which pipeline produced this capture.
        Defaults to ``"desktop.screenshot"``.
    path:
        The optional filesystem path where the image was written.
        The V6 project does not require the image to be on disk
        for downstream consumers; the metadata is enough.
    metadata:
        Free-form additional metadata.  Used to attach
        correlation ids, etc.
    """

    screenshot_id: str
    timestamp: float
    image_width: int
    image_height: int
    monitor_id: Optional[str] = None
    source: str = DEFAULT_SCREENSHOT_SOURCE
    path: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.screenshot_id, str) or not self.screenshot_id:
            raise ValueError(
                "ScreenshotMetadata.screenshot_id must be a non-empty string"
            )
        if not isinstance(self.timestamp, (int, float)) or self.timestamp < 0:
            raise ValueError(
                "ScreenshotMetadata.timestamp must be a non-negative number "
                f"(got {self.timestamp!r})"
            )
        if not isinstance(self.image_width, int) or self.image_width <= 0:
            raise ValueError(
                "ScreenshotMetadata.image_width must be a positive int "
                f"(got {self.image_width!r})"
            )
        if not isinstance(self.image_height, int) or self.image_height <= 0:
            raise ValueError(
                "ScreenshotMetadata.image_height must be a positive int "
                f"(got {self.image_height!r})"
            )
        if not isinstance(self.source, str) or not self.source:
            raise ValueError(
                "ScreenshotMetadata.source must be a non-empty string"
            )

    @property
    def width(self) -> int:
        return self.image_width

    @property
    def height(self) -> int:
        return self.image_height

    def age_seconds(self, *, now: Optional[float] = None) -> float:
        """Return how many seconds have passed since this capture.

        If ``now`` is ``None``, the current wall-clock time is
        used.  The returned value is non-negative.
        """
        if now is None:
            now = time.time()
        return max(0.0, float(now) - float(self.timestamp))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": "ScreenshotMetadata",
            "screenshot_id": self.screenshot_id,
            "timestamp": self.timestamp,
            "image_width": self.image_width,
            "image_height": self.image_height,
            "monitor_id": self.monitor_id,
            "source": self.source,
            "path": self.path,
            "metadata": dict(self.metadata),
        }


def make_screenshot_metadata(
    *,
    image_width: int,
    image_height: int,
    timestamp: Optional[float] = None,
    screenshot_id: Optional[str] = None,
    monitor_id: Optional[str] = None,
    source: str = DEFAULT_SCREENSHOT_SOURCE,
    path: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> ScreenshotMetadata:
    """Build a :class:`ScreenshotMetadata` with sensible defaults.

    The factory stamps a fresh UUID4 and the current wall-clock
    time when the caller does not pass them.
    """
    return ScreenshotMetadata(
        screenshot_id=screenshot_id or uuid.uuid4().hex,
        timestamp=float(timestamp) if timestamp is not None else time.time(),
        image_width=int(image_width),
        image_height=int(image_height),
        monitor_id=monitor_id,
        source=source,
        path=path,
        metadata=dict(metadata) if metadata else {},
    )


def from_capability_result(result: Dict[str, Any]) -> ScreenshotMetadata:
    """Build a :class:`ScreenshotMetadata` from a capability result dict.

    The ``desktop.screenshot`` capability returns a dict with at
    least ``path`` and ideally ``width`` / ``height``.  When the
    shape is missing or partial, the factory uses safe defaults
    (1×1 image) and stamps a fresh id and timestamp — the
    coordinate-safety gate will then reject the screenshot because
    nothing fits in a 1×1 image, which is the *desired* safe
    failure.
    """
    if not isinstance(result, dict):
        result = {}
    return make_screenshot_metadata(
        image_width=int(result.get("width", 1) or 1),
        image_height=int(result.get("height", 1) or 1),
        timestamp=result.get("timestamp"),
        monitor_id=result.get("monitor_id"),
        path=result.get("path"),
        metadata={
            k: v
            for k, v in result.items()
            if k not in {"width", "height", "timestamp", "monitor_id", "path"}
        },
    )


__all__ = [
    "ScreenshotMetadata",
    "make_screenshot_metadata",
    "from_capability_result",
    "DEFAULT_SCREENSHOT_SOURCE",
]
