"""
Omnix V6 — System 3 (Vision) screen subpackage.

Public surface for the multi-monitor / DPI awareness layer and
the screen-stability detector.  Both modules are pure, side-
effect-free, and importable on non-Windows hosts (they fall
back to a single virtual primary monitor when Win32 is
unavailable).
"""
from .monitor import (
    MonitorInfo,
    enumerate_monitors,
    refresh_monitors,
    get_monitor_by_id,
    primary_monitor,
    to_virtual_coords,
    from_virtual_coords,
)
from .stability import (
    compute_stability,
    is_stable,
    StabilityWindow,
    DEFAULT_THRESHOLD,
)


__all__ = [
    "MonitorInfo",
    "enumerate_monitors",
    "refresh_monitors",
    "get_monitor_by_id",
    "primary_monitor",
    "to_virtual_coords",
    "from_virtual_coords",
    "compute_stability",
    "is_stable",
    "StabilityWindow",
    "DEFAULT_THRESHOLD",
]
