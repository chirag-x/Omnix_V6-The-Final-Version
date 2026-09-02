"""
Browser contracts tests (Phase 8).

Verifies the *closed* typed contracts at the browser subsystem
boundary.  No real browser is launched; the contracts are pure
data and must be self-consistent.

These tests confirm:

* Every action has a closed parameter key set.
* Every action in the closed enum is wired into ACTION_PARAM_KEYS.
* Result statuses are a closed set.
* TargetResolutionMethod is a closed set.
* BrowserTarget / BrowserRequest / BrowserResult are frozen.
* with_parameter / with_target builders are pure.
* Observation never claims ``verified`` (R-8).
"""

from __future__ import annotations

import pytest

from browser.models.contracts import (
    ACTION_PARAM_KEYS,
    BROWSER_OBSERVATION_SOURCES,
    BrowserAction,
    BrowserElement,
    BrowserObservation,
    BrowserPageState,
    BrowserRequest,
    BrowserResult,
    BrowserResultStatus,
    BrowserSessionInfo,
    BrowserTarget,
    LocatorKind,
    TargetResolutionMethod,
)


# ---------------------------------------------------------------------------
# Action closed set
# ---------------------------------------------------------------------------

def test_browser_action_is_closed_enum() -> None:
    """The action enum is the only path to mutate the browser."""
    values = {a.value for a in BrowserAction}
    expected = {
        "open", "navigate", "back", "forward", "reload",
        "click", "type", "press", "scroll", "select",
        "hover", "wait", "extract_text", "extract_page",
        "download", "close",
    }
    assert values == expected
    # No duplicates.
    assert len(values) == len(list(BrowserAction))


def test_every_action_has_param_keys() -> None:
    """Every action is in ACTION_PARAM_KEYS (and only those actions)."""
    assert set(ACTION_PARAM_KEYS.keys()) == set(BrowserAction)


def test_action_param_keys_are_tuples_of_strings() -> None:
    """Param keys are immutable tuples of strings."""
    for action, keys in ACTION_PARAM_KEYS.items():
        assert isinstance(keys, tuple)
        for k in keys:
            assert isinstance(k, str)
        # No duplicates within an action.
        assert len(set(keys)) == len(keys)


def test_action_param_keys_have_no_secret_like_names() -> None:
    """The closed sets must not accept arbitrary ``password``/``cookie`` keys."""
    forbidden = {"password", "passwd", "pwd", "secret", "cookie", "session_token"}
    for action, keys in ACTION_PARAM_KEYS.items():
        for k in keys:
            assert k not in forbidden, (
                f"action {action.value!r} accepts forbidden key {k!r}"
            )


# ---------------------------------------------------------------------------
# Result status closed set
# ---------------------------------------------------------------------------

def test_browser_result_status_is_closed_enum() -> None:
    values = {s.value for s in BrowserResultStatus}
    expected = {
        "ok", "target_not_found", "navigation_failed", "timeout",
        "download_failed", "invalid_request", "session_not_found",
        "error", "blocked",
    }
    assert values == expected


# ---------------------------------------------------------------------------
# Locator / Resolution method closed sets
# ---------------------------------------------------------------------------

def test_locator_kind_is_closed_enum() -> None:
    values = {k.value for k in LocatorKind}
    expected = {"accessibility", "css", "text", "xpath", "test_id"}
    assert values == expected
    # Vision is NOT a LocatorKind (per spec).
    assert "vision" not in values
    assert "image" not in values


def test_target_resolution_method_is_closed_enum() -> None:
    values = {m.value for m in TargetResolutionMethod}
    expected = {
        "dom", "accessibility", "vision_fallback", "unresolved", "skipped",
    }
    assert values == expected


def test_observation_sources_is_closed() -> None:
    assert isinstance(BROWSER_OBSERVATION_SOURCES, tuple)
    assert set(BROWSER_OBSERVATION_SOURCES) == {
        "DOM", "ACCESSIBILITY", "URL", "TITLE", "TEXT",
        "DOWNLOAD", "ERROR",
    }


# ---------------------------------------------------------------------------
# Frozen / immutability (R-10)
# ---------------------------------------------------------------------------

def test_browser_target_is_frozen() -> None:
    t = BrowserTarget(kind=LocatorKind.CSS, value="#x")
    with pytest.raises(Exception):
        t.value = "no"  # type: ignore[misc]


def test_browser_request_is_frozen() -> None:
    r = BrowserRequest(action=BrowserAction.OPEN)
    with pytest.raises(Exception):
        r.action = BrowserAction.CLOSE  # type: ignore[misc]


def test_browser_result_is_frozen() -> None:
    r = BrowserResult(
        status=BrowserResultStatus.OK,
        action=BrowserAction.OPEN,
        request=BrowserRequest(action=BrowserAction.OPEN),
    )
    with pytest.raises(Exception):
        r.status = BrowserResultStatus.ERROR  # type: ignore[misc]


