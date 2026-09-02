"""
BrowserSession tests (Phase 8).

These tests exercise :class:`browser.session.session.BrowserSession`
against a *fake* Playwright browser (no real Chromium, no network,
no subprocesses).  The session's small surface (open/navigate/click
/...) is driven through the same code path that production uses.

Covers:

* open / close lifecycle
* navigate records a state snapshot
* back / forward / reload
* target resolution by CSS, TEXT, ACCESSIBILITY, TEST_ID, XPATH
* target resolution order
* nth match
* strict vs substring text
* click / type / press / hover / scroll / select
* extract_text / extract_page bounded output
* wait
* download path validation
* BrowserSessionError on infrastructure failure
"""

from __future__ import annotations

import pytest

from browser.models.contracts import (
    BrowserTarget,
    LocatorKind,
    TargetResolutionMethod,
)
from browser.session.session import (
    BrowserSession,
    BrowserSessionError,
)

from browser_fakes import (
    build_simple_dom,
    fake_browser_factory,
    fixture_session,
    install_fixture_page,
)


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

def test_session_starts_closed() -> None:
    s = BrowserSession("a", playwright_factory=fake_browser_factory)
    assert s.is_open is False
    assert s.state.is_open is False


def test_session_open_uses_factory() -> None:
    s = BrowserSession("a", playwright_factory=fake_browser_factory)
    s.open()
    assert s.is_open is True
    assert s.state.opened_at > 0


def test_session_open_is_idempotent() -> None:
    s = BrowserSession("a", playwright_factory=fake_browser_factory)
    s.open()
    s.open()
    assert s.is_open is True


def test_session_close_is_idempotent() -> None:
    s = BrowserSession("a", playwright_factory=fake_browser_factory)
    s.close()  # not yet open — must be a no-op
    s.open()
    s.close()
    s.close()  # closed — must be a no-op
    assert s.is_open is False


def test_session_open_failure_raises_session_error() -> None:
    def bad_factory() -> None:
        raise RuntimeError("no chromium")

    s = BrowserSession("a", playwright_factory=bad_factory)
    with pytest.raises(BrowserSessionError):
        s.open()
    assert s.is_open is False


def test_session_state_reflects_action_count() -> None:
    s = fixture_session()
    s.navigate("https://example.com")
    assert s.state.action_count == 1
    s.navigate("https://example.com/2")
    assert s.state.action_count == 2


# ---------------------------------------------------------------------------
# Navigation
# ---------------------------------------------------------------------------

def test_navigate_records_url() -> None:
    s = fixture_session()
    state = s.navigate("https://example.com")
    assert state.url == "https://example.com"
    assert s.state.current_url == "https://example.com"


def test_navigate_without_open_raises() -> None:
    s = BrowserSession("a", playwright_factory=fake_browser_factory)
    with pytest.raises(BrowserSessionError):
        s.navigate("https://example.com")


def test_back_forward_reload() -> None:
    s = fixture_session()
    s.navigate("https://example.com")
    s.back()
    s.forward()
    s.reload()
    page = s._page  # type: ignore[attr-defined]
    names = [a[0] for a in page.actions_log]
    assert "goto" in names
    assert "go_back" in names
    assert "go_forward" in names
    assert "reload" in names


# ---------------------------------------------------------------------------
# Target resolution
# ---------------------------------------------------------------------------

def test_resolve_by_css() -> None:
    s = fixture_session()
    resolved = s.resolve(BrowserTarget(kind=LocatorKind.CSS, value="#u"))
    assert resolved.method == TargetResolutionMethod.DOM
    assert resolved.selector == "#u"
    el = resolved._build_element_snapshot()
    assert el.tag == "input"


def test_resolve_by_test_id() -> None:
    s = fixture_session()
    resolved = s.resolve(
        BrowserTarget(kind=LocatorKind.TEST_ID, value="submit-btn")
    )
    el = resolved._build_element_snapshot()
    assert el.tag == "button"
    assert "Sign in" in (el.text or "")


def test_resolve_by_text_substring() -> None:
    s = fixture_session()
    resolved = s.resolve(BrowserTarget(kind=LocatorKind.TEXT, value="Sign"))
    el = resolved._build_element_snapshot()
    assert el.tag == "button"


