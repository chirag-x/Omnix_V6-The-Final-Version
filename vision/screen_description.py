"""
Omnix V6 — System 3 (Vision) screen description model.

:class:`ScreenDescription` is the typed result of
:func:`vision.api.observe` / :func:`vision.api.describe`.  It
captures what is on the screen *right now* — the focused
window, the visible elements (top-K by confidence), the monitor
layout, and a coarse stability signal.

It is intentionally read-only (R-8): the description is an
observation, never a verification verdict.  A caller that wants
"does this match the expected post-state?" should use
:func:`vision.api.verify` instead.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional, Tuple

from vision.grounded_element import GroundedElement


class ScreenStability(str, Enum):
    """A coarse signal about whether the screen is currently
    changing.  Used by :func:`vision.api.wait_for` to decide
    whether the wait should be satisfied.

    The signal is computed by :mod:`vision.screen.stability`
    from a rolling window of recent screenshot hashes; it is
    intentionally low-resolution because the higher-resolution
    decision (which element moved, by how much) is the job of
    :mod:`vision.recovery`.
    """

    STABLE = "STABLE"
    CHANGING = "CHANGING"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class MonitorInfo:
    """Typed description of a single attached monitor.

    The fields are populated by
    :func:`vision.screen.monitor.enumerate_monitors`; the
    defaults are safe values for environments where Win32 is
    unavailable (Linux test runners, headless CI).
    """

    monitor_id: str
    name: str
    bounds_physical_px: Tuple[int, int, int, int]  # (left, top, right, bottom)
    dpi_scale: float
    is_primary: bool
    width: int = 0
    height: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.monitor_id, str) or not self.monitor_id:
            raise ValueError("MonitorInfo.monitor_id must be a non-empty string")
        if not isinstance(self.bounds_physical_px, (tuple, list)) or len(self.bounds_physical_px) != 4:
            raise ValueError("MonitorInfo.bounds_physical_px must be a 4-tuple")
        if self.dpi_scale <= 0:
            raise ValueError(
                f"MonitorInfo.dpi_scale must be > 0 (got {self.dpi_scale!r})"
            )
        # Mirror bounds into width/height for convenience.
        l, t, r, b = (int(v) for v in self.bounds_physical_px)
        if self.width <= 0:
            object.__setattr__(self, "width", r - l)
        if self.height <= 0:
            object.__setattr__(self, "height", b - t)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "monitor_id": self.monitor_id,
            "name": self.name,
            "bounds_physical_px": list(self.bounds_physical_px),
            "dpi_scale": self.dpi_scale,
            "is_primary": self.is_primary,
            "width": self.width,
            "height": self.height,
        }


@dataclass(frozen=True)
class WindowInfo:
    """Typed description of a single visible window.

    The fields mirror what :func:`system.windows.window_service`
    reports; the dataclass is decoupled from the service so
    consumers (Brain, Agent, future vision callers) do not
    depend on the Windows-only implementation.
    """

    hwnd: int
    title: str
    process: str
    pid: int
    is_focused: bool
    bounds_physical_px: Tuple[int, int, int, int] = (0, 0, 0, 0)
    monitor_id: Optional[str] = None

    def __post_init__(self) -> None:
        if not isinstance(self.hwnd, int) or self.hwnd < 0:
            raise ValueError(f"WindowInfo.hwnd must be a non-negative int (got {self.hwnd!r})")
        if not isinstance(self.pid, int) or self.pid < 0:
            raise ValueError(f"WindowInfo.pid must be a non-negative int (got {self.pid!r})")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "hwnd": self.hwnd,
            "title": self.title,
            "process": self.process,
            "pid": self.pid,
            "is_focused": self.is_focused,
            "bounds_physical_px": list(self.bounds_physical_px),
            "monitor_id": self.monitor_id,
        }


@dataclass(frozen=True)
class ScreenDescription:
    """The structured output of :func:`vision.api.observe`.

    A frozen dataclass (R-10).  Every field is documented and
    documented defaults exist so callers can construct a
    description from partial data in tests.

    Attributes
    ----------
    screenshot_id:
        Identifier of the screenshot the description was built
        from.  ``None`` when no screenshot was needed (e.g. a
        pure UIA description).
    timestamp:
        Wall-clock time of the observation (Unix seconds, float).
    monitors:
        All attached monitors.  Always at least one; the
        primary monitor is always present.
    focused_window:
        The currently focused window, or ``None`` if no window
        is focused (e.g. desktop is showing, no process is in
        the foreground).
    elements:
        The top-K :class:`GroundedElement` observations,
        sorted by confidence descending.  ``K`` is bounded by
        the implementation; the System 3 default is 50.
    stability:
        The coarse screen-stability signal.
    text_density:
        OCR text characters per 1000 pixels of primary monitor.
        ``0.0`` when no OCR was performed.
    notes:
        Free-form notes (e.g. ``"UIA only; no screenshot"``).
    """

    screenshot_id: Optional[str]
    timestamp: float
    monitors: Tuple[MonitorInfo, ...]
    focused_window: Optional[WindowInfo]
    elements: Tuple[GroundedElement, ...]
    stability: ScreenStability
    text_density: float = 0.0
    notes: Tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.monitors:
            raise ValueError("ScreenDescription.monitors must be non-empty")
        if not isinstance(self.timestamp, (int, float)) or self.timestamp < 0:
            raise ValueError(
                f"ScreenDescription.timestamp must be >= 0 (got {self.timestamp!r})"
            )
        if not isinstance(self.stability, ScreenStability):
            try:
                object.__setattr__(self, "stability", ScreenStability(str(self.stability)))
            except ValueError:
                object.__setattr__(self, "stability", ScreenStability.UNKNOWN)

    @property
    def primary_monitor(self) -> Optional[MonitorInfo]:
        for m in self.monitors:
            if m.is_primary:
                return m
        return self.monitors[0] if self.monitors else None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "screenshot_id": self.screenshot_id,
            "timestamp": self.timestamp,
            "monitors": [m.to_dict() for m in self.monitors],
            "focused_window": None if self.focused_window is None else self.focused_window.to_dict(),
            "elements": [e.to_dict() for e in self.elements],
            "stability": self.stability.value,
            "text_density": self.text_density,
            "notes": list(self.notes),
        }


def empty_description(
    *, monitors: Optional[Tuple[MonitorInfo, ...]] = None
) -> ScreenDescription:
    """Return a sentinel :class:`ScreenDescription` for tests and
    for the case where the screen cannot be observed (no
    ScreenshotProvider, headless test runner, etc.).
    """
    primary = MonitorInfo(
        monitor_id="primary",
        name="primary",
        bounds_physical_px=(0, 0, 1920, 1080),
        dpi_scale=1.0,
        is_primary=True,
    )
    return ScreenDescription(
        screenshot_id=None,
        timestamp=time.time(),
        monitors=monitors or (primary,),
        focused_window=None,
        elements=(),
        stability=ScreenStability.UNKNOWN,
        text_density=0.0,
        notes=("empty_description: no ScreenshotProvider available",),
    )


def make_screenshot_id() -> str:
    """Generate a fresh screenshot id.  Tiny helper so call sites
    don't import ``uuid`` directly.
    """
    return uuid.uuid4().hex


__all__ = [
    "ScreenDescription",
    "ScreenStability",
    "MonitorInfo",
    "WindowInfo",
    "empty_description",
    "make_screenshot_id",
]