def test_browser_observation_is_frozen() -> None:
    o = BrowserObservation(source="URL")
    with pytest.raises(Exception):
        o.source = "ERROR"  # type: ignore[misc]


def test_browser_page_state_is_frozen() -> None:
    p = BrowserPageState(url="https://example.com")
    with pytest.raises(Exception):
        p.url = "https://other"  # type: ignore[misc]


def test_browser_element_is_frozen() -> None:
    e = BrowserElement(tag="button")
    with pytest.raises(Exception):
        e.tag = "a"  # type: ignore[misc]


def test_browser_session_info_is_frozen() -> None:
    s = BrowserSessionInfo(session_id="s", is_open=True)
    with pytest.raises(Exception):
        s.is_open = False  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Builders (R-10)
# ---------------------------------------------------------------------------

def test_request_with_parameter_is_pure() -> None:
    """with_parameter returns a new instance, leaves the original untouched."""
    r1 = BrowserRequest(action=BrowserAction.NAVIGATE)
    r2 = r1.with_parameter("url", "https://example.com")
    assert r1.parameters == {}
    assert r2.parameters["url"] == "https://example.com"


def test_request_with_target_is_pure() -> None:
    r1 = BrowserRequest(action=BrowserAction.CLICK)
    t = BrowserTarget(kind=LocatorKind.CSS, value="#go")
    r2 = r1.with_target(t)
    assert r1.target is None
    assert r2.target == t


def test_result_with_observation_is_pure() -> None:
    r1 = BrowserResult(
        status=BrowserResultStatus.OK,
        action=BrowserAction.OPEN,
        request=BrowserRequest(action=BrowserAction.OPEN),
    )
    obs = BrowserObservation(source="URL")
    r2 = r1.with_observation(obs)
    assert r1.observation is None
    assert r2.observation is obs


def test_result_with_error_is_pure() -> None:
    r1 = BrowserResult(
        status=BrowserResultStatus.OK,
        action=BrowserAction.OPEN,
        request=BrowserRequest(action=BrowserAction.OPEN),
    )
    r2 = r1.with_error(
        BrowserResultStatus.ERROR, "boom"
    )
    assert r1.status == BrowserResultStatus.OK
    assert r2.status == BrowserResultStatus.ERROR
    assert r2.error == "boom"


def test_result_with_metadata_is_pure() -> None:
    r1 = BrowserResult(
        status=BrowserResultStatus.OK,
        action=BrowserAction.OPEN,
        request=BrowserRequest(action=BrowserAction.OPEN),
    )
    r2 = r1.with_metadata(saved_to="/tmp/x.png")
    assert r1.metadata == {}
    assert r2.metadata["saved_to"] == "/tmp/x.png"


# ---------------------------------------------------------------------------
# Observation / Verifier honesty (R-8)
# ---------------------------------------------------------------------------

def test_observation_ok_property_distinguishes_error_source() -> None:
    o1 = BrowserObservation(source="URL")
    o2 = BrowserObservation(source="ERROR", error="oops")
    o3 = BrowserObservation(source="URL", error="transient")
    assert o1.ok is True
    assert o2.ok is False
    # If the source is not ERROR but an error is set, it's still
    # flagged as not ok (the Brain / Verifier must inspect).
    assert o3.ok is False


def test_observation_does_not_claim_verified() -> None:
    """R-8: observation is *observational*, not *verifying*."""
    o = BrowserObservation(source="DOM")
    # Observation has no ``verified`` field at all.
    assert not hasattr(o, "verified")
    assert "verified" not in o.to_dict()


# ---------------------------------------------------------------------------
# to_dict round-trips
# ---------------------------------------------------------------------------

def test_request_to_dict_omits_empty_fields() -> None:
    r = BrowserRequest(action=BrowserAction.OPEN)
    d = r.to_dict()
    assert d == {"action": "open"}


def test_request_to_dict_includes_goal_and_step() -> None:
    r = BrowserRequest(
        action=BrowserAction.NAVIGATE,
        goal_id="g1",
        plan_step_id="s1",
    )
    d = r.to_dict()
    assert d["goal_id"] == "g1"
    assert d["plan_step_id"] == "s1"


def test_target_to_dict_includes_nth_when_set() -> None:
    t = BrowserTarget(kind=LocatorKind.CSS, value="a", nth=2)
    d = t.to_dict()
    assert d["nth"] == 2


def test_target_to_dict_includes_strict_when_true() -> None:
    t = BrowserTarget(kind=LocatorKind.TEXT, value="Go", strict=True)
    d = t.to_dict()
    assert d["strict"] is True


# ---------------------------------------------------------------------------
# Realistic usage: every action can be wrapped in a request
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("action", list(BrowserAction))
def test_every_action_constructs_a_request(action: BrowserAction) -> None:
    r = BrowserRequest(action=action)
    assert r.action is action
    assert r.parameters == {}
    assert r.target is None
    # to_dict must always produce an action key.
    assert r.to_dict()["action"] == action.value
