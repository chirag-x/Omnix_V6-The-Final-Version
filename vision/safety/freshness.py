"""
Omnix V6 — Screenshot freshness (Phase 13).

A :class:`vision.observations.screenshot_metadata.ScreenshotMetadata`
is *fresh* when the time elapsed since its capture is at most
``max_age_s``.  Stale screenshots are rejected by the
coordinate-safety gate so the agent does not dispatch a click based
on a picture that no longer matches the screen.

Defaults
--------
``DEFAULT_MAX_AGE_S = 5.0`` seconds is the V6 default for a
human-paced interaction.  Hosts (and tests) may override it via
``OmnixConfig.vision_max_screenshot_stale_s``.

This module is a pure function over already-built metadata.  It
does NOT import any computer-use surface.
"""
from __future__ import annotations

import time
from typing import Optional

from vision.observations.screenshot_metadata import ScreenshotMetadata


DEFAULT_MAX_AGE_S: float = 5.0


class StaleScreenError(ValueError):
    """Raised when a screenshot is too old to act on.

    Subclass of ``ValueError`` so callers that catch the broad
    class still see it.  The Agent catches it and routes the step
    to recovery with a SAFETY failure.
    """


def is_fresh(
    meta: Optional[ScreenshotMetadata],
    *,
    now: Optional[float] = None,
    max_age_s: float = DEFAULT_MAX_AGE_S,
) -> bool:
    """Return ``True`` when ``meta`` is fresh enough to act on.

    The function returns ``False`` when ``meta`` is ``None``,
    when its ``timestamp`` is not a number, or when the age
    exceeds ``max_age_s``.  Negative ages (clock skew) are
    clamped to 0 by :meth:`ScreenshotMetadata.age_seconds`, so a
    screenshot stamped in the future is *fresh* (a host clock
    should not block a real capture).
    """
    if meta is None:
        return False
    if not isinstance(meta, ScreenshotMetadata):
        return False
    if not isinstance(meta.timestamp, (int, float)):
        return False
    if max_age_s <= 0:
        return False
    age = meta.age_seconds(now=now)
    return age <= max_age_s


def require_fresh(
    meta: Optional[ScreenshotMetadata],
    *,
    now: Optional[float] = None,
    max_age_s: float = DEFAULT_MAX_AGE_S,
) -> ScreenshotMetadata:
    """Return ``meta`` if it is fresh; raise :class:`StaleScreenError`
    otherwise.

    The Agent uses this as an explicit precondition before
    dispatching an action grounded on ``meta``.
    """
    if meta is None:
        raise StaleScreenError(
            "no screenshot metadata supplied; refusing to act on stale evidence"
        )
    if not is_fresh(meta, now=now, max_age_s=max_age_s):
        age = meta.age_seconds(now=now)
        raise StaleScreenError(
            f"screenshot {meta.screenshot_id!r} is stale "
            f"(age={age:.3f}s > max_age_s={max_age_s:.3f}s); "
            f"refresh and retry."
        )
    return meta


__all__ = [
    "StaleScreenError",
    "is_fresh",
    "require_fresh",
    "DEFAULT_MAX_AGE_S",
]
