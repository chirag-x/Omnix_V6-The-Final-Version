"""
Omnix V6 — System 3 (Vision) public typed model.

The :class:`GroundedElement` dataclass is the canonical, *typed*
result of any vision grounding call in the System 3 Peak Upgrade.
It is the contract every public API function in :mod:`vision.api`
returns, and the contract every future brain-level / planner-level
caller should consume.

Why a new type
--------------
The existing :class:`vision.observations.targets.TargetCandidate`
(Phase 7) is a low-level observation type.  It carries
``(source, bbox, confidence, text, properties)`` and that is the
right shape for the perception router.  But the System 3 spec
also asks for:

  * a stable element id (for trace correlation),
  * an *element type* (button, link, edit, ...),
  * *enabled / visible / interactable* booleans,
  * a *semantic_role* (e.g. ``"search_result"``),
  * an 11-value typed status enum,
  * a *physical* bounding box (multi-monitor / DPI aware),
  * a monitor id and screenshot id for cross-reference.

The new dataclass is *additive*: every existing ``TargetCandidate``
can be losslessly lifted into a :class:`GroundedElement` via
:func:`from_target_candidate`, and the legacy
:class:`core.services.vision_service.VisionResult` contract
(used by the Agent and the multi-step coordinator) is unchanged.

R-8 (no claimed verification): ``status`` is observational, not a
boolean the service sets to ``True``.  ``OBSERVED`` means "we
found a candidate"; verification is the *caller's* job.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional, Tuple

from core.orchestration.models import ObservationSource
from vision.observations.targets import TargetCandidate


# ---------------------------------------------------------------------------
# Status enum
# ---------------------------------------------------------------------------

class GroundedElementStatus(str, Enum):
    """The 11-value System 3 status vocabulary.

    The new enum is *additive* to the existing
    ``VisionResult.status`` (OBSERVED / AMBIGUOUS / NOT_FOUND /
    ERROR).  It is the canonical status carried on
    :class:`GroundedElement`; the legacy four-value vocabulary is
    translated at the seam so existing call sites do not have to
    migrate.

    Values mirror section 15 of the System 3 spec.
    """

    OBSERVED = "OBSERVED"
    TARGET_NOT_FOUND = "TARGET_NOT_FOUND"
    LOW_CONFIDENCE = "LOW_CONFIDENCE"
    MULTIPLE_TARGETS = "MULTIPLE_TARGETS"
    WINDOW_NOT_VISIBLE = "WINDOW_NOT_VISIBLE"
    WINDOW_NOT_FOCUSED = "WINDOW_NOT_FOCUSED"
    UI_NOT_READY = "UI_NOT_READY"
    SCREEN_UNSTABLE = "SCREEN_UNSTABLE"
    OCR_FAILED = "OCR_FAILED"
    ACCESSIBILITY_UNAVAILABLE = "ACCESSIBILITY_UNAVAILABLE"
    TIMEOUT = "TIMEOUT"
    TARGET_CHANGED = "TARGET_CHANGED"


# Backwards-compatible mapping from the legacy 4-value vocabulary
# to the new 11-value vocabulary.  This is the *only* place the two
# vocabularies meet; the rest of the system reads one or the other
# consistently.
_LEGACY_STATUS_MAP: Dict[str, GroundedElementStatus] = {
    "OBSERVED": GroundedElementStatus.OBSERVED,
    "AMBIGUOUS": GroundedElementStatus.MULTIPLE_TARGETS,
    "NOT_FOUND": GroundedElementStatus.TARGET_NOT_FOUND,
    "ERROR": GroundedElementStatus.OCR_FAILED,
}


def from_legacy_status(legacy: str) -> GroundedElementStatus:
    """Translate a legacy ``VisionResult.status`` value to the new
    enum.  Unknown values map to ``TARGET_NOT_FOUND`` (the safest
    "no positive observation" status).
    """
    if not isinstance(legacy, str):
        return GroundedElementStatus.TARGET_NOT_FOUND
    return _LEGACY_STATUS_MAP.get(legacy, GroundedElementStatus.TARGET_NOT_FOUND)


# ---------------------------------------------------------------------------
# Element type vocabulary
# ---------------------------------------------------------------------------

# A small, app-agnostic vocabulary of UI element types.  The new
# API normalises whatever the strategy reports into one of these
# strings.  New types can be added freely; this list is not
# authoritative, but it is the closed vocabulary used by tests.
ELEMENT_TYPE_BUTTON = "button"
ELEMENT_TYPE_LINK = "link"
ELEMENT_TYPE_EDIT = "edit"
ELEMENT_TYPE_TEXT = "text"
ELEMENT_TYPE_IMAGE = "image"
ELEMENT_TYPE_CHECKBOX = "checkbox"
ELEMENT_TYPE_RADIO = "radio"
ELEMENT_TYPE_COMBOBOX = "combobox"
ELEMENT_TYPE_MENU_ITEM = "menu_item"
ELEMENT_TYPE_TAB = "tab"
ELEMENT_TYPE_ICON = "icon"
ELEMENT_TYPE_UNKNOWN = "unknown"

KNOWN_ELEMENT_TYPES = frozenset(
    {
        ELEMENT_TYPE_BUTTON,
        ELEMENT_TYPE_LINK,
        ELEMENT_TYPE_EDIT,
        ELEMENT_TYPE_TEXT,
        ELEMENT_TYPE_IMAGE,
        ELEMENT_TYPE_CHECKBOX,
        ELEMENT_TYPE_RADIO,
        ELEMENT_TYPE_COMBOBOX,
        ELEMENT_TYPE_MENU_ITEM,
        ELEMENT_TYPE_TAB,
        ELEMENT_TYPE_ICON,
        ELEMENT_TYPE_UNKNOWN,
    }
)


def normalise_element_type(raw: Optional[str]) -> str:
    """Map a strategy-reported control name to the canonical
    element-type vocabulary.  Unknown values become ``"unknown"``
    rather than being rejected — the new model is descriptive,
    not a closed set.
    """
    if not raw:
        return ELEMENT_TYPE_UNKNOWN
    s = str(raw).strip().lower().replace(" ", "_").replace("-", "_")
    if s in KNOWN_ELEMENT_TYPES:
        return s
    # Common aliases
    alias = {
        "btn": ELEMENT_TYPE_BUTTON,
        "pushbutton": ELEMENT_TYPE_BUTTON,
        "hyperlink": ELEMENT_TYPE_LINK,
        "textbox": ELEMENT_TYPE_EDIT,
        "editcontrol": ELEMENT_TYPE_EDIT,
        "input": ELEMENT_TYPE_EDIT,
        "label": ELEMENT_TYPE_TEXT,
        "statictext": ELEMENT_TYPE_TEXT,
        "picture": ELEMENT_TYPE_IMAGE,
        "img": ELEMENT_TYPE_IMAGE,
        "checkbox": ELEMENT_TYPE_CHECKBOX,
        "radiobutton": ELEMENT_TYPE_RADIO,
        "dropdown": ELEMENT_TYPE_COMBOBOX,
        "combobox": ELEMENT_TYPE_COMBOBOX,
        "menuitem": ELEMENT_TYPE_MENU_ITEM,
        "tabitem": ELEMENT_TYPE_TAB,
    }
    return alias.get(s, ELEMENT_TYPE_UNKNOWN)


# ---------------------------------------------------------------------------
# GroundedElement
# ---------------------------------------------------------------------------

# Source set is the closed vocabulary accepted by the coordinate
# safety gate.  Kept here so the new typed model is self-describing
# without importing from :mod:`vision.safety.coordinates`.
KNOWN_SOURCES = frozenset({"uia", "ocr", "derived", "vision", "screen"})


def _source_to_string(source_type: Any) -> str:
    """Coerce an :class:`ObservationSource` (or any object with a
    ``.value``) into the closed source string set.
    """
    if source_type is None:
        return "screen"
    if isinstance(source_type, str):
        return source_type if source_type in KNOWN_SOURCES else "screen"
    val = getattr(source_type, "value", None)
    if isinstance(val, str):
        return val if val in KNOWN_SOURCES else "screen"
    return "screen"


def _bbox_center(bbox: Tuple[int, int, int, int]) -> Tuple[int, int]:
    left, top, right, bottom = bbox
    cx = int(round((int(left) + int(right)) / 2))
    cy = int(round((int(top) + int(bottom)) / 2))
    return (cx, cy)


@dataclass(frozen=True)
class GroundedElement:
    """The canonical, typed result of a vision grounding call.

    The dataclass is frozen (R-10) and is the only contract the
    new :mod:`vision.api` functions return.  Constructors are
    intentionally permissive: ``None`` / missing fields fall
    back to safe defaults rather than raising, because the API
    is called from many call sites with partial data (e.g. a
    coordinate-derived candidate has no ``text``).

    Attributes
    ----------
    id:
        Stable, unique identifier for this element instance
        (UUID4 hex).  Used by the visual trace for correlation.
    type:
        One of :data:`KNOWN_ELEMENT_TYPES`.  ``"unknown"`` when
        the strategy could not classify the element.
    text:
        The element's text content (button label, link text,
        edit value, etc.).  ``None`` when not applicable.
    confidence:
        In ``[0.0, 1.0]``.  The strategy's own confidence
        (EasyOCR ``prob``, YOLO ``conf``, UIA base confidence).
    bbox:
        ``(left, top, right, bottom)`` in *physical* pixels of
        ``monitor_id``.  Always a 4-tuple of ``int``.
    center:
        ``(cx, cy)`` in the same coordinate system as ``bbox``.
    enabled, visible, interactable:
        Booleans reported by UIA when available, otherwise
        ``True`` (the safe default — we do not claim an
        element is disabled unless we observed it).
    source:
        One of :data:`KNOWN_SOURCES`.  Always set.
    semantic_role:
        Optional free-form role (e.g. ``"search_result"``,
        ``"menu_item"``, ``"tab"``).  ``None`` when the
        caller did not supply it.
    status:
        :class:`GroundedElementStatus`.  Defaults to
        :attr:`GroundedElementStatus.OBSERVED` for a
        positive candidate and to
        :attr:`GroundedElementStatus.TARGET_NOT_FOUND` for
        the sentinel "no candidate" case.
    monitor_id:
        Identifier of the monitor the bbox lives on.  ``"primary"``
        when unknown.  Set to the real monitor id by
        :func:`vision.screen.monitor.enumerate_monitors`.
    screenshot_id:
        Optional id of the screenshot the element was grounded
        against.  ``None`` when no screenshot was needed.
    timestamp:
        Wall-clock time (Unix seconds, float) when the element
        was observed.
    properties:
        Free-form properties bag for strategy-specific data
        (UIA control type, YOLO class id, OCR text bbox
        details, etc.).
    """

    id: str
    type: str
    text: Optional[str]
    confidence: float
    bbox: Tuple[int, int, int, int]
    center: Tuple[int, int]
    enabled: bool
    visible: bool
    interactable: bool
    source: str
    semantic_role: Optional[str]
    status: GroundedElementStatus
    monitor_id: str
    screenshot_id: Optional[str]
    timestamp: float
    properties: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id:
            raise ValueError(
                "GroundedElement.id must be a non-empty string"
            )
        if self.type not in KNOWN_ELEMENT_TYPES:
            # Don't raise — the type vocabulary is descriptive,
            # not closed.  Normalise to "unknown" instead.
            object.__setattr__(self, "type", ELEMENT_TYPE_UNKNOWN)
        if not isinstance(self.confidence, (int, float)):
            raise ValueError(
                f"GroundedElement.confidence must be a number (got {type(self.confidence).__name__})"
            )
        # Clamp confidence into [0, 1] silently — strategies
        # occasionally report values like 1.0000001 due to
        # floating point and we should not fail the grounding
        # over a 1e-7 noise.
        c = float(self.confidence)
        if c < 0.0 or c > 1.0:
            c = max(0.0, min(1.0, c))
            object.__setattr__(self, "confidence", c)
        if not isinstance(self.bbox, (tuple, list)) or len(self.bbox) != 4:
            raise ValueError(
                f"GroundedElement.bbox must be a 4-tuple (got {self.bbox!r})"
            )
        try:
            l, t, r, b = (int(v) for v in self.bbox)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"GroundedElement.bbox must contain ints (got {self.bbox!r})"
            ) from exc
        # Normalise so left < right and top < bottom.
        if r < l:
            l, r = r, l
        if b < t:
            t, b = b, t
        object.__setattr__(self, "bbox", (l, t, r, b))
        # Recompute center from the normalised bbox.
        object.__setattr__(self, "center", _bbox_center((l, t, r, b)))
        if self.source not in KNOWN_SOURCES:
            raise ValueError(
                f"GroundedElement.source must be one of {sorted(KNOWN_SOURCES)} "
                f"(got {self.source!r})"
            )
        if not isinstance(self.status, GroundedElementStatus):
            # Defensive: accept strings and convert.
            try:
                object.__setattr__(
                    self, "status", GroundedElementStatus(str(self.status))
                )
            except ValueError:
                object.__setattr__(
                    self, "status", GroundedElementStatus.TARGET_NOT_FOUND
                )
        if not isinstance(self.monitor_id, str) or not self.monitor_id:
            object.__setattr__(self, "monitor_id", "primary")
        if not isinstance(self.timestamp, (int, float)) or self.timestamp < 0:
            object.__setattr__(self, "timestamp", time.time())

    @property
    def is_observed(self) -> bool:
        """True iff the status is a positive observation."""
        return self.status == GroundedElementStatus.OBSERVED

    @property
    def is_negative(self) -> bool:
        """True iff the status reports a failure or non-finding.

        A ``GroundedElement`` with a negative status is the
        *sentinel* value the new :mod:`vision.api` returns when
        nothing was found; it carries a placeholder bbox at
        ``(0, 0, 0, 0)`` and the caller's code should branch on
        :attr:`is_negative` rather than on the bbox.
        """
        return self.status != GroundedElementStatus.OBSERVED

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "text": self.text,
            "confidence": self.confidence,
            "bbox": list(self.bbox),
            "center": list(self.center),
            "enabled": self.enabled,
            "visible": self.visible,
            "interactable": self.interactable,
            "source": self.source,
            "semantic_role": self.semantic_role,
            "status": self.status.value,
            "monitor_id": self.monitor_id,
            "screenshot_id": self.screenshot_id,
            "timestamp": self.timestamp,
            "properties": dict(self.properties),
        }


# ---------------------------------------------------------------------------
# Adapter: TargetCandidate -> GroundedElement
# ---------------------------------------------------------------------------


def from_target_candidate(
    candidate: TargetCandidate,
    *,
    screenshot_id: Optional[str] = None,
    monitor_id: Optional[str] = None,
    semantic_role: Optional[str] = None,
    enabled: Optional[bool] = None,
    visible: Optional[bool] = None,
    interactable: Optional[bool] = None,
    element_type: Optional[str] = None,
    status: GroundedElementStatus = GroundedElementStatus.OBSERVED,
) -> GroundedElement:
    """Build a :class:`GroundedElement` from a Phase-7
    :class:`TargetCandidate`.

    The adapter is lossless on the fields that overlap
    (source, bbox, confidence, text, properties) and uses safe
    defaults for the new fields the Phase 7 type does not have.
    Callers that *do* have the richer information (e.g. a
    post-action observation that knows the element is disabled)
    should pass it explicitly.
    """
    props = dict(candidate.properties or {})
    if element_type is None:
        element_type = normalise_element_type(props.get("control_type"))
    return GroundedElement(
        id=uuid.uuid4().hex,
        type=element_type,
        text=candidate.text,
        confidence=float(candidate.confidence),
        bbox=tuple(candidate.bbox),
        center=(0, 0),  # overwritten by __post_init__ from bbox
        enabled=bool(enabled) if enabled is not None else True,
        visible=bool(visible) if visible is not None else True,
        interactable=bool(interactable) if interactable is not None else True,
        source=_source_to_string(candidate.source_type),
        semantic_role=semantic_role,
        status=status,
        monitor_id=str(monitor_id) if monitor_id else "primary",
        screenshot_id=screenshot_id,
        timestamp=time.time(),
        properties=props,
    )


# ---------------------------------------------------------------------------
# Sentinel builders
# ---------------------------------------------------------------------------


def not_found(
    *,
    query: str = "",
    screenshot_id: Optional[str] = None,
    monitor_id: Optional[str] = None,
) -> GroundedElement:
    """Return a sentinel :class:`GroundedElement` for
    ``find`` / ``locate`` / ``wait_for`` when nothing matched.
    """
    return GroundedElement(
        id=uuid.uuid4().hex,
        type=ELEMENT_TYPE_UNKNOWN,
        text=None,
        confidence=0.0,
        bbox=(0, 0, 0, 0),
        center=(0, 0),
        enabled=False,
        visible=False,
        interactable=False,
        source="screen",
        semantic_role=None,
        status=GroundedElementStatus.TARGET_NOT_FOUND,
        monitor_id=str(monitor_id) if monitor_id else "primary",
        screenshot_id=screenshot_id,
        timestamp=time.time(),
        properties={"query": query} if query else {},
    )


def low_confidence(
    candidate: TargetCandidate,
    *,
    threshold: float,
    screenshot_id: Optional[str] = None,
    monitor_id: Optional[str] = None,
) -> GroundedElement:
    """Wrap a :class:`TargetCandidate` whose confidence fell below
    the caller's threshold into a
    :attr:`GroundedElementStatus.LOW_CONFIDENCE` element.  The
    bbox and text are preserved so the caller can still see
    *what* the strategy observed, just with a negative status.
    """
    el = from_target_candidate(
        candidate,
        screenshot_id=screenshot_id,
        monitor_id=monitor_id,
        status=GroundedElementStatus.LOW_CONFIDENCE,
    )
    # Attach the threshold for diagnostic purposes.
    new_props = dict(el.properties)
    new_props["confidence_threshold"] = float(threshold)
    return GroundedElement(
        id=el.id,
        type=el.type,
        text=el.text,
        confidence=el.confidence,
        bbox=el.bbox,
        center=el.center,
        enabled=el.enabled,
        visible=el.visible,
        interactable=el.interactable,
        source=el.source,
        semantic_role=el.semantic_role,
        status=el.status,
        monitor_id=el.monitor_id,
        screenshot_id=el.screenshot_id,
        timestamp=el.timestamp,
        properties=new_props,
    )


def ambiguous(
    candidates: list,
    *,
    screenshot_id: Optional[str] = None,
    monitor_id: Optional[str] = None,
) -> GroundedElement:
    """Return a sentinel element for the multiple-matches case.

    The candidates are stored in ``properties["alternatives"]`` so
    the Brain / Agent can present them to the user for
    disambiguation.  The bbox is the centroid of the alternatives
    so the call still has *some* coordinate to fall back on if
    the caller really must do something; the status is negative
    so the fallback is not silent.
    """
    if not candidates:
        return not_found(screenshot_id=screenshot_id, monitor_id=monitor_id)
    # Compute centroid bbox so the caller has a non-empty bbox.
    l = min(_bbox_left(c.bbox if hasattr(c, "bbox") else (0, 0, 0, 0)) for c in candidates)
    t = min(_bbox_top(c.bbox if hasattr(c, "bbox") else (0, 0, 0, 0)) for c in candidates)
    r = max(_bbox_right(c.bbox if hasattr(c, "bbox") else (0, 0, 0, 0)) for c in candidates)
    b = max(_bbox_bottom(c.bbox if hasattr(c, "bbox") else (0, 0, 0, 0)) for c in candidates)
    return GroundedElement(
        id=uuid.uuid4().hex,
        type=ELEMENT_TYPE_UNKNOWN,
        text=None,
        confidence=0.0,
        bbox=(l, t, r, b),
        center=(0, 0),  # overwritten by __post_init__
        enabled=True,
        visible=True,
        interactable=False,  # ambiguous -> not safe to act on
        source="screen",
        semantic_role=None,
        status=GroundedElementStatus.MULTIPLE_TARGETS,
        monitor_id=str(monitor_id) if monitor_id else "primary",
        screenshot_id=screenshot_id,
        timestamp=time.time(),
        properties={"alternatives": len(candidates)},
    )


def _bbox_left(bbox):
    return int(bbox[0])


def _bbox_top(bbox):
    return int(bbox[1])


def _bbox_right(bbox):
    return int(bbox[2])


def _bbox_bottom(bbox):
    return int(bbox[3])


__all__ = [
    "GroundedElement",
    "GroundedElementStatus",
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
    "not_found",
    "low_confidence",
    "ambiguous",
]
