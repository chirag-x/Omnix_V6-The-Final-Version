"""
Omnix V6 — System 3 (Vision) multi-monitor / DPI awareness.

This module is the *only* place in the Vision subsystem that
talks to the OS to enumerate monitors.  Every other module
receives a tuple of :class:`MonitorInfo` from
:func:`enumerate_monitors` and is otherwise platform-neutral.

Why this lives in ``vision/`` and not in ``system/windows/``:
    - The Vision subsystem needs the monitor table to validate
      grounded coordinates (multi-monitor / DPI safety gate);
      the coordinate safety gate is a Vision concern.
    - Decoupling the call from ``system/windows/`` means
      non-Windows hosts (Linux test runners, headless CI,
      macOS dev machines) can still use the rest of the
      Vision subsystem; they get a single virtual primary
      monitor instead of a Win32 error.
    - The ctypes call is fully contained here; no other module
      needs to know that GDI is the underlying API.

Implementation
--------------
We use ``EnumDisplayMonitors`` + ``GetDpiForMonitor`` via
``ctypes`` so we do not introduce a hard dependency on
``pywin32`` (the rest of V6 uses pywin32 elsewhere, but the
Vision layer's lazy imports should not pull it in for a
single API call).

Fallback
--------
When the Win32 calls fail or the host is non-Windows, we
return a single virtual primary monitor with the bounds
``(0, 0, 1920, 1080)`` and ``dpi_scale = 1.0``.  The rest of
the Vision subsystem can run unchanged; the coordinate
safety gate will then reject any out-of-bounds candidate
with the same error message as before.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import logging
import sys
import threading
from dataclasses import dataclass
from typing import List, Optional, Tuple

from vision.screen_description import MonitorInfo

# Default virtual primary monitor for non-Windows / Win32-unavailable hosts.
_DEFAULT_BOUNDS = (0, 0, 1920, 1080)
_DEFAULT_DPI_SCALE = 1.0

_lock = threading.Lock()
_cached_monitors: Optional[Tuple[MonitorInfo, ...]] = None

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Win32 ctypes glue
# ---------------------------------------------------------------------------

if sys.platform == "win32":
    try:
        _user32 = ctypes.WinDLL("user32", use_last_error=True)
        _shcore = ctypes.WinDLL("shcore", use_last_error=True)
        _WIN32_AVAILABLE = True
    except Exception:  # noqa: BLE001
        _user32 = None
        _shcore = None
        _WIN32_AVAILABLE = False
else:
    _user32 = None
    _shcore = None
    _WIN32_AVAILABLE = False


# MONITORENUMPROC callback signature.
if _WIN32_AVAILABLE:
    _MONITORENUMPROC = ctypes.WINFUNCTYPE(
        ctypes.c_bool,
        wt.HMONITOR,
        wt.HDC,
        ctypes.POINTER(wt.RECT),
        ctypes.POINTER(wt.LPARAM),
    )

    _MDT_EFFECTIVE_DPI = 0


def _enum_monitors_win32() -> List[MonitorInfo]:  # pragma: no cover - Win32 path
    """Enumerate the attached monitors via Win32."""
    out: List[MonitorInfo] = []

    def _cb(hmonitor, hdc, lprect, lparam):
        try:
            rect = lprect.contents
            # Resolve DPI for the monitor.
            dpi_x = wt.UINT(96)
            dpi_y = wt.UINT(96)
            try:
                _shcore.GetDpiForMonitor(
                    hmonitor, _MDT_EFFECTIVE_DPI,
                    ctypes.byref(dpi_x), ctypes.byref(dpi_y),
                )
            except Exception:  # noqa: BLE001
                pass
            # Scale factor (96 DPI = 1.0).
            scale = float(dpi_x.value) / 96.0 if dpi_x.value else 1.0
            if scale <= 0.0:
                scale = 1.0
            # Monitor info: name + primary.
            try:
                info = wt.MONITORINFO()
                info.cbSize = ctypes.sizeof(wt.MONITORINFO)
                if _user32.GetMonitorInfoW(hmonitor, ctypes.byref(info)):
                    name = info.szDevice
                    is_primary = bool(info.dwFlags & 1)  # MONITORINFOF_PRIMARY
                else:
                    name = f"monitor-{hmonitor}"
                    is_primary = False
            except Exception:  # noqa: BLE001
                name = f"monitor-{hmonitor}"
                is_primary = False
            monitor_id = f"monitor-{hmonitor}"
            out.append(
                MonitorInfo(
                    monitor_id=monitor_id,
                    name=name,
                    bounds_physical_px=(
                        int(rect.left), int(rect.top),
                        int(rect.right), int(rect.bottom),
                    ),
                    dpi_scale=scale,
                    is_primary=is_primary,
                )
            )
        except Exception as exc:  # noqa: BLE001
            _log.debug("monitor enum callback failed: %s", exc)
        return True

    try:
        _user32.EnumDisplayMonitors(None, None, _MONITORENUMPROC(_cb), 0)
    except Exception as exc:  # noqa: BLE001
        _log.debug("EnumDisplayMonitors failed: %s", exc)
    return out


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _virtual_primary() -> MonitorInfo:
    """Return the safe fallback for non-Windows hosts."""
    return MonitorInfo(
        monitor_id="primary",
        name="primary",
        bounds_physical_px=_DEFAULT_BOUNDS,
        dpi_scale=_DEFAULT_DPI_SCALE,
        is_primary=True,
    )


def enumerate_monitors(*, force_refresh: bool = False) -> Tuple[MonitorInfo, ...]:
    """Return a tuple of all attached monitors.

    The result is cached per-process; pass ``force_refresh=True``
    to invalidate the cache (e.g. after a monitor hot-plug
    event).  The cache is process-local and guarded by a lock so
    the call is safe to make from any thread.

    On non-Windows hosts (or when the Win32 calls fail), returns
    a single virtual primary monitor ``(0, 0, 1920, 1080)``
    with ``dpi_scale = 1.0``.
    """
    global _cached_monitors
    with _lock:
        if _cached_monitors is not None and not force_refresh:
            return _cached_monitors
        monitors: Tuple[MonitorInfo, ...]
        if _WIN32_AVAILABLE:
            try:
                raw = _enum_monitors_win32()
                if raw:
                    # Sort by (is_primary desc, left asc) so the
                    # primary monitor is always index 0.
                    raw.sort(key=lambda m: (not m.is_primary, m.bounds_physical_px[0]))
                    monitors = tuple(raw)
                else:
                    monitors = (_virtual_primary(),)
            except Exception as exc:  # noqa: BLE001
                _log.debug("monitor enumeration failed: %s", exc)
                monitors = (_virtual_primary(),)
        else:
            monitors = (_virtual_primary(),)
        _cached_monitors = monitors
        return monitors


def refresh_monitors() -> Tuple[MonitorInfo, ...]:
    """Invalidate the cache and re-enumerate.  Returns the fresh
    tuple.  Useful after a monitor hot-plug event.
    """
    return enumerate_monitors(force_refresh=True)


def get_monitor_by_id(monitor_id: str) -> Optional[MonitorInfo]:
    """Return the monitor with the given id, or ``None``."""
    for m in enumerate_monitors():
        if m.monitor_id == monitor_id:
            return m
    return None


def primary_monitor() -> MonitorInfo:
    """Return the primary monitor, falling back to the first
    monitor if none claims primary.
    """
    monitors = enumerate_monitors()
    for m in monitors:
        if m.is_primary:
            return m
    return monitors[0]


# ---------------------------------------------------------------------------
# Coordinate-space conversion helpers
# ---------------------------------------------------------------------------


def to_virtual_coords(
    point: Tuple[int, int],
    *,
    monitor_id: Optional[str] = None,
) -> Tuple[int, int]:
    """Convert a physical-pixel point to *virtual* (DPI-scaled)
    coordinates for the given monitor.

    On a 100% DPI monitor (scale = 1.0) the result is identical
    to the input.  On a 150% DPI monitor the result is the
    input divided by 1.5 (rounded).  Use this when a downstream
    consumer expects logical coordinates (e.g. some Win32 APIs).
    """
    if not isinstance(point, (tuple, list)) or len(point) != 2:
        raise ValueError(f"point must be a 2-tuple (got {point!r})")
    x, y = int(point[0]), int(point[1])
    if monitor_id:
        m = get_monitor_by_id(monitor_id)
        if m is None:
            return (x, y)
        scale = m.dpi_scale
    else:
        scale = primary_monitor().dpi_scale
    if scale <= 0:
        return (x, y)
    return (int(round(x / scale)), int(round(y / scale)))


def from_virtual_coords(
    point: Tuple[int, int],
    *,
    monitor_id: Optional[str] = None,
) -> Tuple[int, int]:
    """Inverse of :func:`to_virtual_coords`.  Convert a
    logical-coordinate point back to physical pixels.
    """
    if not isinstance(point, (tuple, list)) or len(point) != 2:
        raise ValueError(f"point must be a 2-tuple (got {point!r})")
    x, y = int(point[0]), int(point[1])
    if monitor_id:
        m = get_monitor_by_id(monitor_id)
        if m is None:
            return (x, y)
        scale = m.dpi_scale
    else:
        scale = primary_monitor().dpi_scale
    if scale <= 0:
        return (x, y)
    return (int(round(x * scale)), int(round(y * scale)))


__all__ = [
    "MonitorInfo",
    "enumerate_monitors",
    "refresh_monitors",
    "get_monitor_by_id",
    "primary_monitor",
    "to_virtual_coords",
    "from_virtual_coords",
]
