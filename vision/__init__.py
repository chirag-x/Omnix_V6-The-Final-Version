"""
Omnix V6 — System 3 (Vision) public namespace.

Subsystems outside the Vision layer should import from this
module — not from the internal :mod:`vision.router`,
:mod:`vision.strategies`, or :mod:`vision.observations`
subpackages.  The eight public functions and the typed
result model are the only contract the rest of V6 needs to
honour.
"""
from vision.api import (
    VisionError,
    WaitTimeout,
    VerificationVerdict,
    describe,
    find,
    is_focused,
    is_visible,
    locate,
    observe,
    set_default_provider,
    set_default_router,
    verify,
    wait_for,
)
from vision.grounded_element import (
    ELEMENT_TYPE_BUTTON,
    ELEMENT_TYPE_CHECKBOX,
    ELEMENT_TYPE_COMBOBOX,
    ELEMENT_TYPE_EDIT,
    ELEMENT_TYPE_ICON,
    ELEMENT_TYPE_IMAGE,
    ELEMENT_TYPE_LINK,
    ELEMENT_TYPE_MENU_ITEM,
    ELEMENT_TYPE_RADIO,
    ELEMENT_TYPE_TAB,
    ELEMENT_TYPE_TEXT,
    ELEMENT_TYPE_UNKNOWN,
    GroundedElement,
    GroundedElementStatus,
    KNOWN_ELEMENT_TYPES,
    KNOWN_SOURCES,
    from_target_candidate,
    from_legacy_status,
    normalise_element_type,
)
from vision.screen import (
    DEFAULT_THRESHOLD as _DEFAULT_STABILITY_THRESHOLD,
    MonitorInfo,
    StabilityWindow,
    compute_stability,
    enumerate_monitors,
    from_virtual_coords,
    is_stable,
    primary_monitor,
    refresh_monitors,
    to_virtual_coords,
)
from vision.screen_description import (
    ScreenDescription,
    ScreenStability,
    WindowInfo,
    empty_description,
    make_screenshot_id,
)


# The stability threshold is re-exported under a vision-level
# name so callers don't need to know about the subpackage
# layout.
STABILITY_THRESHOLD = _DEFAULT_STABILITY_THRESHOLD

__all__ = [
    # Public API
    "observe",
    "describe",
    "find",
    "locate",
    "is_visible",
    "is_focused",
    "wait_for",
    "verify",
    "VisionError",
    "WaitTimeout",
    "VerificationVerdict",
    "set_default_router",
    "set_default_provider",
    # Typed result model
    "GroundedElement",
    "GroundedElementStatus",
    "ScreenDescription",
    "ScreenStability",
    "MonitorInfo",
    "WindowInfo",
    "StabilityWindow",
    "STABILITY_THRESHOLD",
    # Element-type vocabulary
    "ELEMENT_TYPE_BUTTON",
    "ELEMENT_TYPE_LINK",
    "ELEMENT_TYPE_EDIT",
    "ELEMENT_TYPE_TEXT",
    "ELEMENT_TYPE_IMAGE",
    "ELEMENT_TYPE_CHECKBOX",
    "ELEMENT_TYPE_RADIO",
    "ELEMENT_TYPE_COMBOBOX",
    "ELEMENT_TYPE_MENU_ITEM",
    "ELEMENT_TYPE_TAB",
    "ELEMENT_TYPE_ICON",
    "ELEMENT_TYPE_UNKNOWN",
    "KNOWN_ELEMENT_TYPES",
    "KNOWN_SOURCES",
    "from_target_candidate",
    "from_legacy_status",
    "normalise_element_type",
    # Screen helpers
    "enumerate_monitors",
    "refresh_monitors",
    "primary_monitor",
    "to_virtual_coords",
    "from_virtual_coords",
    "compute_stability",
    "is_stable",
    "empty_description",
    "make_screenshot_id",
]