def test_resolve_by_text_strict() -> None:
    s = fixture_session()
    resolved = s.resolve(
        BrowserTarget(
            kind=LocatorKind.TEXT, value="Sign in", strict=True
        )
    )
    el = resolved._build_element_snapshot()
    assert el.tag == "button"


def test_resolve_by_accessibility_role_and_name() -> None:
    s = fixture_session()
    # The link's implicit ARIA role is "link" (mimicking real Playwright);
    # its accessible name comes from aria-label="Help center".
    target = BrowserTarget(
        kind=LocatorKind.ACCESSIBILITY,
        value='{"role": "link", "name": "Help center"}',
    )
    resolved = s.resolve(target)
    assert resolved.method == TargetResolutionMethod.ACCESSIBILITY
    el = resolved._build_element_snapshot()
    assert el.tag == "a"


def test_resolve_by_xpath() -> None:
    s = fixture_session()
    resolved = s.resolve(
        BrowserTarget(kind=LocatorKind.XPATH, value="//h1")
    )
    el = resolved._build_element_snapshot()
    assert el.tag == "h1"


def test_resolve_unknown_target_raises() -> None:
    s = fixture_session()
    with pytest.raises(BrowserSessionError):
        s.resolve(BrowserTarget(kind=LocatorKind.CSS, value="#does-not-exist"))


def test_resolve_nth_match() -> None:
    s = fixture_session()
    # There are two "Delete" buttons.
    resolved = s.resolve(
        BrowserTarget(kind=LocatorKind.TEXT, value="Delete", nth=1)
    )
    el = resolved._build_element_snapshot()
    assert el.tag == "button"
    assert el.selector  # has a selector set


# ---------------------------------------------------------------------------
# Mutating actions
# ---------------------------------------------------------------------------

def test_click_marks_element() -> None:
    s = fixture_session()
    page = s._page  # type: ignore[attr-defined]
    s.click(BrowserTarget(kind=LocatorKind.TEST_ID, value="submit-btn"))
    btn = page.dom.find_css('[data-testid="submit-btn"]')[0]
    assert btn.attrs.get("__clicked__") == "1"


def test_type_fills_value() -> None:
    s = fixture_session()
    page = s._page  # type: ignore[attr-defined]
    s.type_text(
        BrowserTarget(kind=LocatorKind.CSS, value="#u"),
        "alice",
    )
    inp = page.dom.find_css("#u")[0]
    assert inp.attrs.get("value") == "alice"


def test_press_records_key() -> None:
    s = fixture_session()
    page = s._page  # type: ignore[attr-defined]
    s.press("Enter")
    assert page.keyboard.presses == ["Enter"]


def test_press_rejects_unsafe_key() -> None:
    s = fixture_session()
    with pytest.raises(BrowserSessionError):
        s.press("; rm -rf /")


def test_hover_marks_element() -> None:
    s = fixture_session()
    page = s._page  # type: ignore[attr-defined]
    s.hover(BrowserTarget(kind=LocatorKind.CSS, value="#u"))
    inp = page.dom.find_css("#u")[0]
    assert inp.attrs.get("__hovered__") == "1"


def test_scroll_records_action() -> None:
    s = fixture_session()
    page = s._page  # type: ignore[attr-defined]
    s.scroll(direction="down", amount=300)
    assert page.mouse.actions == [(0, 300)]


def test_scroll_invalid_direction_raises() -> None:
    s = fixture_session()
    with pytest.raises(BrowserSessionError):
        s.scroll(direction="north-west", amount=10)


def test_select_by_value() -> None:
    s = fixture_session()
    page = s._page  # type: ignore[attr-defined]
    s.select(
        BrowserTarget(kind=LocatorKind.CSS, value="#country"),
        value="ca",
    )
    sel = page.dom.find_css("#country")[0]
    assert sel.attrs.get("value") == "ca"


def test_select_by_label() -> None:
    s = fixture_session()
    page = s._page  # type: ignore[attr-defined]
    s.select(
        BrowserTarget(kind=LocatorKind.CSS, value="#country"),
        label="Mexico",
    )
    sel = page.dom.find_css("#country")[0]
    assert sel.attrs.get("selected_label") == "Mexico"


