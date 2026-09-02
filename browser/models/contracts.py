"""
Browser typed contracts (Phase 8).

This module defines the *only* data shapes that flow across the
browser subsystem boundary.  Everything is a frozen dataclass
or a closed-set ``str`` enum.  No free-form fields; no dicts
of arbitrary keys; no string-encoded commands.

Architectural rules honoured
----------------------------

* R-10 — ``frozen=True`` dataclasses with ``with_*`` builders.
* R-13 — No invented action kinds; ``BrowserAction`` is a closed set.
* R-14 — Browser is a *service*, not a singleton.  The service
  receives and returns these contracts.
* R-21 — The closed action set is the only path to a real browser.
* R-24 — Natural language is user-facing; these are internal,
  structured calls.

Closed sets
-----------

* :class:`BrowserAction` — the only mutations a plan may request.
* :class:`LocatorKind`   — the only ways a target may be identified.
* :class:`TargetResolutionMethod` — closed set of ways the locator
  was actually resolved at runtime.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Dict, Mapping, Optional, Tuple


# ---------------------------------------------------------------------------
# Action / locator closed sets
# ---------------------------------------------------------------------------

class BrowserAction(str, Enum):
    """The closed set of browser mutations a plan may request.

    Anything outside this enum is rejected at the
    :class:`core.services.browser_service.BrowserService` boundary.
    """

    OPEN = "open"                       # launch a browser context
    NAVIGATE = "navigate"               # goto a URL
    BACK = "back"
    FORWARD = "forward"
    RELOAD = "reload"
    CLICK = "click"                     # click a target element
    TYPE = "type"                       # type text into a target
    PRESS = "press"                     # press a single key / chord
    SCROLL = "scroll"                   # scroll the page (or a target)
    SELECT = "select"                   # select a <select> option
    HOVER = "hover"                     # hover over a target
    WAIT = "wait"                       # wait for a target or N ms
    EXTRACT_TEXT = "extract_text"       # read text content of a target
    EXTRACT_PAGE = "extract_page"       # read the whole page snapshot
    DOWNLOAD = "download"               # download a file via a target
    CLOSE = "close"                     # close the browser


# Source values for :class:`BrowserObservation` — closed set so the
# Brain / Verifier can branch on them deterministically.
BROWSER_OBSERVATION_SOURCES: Tuple[str, ...] = (
    "DOM",          # parsed from the live page DOM
    "ACCESSIBILITY",  # from the accessibility tree (role/aria)
    "URL",          # from the current URL
    "TITLE",        # from <title>
    "TEXT",         # free text the service was asked to read
    "DOWNLOAD",     # download result
    "ERROR",        # structured error
)


class LocatorKind(str, Enum):
    """The closed set of ways a :class:`BrowserTarget` may identify
    a DOM element.

    The browser service resolves these in the order:

        ACCESSIBILITY  →  CSS  →  TEXT  →  XPATH  →  TEST_ID

    Vision is *not* a LocatorKind — it is a fallback, not a primary
    targeting mechanism (per the Phase 8 spec).
    """

    ACCESSIBILITY = "accessibility"  # role + name (aria-label/aria-labelledby/text)
    CSS = "css"                      # a CSS selector
    TEXT = "text"                    # visible text (case-insensitive substring / exact)
    XPATH = "xpath"                  # an XPath
    TEST_ID = "test_id"              # data-testid=...


class TargetResolutionMethod(str, Enum):
    """How a target was actually resolved at runtime.

    Surfaced on :class:`BrowserResult` for the Brain / Verifier to
    branch on.  ``UNRESOLVED`` means the target was not found; the
    caller should not retry the same locator.
    """

    DOM = "dom"                          # live DOM query
    ACCESSIBILITY = "accessibility"      # accessibility tree
    VISION_FALLBACK = "vision_fallback"  # resolved through vision (only if vision enabled)
    UNRESOLVED = "unresolved"            # target not found
    SKIPPED = "skipped"                  # resolution not required (e.g. navigate)


# ---------------------------------------------------------------------------
# Target, Request, Element
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BrowserTarget:
    """A description of a DOM element the agent wants to act on.

    Exactly one of ``kind``/``value`` identifies the element.  The
    service resolves the target through a closed set of locators
    (see :class:`LocatorKind`); it never accepts raw JavaScript.

    Examples
    --------

    >>> BrowserTarget(kind=LocatorKind.CSS, value="#submit")
    >>> BrowserTarget(kind=LocatorKind.TEXT, value="Sign in")
    >>> BrowserTarget(kind=LocatorKind.ACCESSIBILITY,
    ...               value='{"role": "button", "name": "Search"}')
    """

    kind: LocatorKind
    value: str
    # Optional human-readable label; never used for resolution, only
    # for diagnostics and logging.  Must not contain secrets.
    label: str = ""
    # Optional Nth match (0-indexed).  ``None`` means "first match".
    nth: Optional[int] = None
    # When True the service must use a strict (exact) match.
    # Defaults to False (substring matches for text, first match for css).
    strict: bool = False

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "kind": self.kind.value,
            "value": self.value,
        }
        if self.label:
            d["label"] = self.label
        if self.nth is not None:
            d["nth"] = self.nth
        if self.strict:
            d["strict"] = True
        return d


@dataclass(frozen=True)
class BrowserRequest:
    """A single browser action request.

    A :class:`BrowserRequest` is the *internal* contract between the
    planner/agent and :class:`BrowserService`.  It carries a closed
    :class:`BrowserAction`, the optional :class:`BrowserTarget`, and
    closed-set parameters.  Free-form ``str`` payloads are forbidden.
    """

    action: BrowserAction
    session_id: str = ""             # empty = default session
    target: Optional[BrowserTarget] = None
    # Closed-set action parameters.  Each action has a well-defined
    # key set (see :data:`_ACTION_PARAM_KEYS`).  Unknown keys are
    # rejected at the service boundary.
    parameters: Mapping[str, Any] = field(default_factory=dict)
    # The originating goal id (for logging/audit).  Never logged
    # with secrets.
    goal_id: str = ""
    # The originating plan step id (for logging/audit).
    plan_step_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"action": self.action.value}
        if self.session_id:
            d["session_id"] = self.session_id
        if self.target is not None:
            d["target"] = self.target.to_dict()
        if self.parameters:
            d["parameters"] = dict(self.parameters)
        if self.goal_id:
            d["goal_id"] = self.goal_id
        if self.plan_step_id:
            d["plan_step_id"] = self.plan_step_id
        return d

    def with_parameter(self, key: str, value: Any) -> "BrowserRequest":
        new_params = dict(self.parameters)
        new_params[key] = value
        return replace(self, parameters=new_params)

    def with_target(self, target: Optional[BrowserTarget]) -> "BrowserRequest":
        return replace(self, target=target)


# ---------------------------------------------------------------------------
# Element, PageState, Observation
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BrowserElement:
    """A snapshot of a single DOM element as seen by the service."""

    tag: str
    text: str = ""
    role: str = ""
    name: str = ""
    value: str = ""
    href: str = ""
    selector: str = ""               # the canonical CSS selector
    attributes: Mapping[str, str] = field(default_factory=dict)
    visible: bool = True

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "tag": self.tag,
            "visible": self.visible,
        }
        if self.text:
            d["text"] = self.text
        if self.role:
            d["role"] = self.role
        if self.name:
            d["name"] = self.name
        if self.value:
            d["value"] = self.value
        if self.href:
            d["href"] = self.href
        if self.selector:
            d["selector"] = self.selector
        if self.attributes:
            d["attributes"] = dict(self.attributes)
        return d


@dataclass(frozen=True)
class BrowserPageState:
    """A structural snapshot of the current page (URL + title + refs)."""

    url: str
    title: str = ""
    # A small, bounded set of representative element references so
    # the Brain can decide if "we are on the search results page".
    # These are NOT full HTML dumps (would be slow + would leak
    # structure to logs); they're bounded key/value pairs.
    element_refs: Tuple[BrowserElement, ...] = ()
    # The DOM source is the page HTML — may be elided for memory.
    dom_source: str = ""
    # A bounded, snapshot of visible text on the page (truncated).
    visible_text: str = ""
    cookies_count: int = 0
    is_secure_context: bool = False
    viewport: Tuple[int, int] = (0, 0)

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "url": self.url,
            "cookies_count": self.cookies_count,
            "is_secure_context": self.is_secure_context,
            "viewport": list(self.viewport),
        }
        if self.title:
            d["title"] = self.title
        if self.element_refs:
            d["element_refs"] = [e.to_dict() for e in self.element_refs]
        if self.dom_source:
            d["dom_source"] = self.dom_source
        if self.visible_text:
            d["visible_text"] = self.visible_text
        return d


@dataclass(frozen=True)
class BrowserObservation:
    """A typed observation from the browser.

    Like :class:`core.orchestration.models.Observation`, this is
    *observational*, not *verifying*.  The Brain / Verifier is the
    only thing that decides whether the observation matches the
    :class:`ExpectedEffect`; the service never claims ``verified``.
    """

    source: str                              # one of BROWSER_OBSERVATION_SOURCES
    state: Optional[BrowserPageState] = None
    element: Optional[BrowserElement] = None
    extracted_text: str = ""
    error: Optional[str] = None
    resolution_method: TargetResolutionMethod = TargetResolutionMethod.SKIPPED
    # A bounded record of what happened — never full HTML, never cookies.
    details: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "source": self.source,
            "resolution_method": self.resolution_method.value,
        }
        if self.state is not None:
            d["state"] = self.state.to_dict()
        if self.element is not None:
            d["element"] = self.element.to_dict()
        if self.extracted_text:
            d["extracted_text"] = self.extracted_text
        if self.error:
            d["error"] = self.error
        if self.details:
            d["details"] = dict(self.details)
        return d

    @property
    def ok(self) -> bool:
        return self.error is None and self.source != "ERROR"


# ---------------------------------------------------------------------------
# Result and Session
# ---------------------------------------------------------------------------

class BrowserResultStatus(str, Enum):
    """Closed set of result statuses a :class:`BrowserService` may return."""

    OK = "ok"                                # success
    TARGET_NOT_FOUND = "target_not_found"    # target could not be resolved
    NAVIGATION_FAILED = "navigation_failed"  # navigation error
    TIMEOUT = "timeout"                      # action timed out
    DOWNLOAD_FAILED = "download_failed"      # download error
    INVALID_REQUEST = "invalid_request"      # request failed service-side validation
    SESSION_NOT_FOUND = "session_not_found"  # unknown session id
    ERROR = "error"                          # unclassified error
    BLOCKED = "blocked"                      # safety policy refused the request


@dataclass(frozen=True)
class BrowserResult:
    """The structured result of a :class:`BrowserRequest`.

    Mirrors V6's result-normalisation pattern (R-3, R-8): success/
    failure are explicit enums; the service never claims ``verified``
    from a single action; ``observation`` is the post-action state
    the Brain can compare against the :class:`ExpectedEffect`.
    """

    status: BrowserResultStatus
    action: BrowserAction
    request: BrowserRequest
    observation: Optional[BrowserObservation] = None
    error: Optional[str] = None
    started_at: float = 0.0
    finished_at: float = 0.0
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "status": self.status.value,
            "action": self.action.value,
            "request": self.request.to_dict(),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }
        if self.observation is not None:
            d["observation"] = self.observation.to_dict()
        if self.error:
            d["error"] = self.error
        if self.metadata:
            d["metadata"] = dict(self.metadata)
        return d

    @property
    def ok(self) -> bool:
        return self.status == BrowserResultStatus.OK

    def with_observation(
        self, observation: Optional[BrowserObservation]
    ) -> "BrowserResult":
        return replace(self, observation=observation)

    def with_error(self, status: BrowserResultStatus, error: str) -> "BrowserResult":
        return replace(self, status=status, error=error)

    def with_metadata(self, **kv: Any) -> "BrowserResult":
        new_meta = dict(self.metadata)
        new_meta.update(kv)
        return replace(self, metadata=new_meta)


@dataclass(frozen=True)
class BrowserSessionInfo:
    """A small, *non-secret* description of an open browser session.

    Cookies, storage, credentials, and full page text are never
    included here — only URLs, titles, viewports, and lifecycle
    state.  The Brain / Verifier can inspect this safely; the LLM
    never sees session secrets.
    """

    session_id: str
    is_open: bool
    current_url: str = ""
    current_title: str = ""
    viewport: Tuple[int, int] = (0, 0)
    headless: bool = True
    opened_at: float = 0.0
    last_action_at: float = 0.0
    action_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "is_open": self.is_open,
            "current_url": self.current_url,
            "current_title": self.current_title,
            "viewport": list(self.viewport),
            "headless": self.headless,
            "opened_at": self.opened_at,
            "last_action_at": self.last_action_at,
            "action_count": self.action_count,
        }


# ---------------------------------------------------------------------------
# Per-action parameter key sets (closed sets)
# ---------------------------------------------------------------------------
#
# Each action accepts only the keys listed in its tuple.  Unknown
# keys are rejected at the service boundary.  This is the same
# closed-set discipline as the capability layer (R-21).
# ---------------------------------------------------------------------------

ACTION_PARAM_KEYS: Mapping[BrowserAction, Tuple[str, ...]] = {
    BrowserAction.OPEN: (
        "headless",          # bool
        "viewport_width",    # int
        "viewport_height",   # int
        "browser_engine",    # "chromium" | "firefox" | "webkit"  (default chromium)
        "start_url",         # optional initial URL
    ),
    BrowserAction.NAVIGATE: (
        "url",               # str (required)
        "wait_until",        # "load" | "domcontentloaded" | "networkidle"
        "timeout_ms",        # int
    ),
    BrowserAction.BACK: ("timeout_ms",),
    BrowserAction.FORWARD: ("timeout_ms",),
    BrowserAction.RELOAD: ("timeout_ms",),
    BrowserAction.CLICK: (
        "button",            # "left" | "right" | "middle"
        "click_count",       # int (default 1)
        "delay_ms",          # int — pre-click delay
        "force",             # bool
    ),
    BrowserAction.TYPE: (
        "text",              # str (required)
        "delay_ms",          # int — per-keystroke delay
        "submit",            # bool — press Enter after typing
    ),
    BrowserAction.PRESS: (
        "key",               # str (required), e.g. "Enter", "Escape", "Tab", "Control+a"
    ),
    BrowserAction.SCROLL: (
        "direction",         # "up" | "down" | "left" | "right" (required)
        "amount",            # int (required, pixels)
    ),
    BrowserAction.SELECT: (
        "value",             # str — the option's value attribute (required)
        "label",             # str — the option's visible label (required if value not set)
    ),
    BrowserAction.HOVER: (
        "timeout_ms",
    ),
    BrowserAction.WAIT: (
        "until",             # "visible" | "hidden" | "attached" | "networkidle" (required)
        "timeout_ms",        # int
    ),
    BrowserAction.EXTRACT_TEXT: (
        "max_chars",         # int — bound the result (default 4000)
        "include_attributes",  # bool — include role/name/href in output
    ),
    BrowserAction.EXTRACT_PAGE: (
        "max_chars",         # int
    ),
    BrowserAction.DOWNLOAD: (
        "save_to",           # str (required) — absolute filesystem path
    ),
    BrowserAction.CLOSE: (),
}
