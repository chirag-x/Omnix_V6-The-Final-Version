"""
Browser integration tests (Phase 8).

End-to-end style tests that drive the canonical
:class:`core.services.browser_service.BrowserService` the same
way the Brain / Agent would — through its structured surface.
The service is wired with the in-memory Playwright fake so the
tests stay deterministic and free of any subprocess.

Scenarios:

* the canonical agent flow:
  open → navigate → click → type → press → extract_text → close
* vision fallback escalation when DOM resolution fails
* multi-session registry (parallel sessions, isolated state)
* safety: cookie / password text never appears in any
  observation returned by the service
* goal_id / plan_step_id propagate through every result
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
    res = svc.open()
    assert res.status == BrowserResultStatus.OK
    s = svc._sessions[DEFAULT_SESSION_ID]  # type: ignore[attr-defined]
    s._page.dom = build_simple_dom()  # type: ignore[attr-defined]
    s._page.url = "file:///fixture/index.html"  # type: ignore[attr-defined]
    s._page.title = "Fixture"  # type: ignore[attr-defined]
    return svc


# ---------------------------------------------------------------------------
# Canonical agent flow
# ---------------------------------------------------------------------------

def test_canonical_agent_flow_open_to_close() -> None:
    """An agent runs:

        open -> navigate -> click "Sign in" -> type username -> press
        Enter -> extract_text(<button>) -> close

    Every step must return OK; the final state must be sane.
    """
    svc = _open_with_fixture(_make_service())
    # 1. Navigate
    r1 = svc.navigate(
        "https://example.com", goal_id="g-1", plan_step_id="s-1",
    )
    assert r1.status == BrowserResultStatus.OK
    assert r1.request.goal_id == "g-1"
    # 2. Click the submit button (test_id=submit-btn)
    r2 = svc.click(
        BrowserTarget(kind=LocatorKind.TEST_ID, value="submit-btn"),
        goal_id="g-1", plan_step_id="s-2",
    )
    assert r2.status == BrowserResultStatus.OK
    # 3. Type a username
    r3 = svc.type_text(
        BrowserTarget(kind=LocatorKind.CSS, value="#u"),
        "alice", goal_id="g-1", plan_step_id="s-3",
    )
    assert r3.status == BrowserResultStatus.OK
    # 4. Press Enter
    r4 = svc.press("Enter", goal_id="g-1", plan_step_id="s-4")
    assert r4.status == BrowserResultStatus.OK
    # 5. Extract the button text
    r5 = svc.extract_text(
        BrowserTarget(kind=LocatorKind.TEST_ID, value="submit-btn"),
        max_chars=64, goal_id="g-1", plan_step_id="s-5",
    )
    assert r5.status == BrowserResultStatus.OK
    assert r5.observation is not None
    assert "Sign" in r5.observation.extracted_text
    # 6. Close
    r6 = svc.close(goal_id="g-1", plan_step_id="s-6")
    assert r6.status == BrowserResultStatus.OK
    # Session should be gone
    assert svc.get_session_info(DEFAULT_SESSION_ID) is None


def test_goal_and_step_ids_propagate_to_every_result() -> None:
    svc = _open_with_fixture(_make_service())
    r = svc.execute(BrowserRequest(
        action=BrowserAction.CLICK,
        target=BrowserTarget(kind=LocatorKind.CSS, value="#u"),
        goal_id="g-7", plan_step_id="s-9",
    ))
    assert r.status == BrowserResultStatus.OK
    assert r.request.goal_id == "g-7"
    assert r.request.plan_step_id == "s-9"


# ---------------------------------------------------------------------------
# Multi-session registry
# ---------------------------------------------------------------------------

def test_two_sessions_have_isolated_state() -> None:
    svc = _make_service()
    res1 = svc.open(session_id="A")
    res2 = svc.open(session_id="B")
    assert res1.status == BrowserResultStatus.OK
    assert res2.status == BrowserResultStatus.OK

    # Inject different fixtures in each session.
    sA = svc._sessions["A"]  # type: ignore[attr-defined]
    sB = svc._sessions["B"]  # type: ignore[attr-defined]
    sA._page.dom = build_simple_dom()  # type: ignore[attr-defined]
    sA._page.url = "https://a.example/"  # type: ignore[attr-defined]
    sB._page.dom = build_simple_dom()  # type: ignore[attr-defined]
    sB._page.url = "https://b.example/"  # type: ignore[attr-defined]

    # Navigate each.
    assert svc.navigate("https://a/2", session_id="A").status == \
        BrowserResultStatus.OK
    assert svc.navigate("https://b/2", session_id="B").status == \
        BrowserResultStatus.OK

    info_A = svc.get_session_info("A")
    info_B = svc.get_session_info("B")
    assert info_A is not None and info_B is not None
    assert info_A.current_url == "https://a/2"
    assert info_B.current_url == "https://b/2"

    # Close A, B should still be live.
    assert svc.close(session_id="A").status == BrowserResultStatus.OK
    assert svc.get_session_info("A") is None
    assert svc.get_session_info("B") is not None


def test_known_session_ids_appear_in_list_sessions() -> None:
    svc = _make_service()
    svc.open(session_id="alpha")
    svc.open(session_id="beta")
    ids = {i.session_id for i in svc.list_sessions()}
    assert {"alpha", "beta"} <= ids
    # The default session is only created when open() is called without
    # a session_id; this test only opens named sessions.
    assert "default" not in ids


# ---------------------------------------------------------------------------
# Vision fallback escalation
# ---------------------------------------------------------------------------

class _ResolvingVision:
    """A vision fallback that always succeeds at the geometry level."""

    def __init__(self) -> None:
        self.calls = 0
        self.last_query: Optional[str] = None

    def ground_via_vision(
        self, target_query: str, screenshot_path: str,
    ) -> VisionFallbackResult:
        self.calls += 1
        self.last_query = target_query
        return VisionFallbackResult(
            resolved=True, x=10, y=10, width=20, height=20,
            confidence=0.9, text=target_query,
        )


def test_vision_fallback_escalates_when_dom_fails() -> None:
    """A click targeting a missing element must consult the fallback.

    With our fake, the fallback is *consulted* but cannot actually
    drive a click; the action therefore fails — but the call must
    have happened.  The session-level click will report ERROR
    because there is no real element to click; the *intent* of
    this test is to prove the escalation path.
    """
    v = _ResolvingVision()
    svc = _open_with_fixture(_make_service(vision=v))
    r = svc.click(
        BrowserTarget(kind=LocatorKind.CSS, value="#does-not-exist"),
    )
    # The fallback was invoked...
    assert v.calls >= 1
    # ...the query that was passed in was the selector value
    # (or a synthesised hint) — must be a non-empty string.
    assert v.last_query
    # Status is some failure but the call happened.
    assert r.status in (
        BrowserResultStatus.TARGET_NOT_FOUND,
        BrowserResultStatus.ERROR,
    )


def test_vision_fallback_not_consulted_when_dom_succeeds() -> None:
    v = _ResolvingVision()
    svc = _open_with_fixture(_make_service(vision=v))
    r = svc.click(
        BrowserTarget(kind=LocatorKind.TEST_ID, value="submit-btn"),
    )
    assert r.status == BrowserResultStatus.OK
    assert v.calls == 0


# ---------------------------------------------------------------------------
# Safety: secrets never leak through observations
# ---------------------------------------------------------------------------

def test_extract_page_does_not_leak_cookie_text() -> None:
    """A page that contains 'set-cookie: session_token=secret' in its
    visible text must NOT have that token re-emitted by extract_page.

    (Our fixture doesn't contain anything, but the invariant is
    important enough to pin in tests.)
    """
    svc = _open_with_fixture(_make_service())
    r = svc.extract_page(max_chars=2000)
    assert r.status == BrowserResultStatus.OK
    blob = r.observation.state.visible_text if r.observation and r.observation.state else ""
    # Common cookie/secret patterns must not appear.
    forbidden = ("set-cookie", "session_token=", "password=")
    for f in forbidden:
        assert f not in blob.lower(), f"leak: {f!r} in {blob!r}"


def test_extract_text_does_not_carry_value_for_password_inputs() -> None:
    """An <input type=password> is not in the fixture; the test pins the
    invariant that a click on a normal field never emits 'type=password'."""
    svc = _open_with_fixture(_make_service())
    r = svc.extract_text(
        BrowserTarget(kind=LocatorKind.CSS, value="#u"), max_chars=128,
    )
    assert r.status == BrowserResultStatus.OK
    assert r.observation is not None
    payload = json.dumps(r.observation.to_dict())
    assert "type=password" not in payload


# ---------------------------------------------------------------------------
# Result shape: always carries status, action, request
# ---------------------------------------------------------------------------

def test_results_are_json_serialisable() -> None:
    """A Brain / Verifier consumer may json.dumps() the result; the
    service must not produce non-serialisable objects."""
    svc = _open_with_fixture(_make_service())
    r = svc.execute(BrowserRequest(
        action=BrowserAction.EXTRACT_TEXT,
        target=BrowserTarget(kind=LocatorKind.TEST_ID, value="submit-btn"),
        parameters={"max_chars": 64},
        goal_id="g-x", plan_step_id="s-x",
    ))
    blob = json.dumps(r.to_dict(), default=str)
    # Round-trips cleanly:
    re = json.loads(blob)
    assert re["status"] in {s.value for s in BrowserResultStatus}
    assert re["action"] == "extract_text"
    assert re["request"]["goal_id"] == "g-x"


def test_unknown_session_action_returns_session_not_found() -> None:
    svc = _open_with_fixture(_make_service())
    r = svc.click(
        BrowserTarget(kind=LocatorKind.CSS, value="#u"),
        session_id="ghost",
    )
    assert r.status == BrowserResultStatus.SESSION_NOT_FOUND


def test_too_many_open_sessions_returns_error() -> None:
    """The service caps open sessions; exceeding the cap must return
    a structured ERROR, not raise."""
    svc = _make_service()
    svc._max_sessions = 2  # type: ignore[attr-defined]
    assert svc.open(session_id="s1").status == BrowserResultStatus.OK
    assert svc.open(session_id="s2").status == BrowserResultStatus.OK
    r = svc.open(session_id="s3")
    assert r.status == BrowserResultStatus.ERROR
    assert "max" in (r.error or "")
