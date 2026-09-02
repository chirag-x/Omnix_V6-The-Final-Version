"""
Browser router tests (Phase 8).

Exercises :class:`browser.router.dispatcher.BrowserRouter` against a
real :class:`BrowserSession` whose Playwright surface is replaced by
the in-memory fake from :mod:`browser_fakes`.

Covers:

* dispatch for every action
* per-action parameter key enforcement at the router layer
* open/close lifecycle through the router
* closed action enum (no unhandled action)
* vision fallback wired in / opted out
* result always carries request + status + action
* target resolution order (DOM, accessibility, vision_fallback)
* error mapping (target_not_found, navigation_failed, ...)
"""

from __future__ import annotations

from typing import Optional

import pytest

from browser.models.contracts import (
    ACTION_PARAM_KEYS,
    BrowserAction,
    BrowserRequest,
    BrowserResultStatus,
    BrowserTarget,
    LocatorKind,
)
from browser.router.dispatcher import BrowserRouter
from browser.strategies.vision_fallback import (
    NullVisionFallback,
    VisionFallback,
    VisionFallbackResult,
)

from browser_fakes import (
    build_simple_dom,
    fake_browser_factory,
    fixture_session,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _router_session():
    """Create an open session with the fixture DOM ready, plus a router."""
    from browser.session.session import BrowserSession

    browser = fake_browser_factory()
    s = BrowserSession("router-test", playwright_factory=lambda: browser)
    s.open()
    s._page.dom = build_simple_dom()
    s._page.url = "file:///fixture/index.html"
    s._page.title = "Router fixture"
    return s


def _make_router(*, vision: "Optional[VisionFallback]" = None) -> BrowserRouter:
    return BrowserRouter(_router_session(), vision_fallback=vision)


def _req(action: BrowserAction, **params) -> BrowserRequest:
    return BrowserRequest(action=action, parameters=params)


def _with_target(
    req: BrowserRequest, kind: LocatorKind, value: str, **kw
) -> BrowserRequest:
    return req.with_target(BrowserTarget(kind=kind, value=value, **kw))


# ---------------------------------------------------------------------------
# Type / constructor
# ---------------------------------------------------------------------------

def test_router_requires_browser_session() -> None:
    with pytest.raises(TypeError):
        BrowserRouter("not a session")  # type: ignore[arg-type]


def test_router_defaults_to_null_vision_fallback() -> None:
    r = _make_router()
    # Internal accessor — confirm we can use it without raising.
    assert r._vision is not None  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

def test_router_open_creates_session() -> None:
    from browser.session.session import BrowserSession
    from browser_fakes import fake_browser_factory

    browser = fake_browser_factory()
    s = BrowserSession("open-me", playwright_factory=lambda: browser)
    r = BrowserRouter(s)
    assert s.is_open is False
    res = r.dispatch(_req(BrowserAction.OPEN, headless=True, viewport_width=800, viewport_height=600))
    assert res.status == BrowserResultStatus.OK
    assert s.is_open is True
    s.close()  # cleanup


def test_router_close_when_session_never_opened() -> None:
    from browser.session.session import BrowserSession
    from browser_fakes import fake_browser_factory

    browser = fake_browser_factory()
    s = BrowserSession("never-opened", playwright_factory=lambda: browser)
    r = BrowserRouter(s)
    res = r.dispatch(_req(BrowserAction.CLOSE))
    assert res.status == BrowserResultStatus.OK


def test_router_close_after_session_opened() -> None:
    r = _make_router()
    res = r.dispatch(_req(BrowserAction.CLOSE))
    assert res.status == BrowserResultStatus.OK
    assert r._session.is_open is False  # type: ignore[attr-defined]


def test_router_dispatch_without_open_returns_session_not_found() -> None:
    from browser.session.session import BrowserSession
    from browser_fakes import fake_browser_factory

    browser = fake_browser_factory()
    s = BrowserSession("not-open", playwright_factory=lambda: browser)
    r = BrowserRouter(s)
    res = r.dispatch(_req(BrowserAction.NAVIGATE, url="https://example.com"))
    assert res.status == BrowserResultStatus.SESSION_NOT_FOUND


# ---------------------------------------------------------------------------
# Per-action parameter key enforcement
# ---------------------------------------------------------------------------

def test_router_rejects_unknown_param_key() -> None:
    r = _make_router()
    res = r.dispatch(_req(BrowserAction.NAVIGATE, url="https://example.com", evil="x"))
    assert res.status == BrowserResultStatus.INVALID_REQUEST
    assert "evil" in (res.error or "")


def test_router_accepts_only_closed_param_keys() -> None:
    """Every action's param key is in the closed set ACTION_PARAM_KEYS.

    The router first does a *key* check (rejecting unknown keys) and
    then a *value* check (e.g. ``headless`` must be a bool).  This
    test only pins the key check, so we use a value that is shaped
    correctly for each action.
    """
    # Hand-picked valid values per action that pass the value check.
    VALID_VALUES = {
        BrowserAction.OPEN: {"headless": True, "viewport_width": 800,
                             "viewport_height": 600, "start_url": ""},
        BrowserAction.NAVIGATE: {"url": "https://example.com", "wait_until": "load",
                                 "timeout_ms": 5000},
        BrowserAction.BACK: {"timeout_ms": 5000, "wait_until": "load"},
        BrowserAction.FORWARD: {"timeout_ms": 5000, "wait_until": "load"},
        BrowserAction.RELOAD: {"timeout_ms": 5000, "wait_until": "load"},
        BrowserAction.CLICK: {"button": "left", "click_count": 1,
                              "delay_ms": 0, "force": False, "timeout_ms": 5000},
        BrowserAction.HOVER: {"timeout_ms": 5000},
        BrowserAction.TYPE: {"text": "x", "delay_ms": 0, "timeout_ms": 5000},
        BrowserAction.PRESS: {"key": "Enter"},
        BrowserAction.SCROLL: {"direction": "down", "amount": 100},
        BrowserAction.SELECT: {"value": "ca", "label": "", "timeout_ms": 5000},
        BrowserAction.WAIT: {"until": "visible", "timeout_ms": 5000},
        BrowserAction.EXTRACT_TEXT: {"max_chars": 64, "include_attributes": False},
        BrowserAction.EXTRACT_PAGE: {"max_chars": 256},
        BrowserAction.DOWNLOAD: {"save_to": "/tmp/report.pdf"},
        BrowserAction.CLOSE: {},
    }
    for action, keys in ACTION_PARAM_KEYS.items():
        valid = VALID_VALUES.get(action, {})
        # Some actions need a base set of params to be valid regardless
        # of the key under test (e.g. NAVIGATE always needs ``url``).
        BASE_PARAMS = {
            BrowserAction.NAVIGATE: {"url": "https://example.com"},
            BrowserAction.OPEN: {},
            BrowserAction.SELECT: {"value": "ca"},
        }
        # Some actions also need a target.  We use a TARGET that points
        # at the submit button (always present in the fixture).
        TARGETED_ACTIONS = {
            BrowserAction.CLICK, BrowserAction.HOVER, BrowserAction.TYPE,
            BrowserAction.SELECT, BrowserAction.WAIT, BrowserAction.EXTRACT_TEXT,
        }
        for allowed in keys:
            r = _make_router()
            params = dict(BASE_PARAMS.get(action, {}))
            # OPEN requires viewport_width and viewport_height together;
            # dispatch them as a pair, not individually.
            if action == BrowserAction.OPEN and allowed in (
                "viewport_width", "viewport_height"
            ):
                params.update({"viewport_width": 800, "viewport_height": 600})
            else:
                params[allowed] = valid.get(allowed, "x")
            req = BrowserRequest(action=action, parameters=params)
            if action in TARGETED_ACTIONS:
                req = req.with_target(
                    BrowserTarget(kind=LocatorKind.TEST_ID, value="submit-btn")
                )
            res = r.dispatch(req)
            # The KEY check is the only thing we're pinning.  The router
            # may still return INVALID_REQUEST for a different reason
            # (e.g. OPEN's headless-not-bool if we passed a string).
            # We only assert that the rejection is *not* a key-set
            # rejection.
            if res.status == BrowserResultStatus.INVALID_REQUEST:
                assert "is not in the closed parameter set" not in (
                    res.error or ""
                ), (
                    f"action {action.value!r}: key {allowed!r} "
                    f"rejected as unknown ({res.error!r})"
                )


# ---------------------------------------------------------------------------
# Navigation
# ---------------------------------------------------------------------------

def test_router_navigate_records_url() -> None:
    r = _make_router()
    res = r.dispatch(_req(BrowserAction.NAVIGATE, url="https://example.com"))
    assert res.status == BrowserResultStatus.OK
    assert r._session.state.current_url == "https://example.com"  # type: ignore[attr-defined]


def test_router_back_forward_reload() -> None:
    r = _make_router()
    assert r.dispatch(_req(BrowserAction.NAVIGATE, url="https://a")).status == BrowserResultStatus.OK
    assert r.dispatch(_req(BrowserAction.BACK)).status == BrowserResultStatus.OK
    assert r.dispatch(_req(BrowserAction.FORWARD)).status == BrowserResultStatus.OK
    assert r.dispatch(_req(BrowserAction.RELOAD)).status == BrowserResultStatus.OK


# ---------------------------------------------------------------------------
# Mutating actions through the router
# ---------------------------------------------------------------------------

def test_router_click_resolves_target() -> None:
    r = _make_router()
    req = _with_target(_req(BrowserAction.CLICK), LocatorKind.TEST_ID, "submit-btn")
    res = r.dispatch(req)
    assert res.status == BrowserResultStatus.OK
    assert res.observation is not None
    assert res.observation.resolution_method.value in {"dom", "accessibility"}


def test_router_type_fills_value() -> None:
    r = _make_router()
    req = _with_target(_req(BrowserAction.TYPE, text="alice"), LocatorKind.CSS, "#u")
    res = r.dispatch(req)
    assert res.status == BrowserResultStatus.OK
    inp = r._session._page.dom.find_css("#u")[0]  # type: ignore[attr-defined]
    assert inp.attrs.get("value") == "alice"


def test_router_press_records_key() -> None:
    r = _make_router()
    res = r.dispatch(_req(BrowserAction.PRESS, key="Enter"))
    assert res.status == BrowserResultStatus.OK


def test_router_press_rejects_unsafe_key() -> None:
    r = _make_router()
    res = r.dispatch(_req(BrowserAction.PRESS, key="; rm -rf /"))
    assert res.status == BrowserResultStatus.ERROR


def test_router_hover() -> None:
    r = _make_router()
    req = _with_target(_req(BrowserAction.HOVER), LocatorKind.CSS, "#u")
    assert r.dispatch(req).status == BrowserResultStatus.OK


def test_router_scroll() -> None:
    r = _make_router()
    res = r.dispatch(_req(BrowserAction.SCROLL, direction="down", amount=200))
    assert res.status == BrowserResultStatus.OK


def test_router_select_by_value() -> None:
    r = _make_router()
    req = _with_target(
        _req(BrowserAction.SELECT, value="ca"), LocatorKind.CSS, "#country"
    )
    assert r.dispatch(req).status == BrowserResultStatus.OK


def test_router_select_requires_value_or_label() -> None:
    r = _make_router()
    req = _with_target(_req(BrowserAction.SELECT), LocatorKind.CSS, "#country")
    res = r.dispatch(req)
    assert res.status == BrowserResultStatus.INVALID_REQUEST


def test_router_target_not_found_returns_target_not_found() -> None:
    r = _make_router()
    req = _with_target(_req(BrowserAction.CLICK), LocatorKind.CSS, "#nope")
    res = r.dispatch(req)
    assert res.status == BrowserResultStatus.TARGET_NOT_FOUND


# ---------------------------------------------------------------------------
# Extract / wait / download
# ---------------------------------------------------------------------------

def test_router_extract_text() -> None:
    r = _make_router()
    req = _with_target(
        _req(BrowserAction.EXTRACT_TEXT, max_chars=64),
        LocatorKind.TEST_ID, "submit-btn",
    )
    res = r.dispatch(req)
    assert res.status == BrowserResultStatus.OK
    obs = res.observation
    assert obs is not None
    assert obs.extracted_text
    assert "Sign" in obs.extracted_text


def test_router_extract_text_without_target_raises() -> None:
    r = _make_router()
    res = r.dispatch(_req(BrowserAction.EXTRACT_TEXT, max_chars=10))
    assert res.status == BrowserResultStatus.INVALID_REQUEST


def test_router_extract_page() -> None:
    r = _make_router()
    res = r.dispatch(_req(BrowserAction.EXTRACT_PAGE, max_chars=128))
    assert res.status == BrowserResultStatus.OK
    assert res.observation is not None


def test_router_wait_until_visible() -> None:
    r = _make_router()
    req = _with_target(
        _req(BrowserAction.WAIT, until="visible", timeout_ms=500),
        LocatorKind.TEST_ID, "submit-btn",
    )
    assert r.dispatch(req).status == BrowserResultStatus.OK


def test_router_wait_until_networkidle() -> None:
    r = _make_router()
    res = r.dispatch(_req(BrowserAction.WAIT, until="networkidle", timeout_ms=500))
    assert res.status == BrowserResultStatus.OK


def test_router_wait_invalid_until_raises() -> None:
    r = _make_router()
    res = r.dispatch(_req(BrowserAction.WAIT, until="gibberish"))
    # Session surfaces "invalid until" as a session error, which the
    # router classifies as ERROR (not one of the targeted buckets).
    assert res.status == BrowserResultStatus.ERROR
    assert "until" in (res.error or "").lower()


# ---------------------------------------------------------------------------
# Observation honesty (R-8): router never claims `verified`
# ---------------------------------------------------------------------------

def test_router_observation_never_claims_verified() -> None:
    r = _make_router()
    req = _with_target(
        _req(BrowserAction.EXTRACT_TEXT, max_chars=64),
        LocatorKind.TEST_ID, "submit-btn",
    )
    res = r.dispatch(req)
    assert res.observation is not None
    assert not hasattr(res.observation, "verified")
    assert "verified" not in res.observation.to_dict()


# ---------------------------------------------------------------------------
# Vision fallback wiring
# ---------------------------------------------------------------------------

def test_router_vision_fallback_invoked_when_dom_fails() -> None:
    class CountingVision:
        """Implements the VisionFallback protocol (ground_via_vision)."""

        def __init__(self) -> None:
            self.calls = 0

        def ground_via_vision(
            self,
            target_query: str,
            screenshot_path: str,
        ) -> VisionFallbackResult:
            self.calls += 1
            return VisionFallbackResult(
                resolved=False,
                error="counting-vision: not really",
            )

    v = CountingVision()
    r = _make_router(vision=v)
    req = _with_target(
        _req(BrowserAction.CLICK),
        LocatorKind.CSS,
        "#does-not-exist",
    )
    # DOM fails → vision is tried → still not_found because the fake
    # vision reports resolved=False, but the call WAS made.
    res = r.dispatch(req)
    assert res.status == BrowserResultStatus.TARGET_NOT_FOUND
    assert v.calls >= 1


def test_null_vision_fallback_does_not_resolve() -> None:
    n = NullVisionFallback()
    r = n.ground_via_vision(target_query="anything", screenshot_path="/tmp/x.png")
    assert r.resolved is False
    assert r.error == "vision fallback not configured"


# ---------------------------------------------------------------------------
# Result always carries the request
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("action", list(BrowserAction))
def test_router_result_carries_request(action: BrowserAction) -> None:
    r = _make_router()
    # We need a session open for non-OPEN actions.  _make_router opens it.
    request = BrowserRequest(action=action)
    res = r.dispatch(request)
    assert res.request is request
    assert res.action is action
    assert res.status in set(BrowserResultStatus)
    # Result must be JSON-serializable.
    d = res.to_dict()
    assert d["action"] == action.value
    assert d["status"] in {s.value for s in BrowserResultStatus}
