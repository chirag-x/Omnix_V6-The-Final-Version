"""
Browser safety policy tests (Phase 8).

The policy is the *only* gate between the V6 process and the
browser subsystem.  These tests pin the policy's behaviour:

* URL scheme allowlist is closed (javascript: is rejected).
* Host allowlist is honoured when set.
* Action count cap is honoured.
* Per-action parameter key set is enforced.
* Executable download extensions are refused by default.
* data: URLs are gated behind ``allow_data_urls``.
* file:// URLs are gated behind ``allow_file_urls``.
* about:blank is gated behind ``allow_about_blank``.
* URL length cap is honoured.
"""

from __future__ import annotations

import pytest

from browser.models.contracts import (
    BrowserAction,
    BrowserRequest,
    BrowserResultStatus,
)
from browser.safety.policy import BrowserSafetyPolicy


# ---------------------------------------------------------------------------
# URL scheme allowlist
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "https://example.com",
    "http://example.com/path",
    "about:blank",
    "file:///C:/tmp/page.html",
    "data:text/html,<h1>hi</h1>",
    "chrome://settings",
    "devtools://devtools/bundled/inspector.html",
])
def test_default_policy_allows_safe_schemes(url: str) -> None:
    policy = BrowserSafetyPolicy()
    decision = policy.check_url(url)
    assert decision.allowed, decision.reason


@pytest.mark.parametrize("url", [
    "javascript:alert(1)",
    "javascript:void(0)",
    "JAVASCRIPT:alert(1)",
    "vbscript:msgbox(1)",
    "ftp://example.com/file.txt",
    "ssh://example.com",
    "view-source:https://example.com",
    "blob:https://example.com/abc",
    "ws://example.com/ws",
])
def test_default_policy_blocks_unsafe_schemes(url: str) -> None:
    policy = BrowserSafetyPolicy()
    decision = policy.check_url(url)
    assert not decision.allowed
    assert decision.status == BrowserResultStatus.BLOCKED


# ---------------------------------------------------------------------------
# data: / file: / about: gating
# ---------------------------------------------------------------------------

def test_data_urls_blocked_when_disallowed() -> None:
    policy = BrowserSafetyPolicy(allow_data_urls=False)
    decision = policy.check_url("data:text/html,<h1>x</h1>")
    assert not decision.allowed
    assert "data" in decision.reason.lower()


def test_file_urls_blocked_when_disallowed() -> None:
    policy = BrowserSafetyPolicy(allow_file_urls=False)
    decision = policy.check_url("file:///C:/tmp/page.html")
    assert not decision.allowed
    assert "file" in decision.reason.lower()


def test_about_blank_blocked_when_disallowed() -> None:
    policy = BrowserSafetyPolicy(allow_about_blank=False)
    decision = policy.check_url("about:blank")
    assert not decision.allowed


# ---------------------------------------------------------------------------
# URL length cap
# ---------------------------------------------------------------------------

def test_url_length_cap() -> None:
    policy = BrowserSafetyPolicy(max_url_length=64)
    long_url = "https://example.com/" + ("a" * 100)
    decision = policy.check_url(long_url)
    assert not decision.allowed
    assert "length" in decision.reason.lower()


def test_url_length_cap_disabled_when_none() -> None:
    policy = BrowserSafetyPolicy(max_url_length=None)
    long_url = "https://example.com/" + ("a" * 10_000)
    decision = policy.check_url(long_url)
    assert decision.allowed


# ---------------------------------------------------------------------------
# Host allowlist
# ---------------------------------------------------------------------------

def test_host_allowlist_allows_listed_host() -> None:
    policy = BrowserSafetyPolicy(
        host_allowlist=frozenset({"example.com", "anthropic.com"})
    )
    decision = policy.check_url("https://example.com/x")
    assert decision.allowed


def test_host_allowlist_blocks_unlisted_host() -> None:
    policy = BrowserSafetyPolicy(
        host_allowlist=frozenset({"example.com"})
    )
    decision = policy.check_url("https://other.com/x")
    assert not decision.allowed
    assert "host" in decision.reason.lower()


def test_host_allowlist_is_case_insensitive() -> None:
    policy = BrowserSafetyPolicy(
        host_allowlist=frozenset({"example.com"})
    )
    decision = policy.check_url("https://Example.COM/x")
    assert decision.allowed


# ---------------------------------------------------------------------------
# Per-action request validation
# ---------------------------------------------------------------------------

def test_action_count_cap() -> None:
    policy = BrowserSafetyPolicy(max_actions_per_session=2)
    request = BrowserRequest(action=BrowserAction.EXTRACT_PAGE)
    assert policy.check_request(request, session_action_count=0).allowed
    assert policy.check_request(request, session_action_count=1).allowed
    d = policy.check_request(request, session_action_count=2)
    assert not d.allowed
    assert d.status == BrowserResultStatus.BLOCKED


