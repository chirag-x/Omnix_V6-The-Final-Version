"""
Omnix V6 — Coordinate safety (Phase 13).

The Agent dispatches a computer action with ``(x, y)`` coordinates
that were *grounded* by vision.  Before the agent fires the mouse,
the :func:`validate_coordinates` function checks that:

  * the coordinates are integers (or integer-coercible);
  * each value is finite (no NaN, no infinity);
  * each value lies inside the screenshot's reported pixel
    dimensions;
  * the originating source is one of the closed set
    (``"uia"``, ``"ocr"``, ``"derived"``, ``"vision"``, ``"screen"``);
  * the screenshot's monitor id matches the candidate's monitor id
    (when both are known).

A failure raises :class:`CoordinateSafetyError`, a ``ValueError``
subclass.  The Agent catches it and routes the step to recovery
with a SAFETY failure (R-21: a malformed grounding must never
silently dispatch).

This module is a pure function over already-grounded data.  It
does NOT import any computer-use surface.
"""
from __future__ import annotations

import math
from typing import Any, Optional, Tuple

from vision.observations.screenshot_metadata import ScreenshotMetadata


# Known sources a grounded target may originate from.  Anything
# else is rejected — refusing to act on evidence whose provenance
# is unknown is a *safety* decision, not a quality decision.
KNOWN_SOURCES = frozenset({"uia", "ocr", "derived", "vision", "screen"})


class CoordinateSafetyError(ValueError):
    """Raised when a grounded target fails coordinate safety.

    This is a ``ValueError`` subclass so callers that catch the
    broad class still see it.  Tests should also catch it
    explicitly to assert specific failure modes.
    """


def is_within_bounds(
    point: Tuple[int, int],
    *,
    width: int,
    height: int,
) -> bool:
    """Return ``True`` when ``(x, y)`` is in ``[0, width) x [0, height)``.

    Edge case: an empty screen (``width <= 0`` or ``height <= 0``)
    is never within bounds.
    """
    if not isinstance(point, tuple) or len(point) != 2:
        return False
    if not isinstance(width, int) or not isinstance(height, int):
        return False
    if width <= 0 or height <= 0:
        return False
    x, y = point
    if not isinstance(x, int) or not isinstance(y, int):
        return False
    return 0 <= x < width and 0 <= y < height


def validate_coordinates(
    point: Any,
    *,
    screenshot_metadata: Optional[ScreenshotMetadata],
    source: Optional[str] = None,
    monitor_id: Optional[str] = None,
) -> Tuple[int, int]:
    """Validate a grounded ``(x, y)`` against a screenshot.

    Parameters
    ----------
    point:
        The candidate coordinate.  Must be a 2-tuple of integers.
    screenshot_metadata:
        The :class:`ScreenshotMetadata` the candidate was drawn
        from.  When ``None``, validation fails (no evidence =
        no action).
    source:
        The originating source (e.g. ``"uia"``).  When ``None``,
        the function does not enforce the source check.
    monitor_id:
        The candidate's monitor id.  When ``None``, the function
        does not enforce the monitor-mismatch check.

    Returns
    -------
    ``(x, y)`` as integers.

    Raises
    ------
    CoordinateSafetyError
        When any of the safety checks fail.
    """
    # 1. Point shape
    if not isinstance(point, (tuple, list)) or len(point) != 2:
        raise CoordinateSafetyError(
            f"grounded point must be a 2-tuple/list (got {type(point).__name__})"
        )
    try:
        x = int(point[0])
        y = int(point[1])
    except (TypeError, ValueError) as exc:
        raise CoordinateSafetyError(
            f"grounded point must contain integers (got {point!r})"
        ) from exc

    # 2. Finiteness (NaN, inf)
    if isinstance(point[0], float) and not math.isfinite(point[0]):
        raise CoordinateSafetyError(
            f"grounded point.x is not finite: {point[0]!r}"
        )
    if isinstance(point[1], float) and not math.isfinite(point[1]):
        raise CoordinateSafetyError(
            f"grounded point.y is not finite: {point[1]!r}"
        )

    # 3. Screenshot must be present
    if screenshot_metadata is None:
        raise CoordinateSafetyError(
            "cannot validate coordinates without a ScreenshotMetadata"
        )
    if not isinstance(screenshot_metadata, ScreenshotMetadata):
        raise CoordinateSafetyError(
            "screenshot_metadata must be a ScreenshotMetadata instance"
        )

    # 4. Within bounds
    if not is_within_bounds(
        (x, y),
        width=screenshot_metadata.image_width,
        height=screenshot_metadata.image_height,
    ):
        raise CoordinateSafetyError(
            f"grounded point ({x}, {y}) is outside screenshot bounds "
            f"({screenshot_metadata.image_width}x"
            f"{screenshot_metadata.image_height})"
        )

    # 5. Source is in the closed set (when provided)
    if source is not None and source not in KNOWN_SOURCES:
        raise CoordinateSafetyError(
            f"grounded point source {source!r} is not in the known source "
            f"set {sorted(KNOWN_SOURCES)}"
        )

    # 6. Monitor-id mismatch
    if (
        monitor_id is not None
        and screenshot_metadata.monitor_id is not None
        and monitor_id != screenshot_metadata.monitor_id
    ):
        raise CoordinateSafetyError(
            f"grounded point monitor {monitor_id!r} does not match "
            f"screenshot monitor {screenshot_metadata.monitor_id!r}"
        )

    return x, y


__all__ = [
    "CoordinateSafetyError",
    "is_within_bounds",
    "validate_coordinates",
    "KNOWN_SOURCES",
]
