"""
Omnix V6 — Vision safety helpers (Phase 13).

Pure functions that gate an *already-grounded* target against the
screenshot it was drawn from, and against time.  These functions
are intentionally small and side-effect-free: they do not import
any action surface and do not perform any computer use.  Their job
is to make the Agent reject a target that is unsafe to act on
(stale, off-screen, non-finite, or from an unknown source).
"""
from .coordinates import (
    CoordinateSafetyError,
    validate_coordinates,
    is_within_bounds,
)
from .freshness import (
    StaleScreenError,
    is_fresh,
    DEFAULT_MAX_AGE_S,
)

__all__ = [
    "CoordinateSafetyError",
    "validate_coordinates",
    "is_within_bounds",
    "StaleScreenError",
    "is_fresh",
    "DEFAULT_MAX_AGE_S",
]