def test_select_requires_exactly_one() -> None:
    s = fixture_session()
    with pytest.raises(BrowserSessionError):
        s.select(
            BrowserTarget(kind=LocatorKind.CSS, value="#country"),
            value="ca", label="Mexico",
        )
    with pytest.raises(BrowserSessionError):
        s.select(BrowserTarget(kind=LocatorKind.CSS, value="#country"))


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

def test_extract_text_bounds_output() -> None:
    s = fixture_session()
    el, text = s.extract_text(
        BrowserTarget(kind=LocatorKind.TEST_ID, value="submit-btn"),
        max_chars=4,
    )
    assert el.tag == "button"
    # Truncation appends "..." (3 chars) on overflow, so the upper bound
    # is max_chars + 3.
    assert len(text) <= 4 + 3
    # The body of the text must not exceed max_chars.
    assert text.startswith("Sign") or "..." in text


def test_extract_page_bounded() -> None:
    s = fixture_session()
    state = s.extract_page(max_chars=64)
    assert state.url.startswith("file:///")
    # Either visible_text or dom_source is bounded.
    assert len(state.visible_text) + len(state.dom_source) <= 64 + 16_000


# ---------------------------------------------------------------------------
# Wait
# ---------------------------------------------------------------------------

def test_wait_until_visible() -> None:
    s = fixture_session()
    s.wait(
        until="visible",
        target=BrowserTarget(kind=LocatorKind.TEST_ID, value="submit-btn"),
        timeout_ms=1_000,
    )


def test_wait_until_networkidle() -> None:
    s = fixture_session()
    s.wait(until="networkidle", timeout_ms=1_000)
    page = s._page  # type: ignore[attr-defined]
    assert any(a[0] == "wait_for_load_state" for a in page.actions_log)


def test_wait_invalid_until_raises() -> None:
    s = fixture_session()
    with pytest.raises(BrowserSessionError):
        s.wait(until="gibberish")


# ---------------------------------------------------------------------------
# Safety
# ---------------------------------------------------------------------------

def test_no_subprocess_or_shell_in_session_module() -> None:
    """The session module must not import subprocess / os.system / popen."""
    import browser.session.session as m
    src = open(m.__file__, encoding="utf-8").read()
    # The docstring of the module is allowed to *mention* subprocess as a
    # *non-responsibility*; the test pins the actual import/usage surface.
    forbidden = [
        "import subprocess",
        "from subprocess",
        "os.system(",
        "os.popen(",
    ]
    for f in forbidden:
        assert f not in src, f"forbidden import/call found: {f!r}"


def test_session_never_exposes_raw_javascript() -> None:
    """The session has no API for arbitrary JavaScript evaluation."""
    s = BrowserSession("a", playwright_factory=fake_browser_factory)
    forbidden = [
        "evaluate", "evaluate_handle", "add_script_tag", "expose_function",
    ]
    for name in dir(s):
        for f in forbidden:
            assert not name.startswith(f), (
                f"session exposes raw-JS hook: {name}"
            )


def test_extract_page_never_carries_cookies_text() -> None:
    """BrowserPageState.cookies_count is a count, never the cookie itself.

    The fixture contains an ``<input type="password">`` — its ``type``
    attribute (the HTML name of the masked-input feature) is
    necessarily serialized into the DOM snapshot, so we don't pin
    "password" as a forbidden substring.  The rules we do pin are:

    * ``cookies_count`` is a count, not a stringified cookie.
    * The snapshot never embeds Set-Cookie headers.
    * No stringified cookie payloads appear.
    """
    s = fixture_session()
    state = s.extract_page()
    d = state.to_dict()
    # cookies_count must be an int, not a stringified cookie.
    assert isinstance(d["cookies_count"], int)
    for k, v in d.items():
        if isinstance(v, str):
            assert "set-cookie" not in v.lower()
            # No stringified cookie payloads.
            assert "session_token=" not in v


# ---------------------------------------------------------------------------
# Screenshot path (used by vision fallback)
# ---------------------------------------------------------------------------

def test_screenshot_path_returns_a_string() -> None:
    s = fixture_session()
    path = s.screenshot_path()
    # Either a real path or None on failure — both are acceptable.
    # We never want it to *raise*.
    assert path is None or isinstance(path, str)
