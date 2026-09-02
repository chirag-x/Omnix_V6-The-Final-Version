"""
Browser service tests (Phase 8).

Exercises :class:`core.services.browser_service.BrowserService` —
the single V6 boundary to the browser subsystem — against the
in-memory fake from :mod:`browser_fakes`.

Covers:

* construction defaults and injection
* open / close / idempotent lifecycle
* the structured API (navigate, click, type, press, hover, scroll,
  select, wait, extract_text, extract_page, back, forward, reload)
* ``execute(request)`` low-level entry
* policy enforcement at the service boundary
* session registry and lookup
* goal_id / plan_step_id propagation
* result.observation and result.error
* safety: no subprocess / shell in service module
"""

from __future__ import annotations

import json
from typing import Optional

import pytest

from browser.models.contracts import (
    BrowserAction,
    BrowserRequest,
    BrowserResult,
    BrowserResultStatus,
    BrowserTarget,
    LocatorKind,
)
from browser.safety.policy import BrowserSafetyPolicy
from browser.strategies.vision_fallback import (
    NullVisionFallback,
    VisionFallback,
    VisionFallbackResult,
)
from core.services.browser_service import (
    DEFAULT_SESSION_ID,
    BrowserService,
)

from browser_fakes import (
    build_simple_dom,
    fake_browser_factory,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_service(
    *,
    policy: Optional[BrowserSafetyPolicy] = None,
    vision: "Optional[VisionFallback]" = None,
) -> BrowserService:
    return BrowserService(
        policy=policy or BrowserSafetyPolicy(),
        vision_fallback=vision,
        playwright_factory=fake_browser_factory,
        headless=True,
    )


def _open_with_fixture(svc: BrowserService) -> BrowserService:
    """Open the service, install a fixture DOM on its page, return it."""
    res = svc.open()
    assert res.status == BrowserResultStatus.OK
    # Find the registered session and inject a fixture DOM.
    s = svc._sessions[DEFAULT_SESSION_ID]  # type: ignore[attr-defined]
    s._page.dom = build_simple_dom()  # type: ignore[attr-defined]
    s._page.url = "file:///fixture/index.html"  # type: ignore[attr-defined]
    return svc


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------

def test_service_default_policy_is_permissive_for_safe_schemes() -> None:
    svc = _make_service()
    assert svc.is_healthy() is True


def test_service_accepts_custom_policy() -> None:
    policy = BrowserSafetyPolicy(
        host_allowlist=frozenset({"example.com"}),
    )
    svc = _open_with_fixture(_make_service(policy=policy))
    res = svc.navigate("https://example.com")
    assert res.status == BrowserResultStatus.OK
    res2 = svc.navigate("https://other.com")
    assert res2.status == BrowserResultStatus.BLOCKED


def test_service_accepts_custom_vision_fallback() -> None:
    class AlwaysResolves:
        def ground_via_vision(
            self, target_query: str, screenshot_path: str,
        ) -> VisionFallbackResult:
            return VisionFallbackResult(
                resolved=True, x=10, y=20, width=30, height=40,
                confidence=0.9, text=target_query,
            )

    svc = _make_service(vision=AlwaysResolves())
    assert isinstance(svc._vision, AlwaysResolves)  # type: ignore[attr-defined]


def test_service_default_vision_fallback_is_null() -> None:
    svc = _make_service()
    assert isinstance(svc._vision, NullVisionFallback)  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

def test_service_open_creates_default_session() -> None:
    svc = _make_service()
    res = svc.open()
    assert res.status == BrowserResultStatus.OK
    infos = svc.list_sessions()
    assert any(i.session_id == DEFAULT_SESSION_ID for i in infos)


def test_service_open_with_viewport() -> None:
    svc = _make_service()
    res = svc.open(viewport=(1024, 768))
    assert res.status == BrowserResultStatus.OK
    info = svc.get_session_info(DEFAULT_SESSION_ID)
    assert info is not None
    assert info.viewport == (1024, 768)


def test_service_open_idempotent() -> None:
    svc = _make_service()
    r1 = svc.open()
    r2 = svc.open()
    assert r1.status == BrowserResultStatus.OK
    assert r2.status == BrowserResultStatus.OK
    assert len(svc.list_sessions()) == 1


def test_service_close() -> None:
    svc = _make_service()
    svc.open()
    r = svc.close()
    assert r.status == BrowserResultStatus.OK


def test_service_close_when_no_session_is_open() -> None:
    svc = _make_service()
    r = svc.close()
    assert r.status == BrowserResultStatus.OK


def test_service_close_specific_session() -> None:
    svc = _make_service()
    svc.open(session_id="s1")
    svc.open(session_id="s2")
    r1 = svc.close(session_id="s1")
    r2 = svc.close(session_id="s2")
    assert r1.status == BrowserResultStatus.OK
    assert r2.status == BrowserResultStatus.OK
    ids = {i.session_id for i in svc.list_sessions()}
    assert "s1" not in ids
    assert "s2" not in ids


def test_service_unknown_session_returns_session_not_found() -> None:
    svc = _make_service()
    svc.open()
    r = svc.click(
        BrowserTarget(kind=LocatorKind.CSS, value="#x"),
        session_id="ghost",
    )
    assert r.status == BrowserResultStatus.SESSION_NOT_FOUND


# ---------------------------------------------------------------------------
# Structured API
# ---------------------------------------------------------------------------

def test_service_navigate_records_url() -> None:
    svc = _open_with_fixture(_make_service())
    res = svc.navigate("https://example.com")
    assert res.status == BrowserResultStatus.OK


def test_service_navigate_to_unsafe_url_blocked() -> None:
    svc = _make_service()
    svc.open()
    res = svc.navigate("javascript:alert(1)")
    assert res.status == BrowserResultStatus.BLOCKED


def test_service_back_forward_reload() -> None:
    svc = _open_with_fixture(_make_service())
    assert svc.navigate("https://a").status == BrowserResultStatus.OK
    assert svc.back().status == BrowserResultStatus.OK
    assert svc.forward().status == BrowserResultStatus.OK
    assert svc.reload().status == BrowserResultStatus.OK


def test_service_click_resolves_dom() -> None:
    svc = _open_with_fixture(_make_service())
    res = svc.click(BrowserTarget(kind=LocatorKind.TEST_ID, value="submit-btn"))
    assert res.status == BrowserResultStatus.OK


def test_service_click_target_not_found() -> None:
    svc = _open_with_fixture(_make_service())
    res = svc.click(BrowserTarget(kind=LocatorKind.CSS, value="#nope"))
    assert res.status == BrowserResultStatus.TARGET_NOT_FOUND


def test_service_type_text() -> None:
    svc = _open_with_fixture(_make_service())
    res = svc.type_text(
        BrowserTarget(kind=LocatorKind.CSS, value="#u"),
        "alice",
    )
    assert res.status == BrowserResultStatus.OK
    inp = svc._sessions[DEFAULT_SESSION_ID]._page.dom.find_css("#u")[0]  # type: ignore[attr-defined]
    assert inp.attrs.get("value") == "alice"


def test_service_press_safe_key() -> None:
    svc = _open_with_fixture(_make_service())
    assert svc.press("Enter").status == BrowserResultStatus.OK


def test_service_press_unsafe_key_rejected() -> None:
    svc = _open_with_fixture(_make_service())
    res = svc.press("; rm -rf /")
    # The session-level safe-key check raises a session error which
    # the router classifies.  Either ERROR or BLOCKED is acceptable.
    assert res.status in (
        BrowserResultStatus.ERROR, BrowserResultStatus.BLOCKED,
    )


def test_service_hover() -> None:
    svc = _open_with_fixture(_make_service())
    res = svc.hover(BrowserTarget(kind=LocatorKind.CSS, value="#u"))
    assert res.status == BrowserResultStatus.OK


def test_service_scroll() -> None:
    svc = _open_with_fixture(_make_service())
    res = svc.scroll(direction="down", amount=200)
    assert res.status == BrowserResultStatus.OK


def test_service_scroll_invalid_direction() -> None:
    svc = _open_with_fixture(_make_service())
    res = svc.scroll(direction="north-west", amount=10)
    assert res.status in (
        BrowserResultStatus.ERROR, BrowserResultStatus.INVALID_REQUEST,
    )


def test_service_select_by_value() -> None:
    svc = _open_with_fixture(_make_service())
    res = svc.select(
        BrowserTarget(kind=LocatorKind.CSS, value="#country"),
        value="ca",
    )
    assert res.status == BrowserResultStatus.OK


def test_service_select_by_label() -> None:
    svc = _open_with_fixture(_make_service())
    res = svc.select(
        BrowserTarget(kind=LocatorKind.CSS, value="#country"),
        label="Mexico",
    )
    assert res.status == BrowserResultStatus.OK


def test_service_select_requires_exactly_one() -> None:
    svc = _open_with_fixture(_make_service())
    res = svc.select(
        BrowserTarget(kind=LocatorKind.CSS, value="#country"),
        value="ca", label="Mexico",
    )
    assert res.status == BrowserResultStatus.INVALID_REQUEST


def test_service_wait_until_visible() -> None:
    svc = _open_with_fixture(_make_service())
    res = svc.wait(
        until="visible",
        target=BrowserTarget(kind=LocatorKind.TEST_ID, value="submit-btn"),
        timeout_ms=1_000,
    )
    assert res.status == BrowserResultStatus.OK


def test_service_wait_until_networkidle() -> None:
    svc = _open_with_fixture(_make_service())
    res = svc.wait(until="networkidle", timeout_ms=1_000)
    assert res.status == BrowserResultStatus.OK


def test_service_extract_text() -> None:
    svc = _open_with_fixture(_make_service())
    res = svc.extract_text(
        BrowserTarget(kind=LocatorKind.TEST_ID, value="submit-btn"),
        max_chars=64,
    )
    assert res.status == BrowserResultStatus.OK
    assert res.observation is not None
    assert "Sign" in res.observation.extracted_text


def test_service_extract_page() -> None:
    svc = _open_with_fixture(_make_service())
    res = svc.extract_page(max_chars=128)
    assert res.status == BrowserResultStatus.OK
    assert res.observation is not None
    assert res.observation.state is not None
    assert res.observation.state.url.startswith("file:///")


def test_service_download_path_validation() -> None:
    """The service routes DOWNLOAD through the safety policy."""
    policy = BrowserSafetyPolicy(allow_executable_downloads=False)
    svc = _make_service(policy=policy)
    svc.open()
    res = svc.download(
        BrowserTarget(kind=LocatorKind.CSS, value="a"),
        save_to="C:/tmp/payload.exe",
    )
    assert res.status == BrowserResultStatus.BLOCKED


# ---------------------------------------------------------------------------
# execute() — low-level entry
# ---------------------------------------------------------------------------

def test_execute_returns_browser_result() -> None:
    svc = _open_with_fixture(_make_service())
    req = BrowserRequest(
        action=BrowserAction.NAVIGATE,
        parameters={"url": "https://example.com"},
    )
    res = svc.execute(req)
    assert isinstance(res, BrowserResult)
    assert res.status == BrowserResultStatus.OK


def test_execute_propagates_goal_and_step_ids() -> None:
    svc = _open_with_fixture(_make_service())
    req = BrowserRequest(
        action=BrowserAction.NAVIGATE,
        parameters={"url": "https://example.com"},
        goal_id="goal-1",
        plan_step_id="step-2",
    )
    res = svc.execute(req)
    assert res.request.goal_id == "goal-1"
    assert res.request.plan_step_id == "step-2"


def test_execute_rejects_non_request() -> None:
    svc = _open_with_fixture(_make_service())
    with pytest.raises(TypeError):
        svc.execute("not a request")  # type: ignore[arg-type]


def test_execute_rejects_unknown_param_key() -> None:
    svc = _open_with_fixture(_make_service())
    req = BrowserRequest(
        action=BrowserAction.NAVIGATE,
        parameters={"url": "https://example.com", "evil": "x"},
    )
    res = svc.execute(req)
    assert res.status == BrowserResultStatus.INVALID_REQUEST
    assert "evil" in (res.error or "")


# ---------------------------------------------------------------------------
# Session registry
# ---------------------------------------------------------------------------

def test_list_sessions_returns_session_ids() -> None:
    svc = _make_service()
    svc.open(session_id="s1")
    svc.open(session_id="s2")
    infos = svc.list_sessions()
    ids = {info.session_id for info in infos}
    assert "s1" in ids
    assert "s2" in ids


def test_get_session_info_returns_structured_state() -> None:
    svc = _make_service()
    svc.open(viewport=(900, 700))
    info = svc.get_session_info(DEFAULT_SESSION_ID)
    assert info is not None
    assert info.is_open is True
    assert info.viewport == (900, 700)
    assert info.headless is True


def test_get_session_info_returns_none_for_unknown() -> None:
    svc = _make_service()
    assert svc.get_session_info("nope") is None


# ---------------------------------------------------------------------------
# Safety pins
# ---------------------------------------------------------------------------

def test_service_module_does_not_import_subprocess() -> None:
    """The service must not import subprocess / os.system / os.popen."""
    import core.services.browser_service as m
    src = open(m.__file__, encoding="utf-8").read()
    forbidden = [
        "import subprocess",
        "from subprocess",
        "os.system(",
        "os.popen(",
    ]
    for f in forbidden:
        assert f not in src, f"forbidden import/call in service: {f!r}"


def test_service_module_uses_loguru_only() -> None:
    """No stdlib logging; everything goes through loguru."""
    import core.services.browser_service as m
    src = open(m.__file__, encoding="utf-8").read()
    assert "import logging" not in src
    assert "from logging" not in src
    assert "from loguru" in src


# ---------------------------------------------------------------------------
# Describe / health
# ---------------------------------------------------------------------------

def test_describe_returns_capability_shape() -> None:
    svc = _make_service()
    d = svc.describe()
    assert isinstance(d, dict)
    assert d.get("service") == "browser"
    assert "engine" in d
    assert "policy" in d
    assert "open_sessions" in d


def test_is_healthy_true_when_default() -> None:
    svc = _make_service()
    assert svc.is_healthy() is True


# ---------------------------------------------------------------------------
# Lifecycle surface (R-14 + ServiceRegistry contract)
# ---------------------------------------------------------------------------

def test_service_exposes_lifecycle_methods() -> None:
    """The ServiceRegistry requires initialize/shutdown/statistics."""
    svc = _make_service()
    assert callable(getattr(svc, "initialize", None))
    assert callable(getattr(svc, "shutdown", None))
    assert callable(getattr(svc, "statistics", None))


def test_service_initialize_returns_true() -> None:
    svc = _make_service()
    assert svc.initialize() is True


def test_service_shutdown_when_no_sessions() -> None:
    svc = _make_service()
    assert svc.shutdown() is True


def test_service_shutdown_closes_open_sessions() -> None:
    svc = _make_service()
    svc.open(session_id="s1")
    svc.open(session_id="s2")
    assert svc.shutdown() is True
    # Sessions are popped from the registry on shutdown.
    assert svc.get_session_info("s1") is None
    assert svc.get_session_info("s2") is None


def test_service_statistics_does_not_leak_secrets() -> None:
    svc = _make_service()
    s = svc.statistics()
    assert s["service"] == "browser"
    assert "open_sessions" in s
    blob = json.dumps(s, default=str)
    # No URL, no cookies, no value text.
    for forbidden in ("password", "cookie", "session_token="):
        assert forbidden not in blob.lower(), f"leak: {forbidden!r}"