def test_action_count_cap_disabled() -> None:
    policy = BrowserSafetyPolicy(max_actions_per_session=None)
    request = BrowserRequest(action=BrowserAction.EXTRACT_PAGE)
    for n in (0, 1, 10_000, 1_000_000):
        assert policy.check_request(request, session_action_count=n).allowed


def test_navigate_requires_url_string() -> None:
    policy = BrowserSafetyPolicy()
    r1 = BrowserRequest(action=BrowserAction.NAVIGATE)
    assert not policy.check_request(r1).allowed
    r2 = BrowserRequest(action=BrowserAction.NAVIGATE, parameters={"url": ""})
    assert not policy.check_request(r2).allowed
    r3 = BrowserRequest(action=BrowserAction.NAVIGATE, parameters={"url": 123})
    assert not policy.check_request(r3).allowed


def test_navigate_validates_url_through_check_url() -> None:
    policy = BrowserSafetyPolicy()
    r = BrowserRequest(
        action=BrowserAction.NAVIGATE,
        parameters={"url": "javascript:alert(1)"},
    )
    d = policy.check_request(r)
    assert not d.allowed
    assert d.status == BrowserResultStatus.BLOCKED


def test_open_validates_start_url() -> None:
    policy = BrowserSafetyPolicy()
    r = BrowserRequest(
        action=BrowserAction.OPEN,
        parameters={"start_url": "javascript:alert(1)"},
    )
    d = policy.check_request(r)
    assert not d.allowed


def test_open_with_safe_start_url_allowed() -> None:
    policy = BrowserSafetyPolicy()
    r = BrowserRequest(
        action=BrowserAction.OPEN,
        parameters={"start_url": "https://example.com"},
    )
    assert policy.check_request(r).allowed


# ---------------------------------------------------------------------------
# Closed parameter set
# ---------------------------------------------------------------------------

def test_unknown_param_key_is_rejected() -> None:
    policy = BrowserSafetyPolicy()
    r = BrowserRequest(
        action=BrowserAction.OPEN,
        parameters={"headless": True, "evil": "drop table"},
    )
    d = policy.check_request(r)
    assert not d.allowed
    assert d.status == BrowserResultStatus.INVALID_REQUEST
    assert "evil" in d.reason


def test_known_param_keys_are_accepted() -> None:
    policy = BrowserSafetyPolicy()
    r = BrowserRequest(
        action=BrowserAction.OPEN,
        parameters={"headless": True, "viewport_width": 1280, "viewport_height": 720},
    )
    assert policy.check_request(r).allowed


# ---------------------------------------------------------------------------
# Download safety
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ext", [
    ".exe", ".msi", ".bat", ".cmd", ".ps1", ".sh",
    ".scr", ".com", ".vbs", ".js", ".jse", ".wsf",
    ".dll", ".so", ".app", ".dmg",
])
def test_executable_download_blocked_by_default(ext: str) -> None:
    policy = BrowserSafetyPolicy(allow_executable_downloads=False)
    r = BrowserRequest(
        action=BrowserAction.DOWNLOAD,
        parameters={"save_to": f"C:/tmp/payload{ext}"},
    )
    d = policy.check_request(r)
    assert not d.allowed
    assert d.status == BrowserResultStatus.BLOCKED


def test_executable_download_allowed_when_explicitly_enabled() -> None:
    policy = BrowserSafetyPolicy(allow_executable_downloads=True)
    r = BrowserRequest(
        action=BrowserAction.DOWNLOAD,
        parameters={"save_to": "C:/tmp/payload.exe"},
    )
    assert policy.check_request(r).allowed


def test_download_requires_save_to() -> None:
    policy = BrowserSafetyPolicy()
    r = BrowserRequest(action=BrowserAction.DOWNLOAD, parameters={})
    d = policy.check_request(r)
    assert not d.allowed
    assert d.status == BrowserResultStatus.INVALID_REQUEST


def test_safe_download_extension_allowed() -> None:
    policy = BrowserSafetyPolicy()
    r = BrowserRequest(
        action=BrowserAction.DOWNLOAD,
        parameters={"save_to": "C:/tmp/report.pdf"},
    )
    assert policy.check_request(r).allowed


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------

def test_url_must_be_a_string() -> None:
    policy = BrowserSafetyPolicy()
    decision = policy.check_url(123)  # type: ignore[arg-type]
    assert not decision.allowed


def test_empty_url_rejected() -> None:
    policy = BrowserSafetyPolicy()
    decision = policy.check_url("")
    assert not decision.allowed
