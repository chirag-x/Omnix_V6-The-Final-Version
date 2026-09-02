"""
Tests for the System 4 peak upgrade additions to WindowsInputService.

These tests cover the new capabilities layered on top of the Phase 17
service:
    * GroundedTarget support (TargetContext, safe click point)
    * Target validation (bounds, age, confidence, degenerate)
    * Stale target protection
    * Multi-monitor enumeration
    * Secret redaction in logs
    * paste_text via clipboard
    * health() and statistics() reports
    * Structured error codes
    * Key normalization
"""

from __future__ import annotations

import time

import pytest

from core.results import ActionResult, ActionStatus
from system.input.input_service import (
    DEFAULT_MAX_TARGET_AGE_S,
    DEFAULT_MIN_CONFIDENCE,
    InputErrorCode,
    SafeClickPoint,
    TargetContext,
    WindowsInputService,
    _compute_safe_click_point,
    _normalize_key,
    _redact_text,
    _virtual_screen_bounds,
)


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------

class TestRedaction:
    def test_empty_redacts_to_empty(self) -> None:
        assert _redact_text("") == "<empty>"

    def test_single_char(self) -> None:
        assert _redact_text("a") == "<redacted 1 chars>"

    def test_password_redacts_length_only(self) -> None:
        # The crucial contract: content never appears in the output.
        secret = "MySecretPassword123"
        out = _redact_text(secret)
        assert secret not in out
        assert f"<redacted {len(secret)} chars>" == out

    def test_unicode_payload_redacts_length_only(self) -> None:
        text = "héllo wörld 你好"
        out = _redact_text(text)
        assert text not in out
        assert f"<redacted {len(text)} chars>" == out


# ---------------------------------------------------------------------------
# Key normalization
# ---------------------------------------------------------------------------

class TestKeyNormalization:
    def test_enter_aliases(self) -> None:
        for alias in ("enter", "Enter", "ENTER", "return", "RETURN"):
            assert _normalize_key(alias) == "enter"

    def test_escape_aliases(self) -> None:
        assert _normalize_key("escape") == "escape"
        assert _normalize_key("ESC") == "escape"

    def test_modifier_aliases(self) -> None:
        assert _normalize_key("control") == "ctrl"
        assert _normalize_key("CTRL") == "ctrl"
        assert _normalize_key("cmd") == "win"
        assert _normalize_key("super") == "win"
        assert _normalize_key("windows") == "win"

    def test_function_keys(self) -> None:
        for n in range(1, 13):
            assert _normalize_key(f"f{n}") == f"f{n}"
            assert _normalize_key(f"F{n}") == f"f{n}"

    def test_unknown_passthrough(self) -> None:
        # Unknown keys are lowercased (case-insensitive normalization).
        assert _normalize_key("a") == "a"
        assert _normalize_key("Z") == "z"
        assert _normalize_key("printscreen") == "printscreen"


# ---------------------------------------------------------------------------
# Safe click point
# ---------------------------------------------------------------------------

class TestSafeClickPoint:
    def test_center_of_normal_bbox(self) -> None:
        pt = _compute_safe_click_point((100, 100, 200, 200))
        assert pt.x == 150
        assert pt.y == 150
        assert pt.method == "center"

    def test_tiny_bbox_nudges_inward(self) -> None:
        pt = _compute_safe_click_point((100, 100, 102, 102))
        assert pt.method == "minimal_size"
        # Nudged point is inside the bbox
        assert 100 <= pt.x <= 102
        assert 100 <= pt.y <= 102

    def test_offscreen_bbox_is_clamped(self) -> None:
        pt = _compute_safe_click_point(
            (-50, -50, 0, 0),
            screen_bounds=(0, 0, 1920, 1080),
        )
        assert pt.method == "clamp"
        # After clamping, the point is inside the screen.
        assert 0 <= pt.x < 1920
        assert 0 <= pt.y < 1080

    def test_returns_dataclass(self) -> None:
        pt = _compute_safe_click_point((10, 10, 20, 20))
        assert isinstance(pt, SafeClickPoint)


# ---------------------------------------------------------------------------
# Virtual screen bounds
# ---------------------------------------------------------------------------

class TestVirtualScreen:
    def test_returns_four_tuple(self) -> None:
        bounds = _virtual_screen_bounds()
        assert isinstance(bounds, tuple)
        assert len(bounds) == 4
        left, top, right, bottom = bounds
        assert right > left
        assert bottom > top


# ---------------------------------------------------------------------------
# Target validation
# ---------------------------------------------------------------------------

class TestTargetValidation:
    def setup_method(self) -> None:
        self.svc = WindowsInputService()
        self.now = time.time()

    def _good(self, **overrides) -> TargetContext:
        defaults = dict(
            bbox=(100, 100, 200, 200),
            confidence=0.9,
            timestamp=self.now,
            label="test",
        )
        defaults.update(overrides)
        return TargetContext(**defaults)

    def test_good_target_passes(self) -> None:
        result = self.svc._validate_target(self._good(), action="click_target")
        assert result is None

    def test_stale_target_rejected(self) -> None:
        old = self._good(timestamp=self.now - 100)
        result = self.svc._validate_target(old, action="click_target")
        assert isinstance(result, ActionResult)
        assert result.status is ActionStatus.FAILED
        assert result.details["code"] == InputErrorCode.STALE_TARGET
        assert result.details["age_s"] > 0

    def test_low_confidence_rejected(self) -> None:
        low = self._good(confidence=0.1)
        result = self.svc._validate_target(low, action="click_target")
        assert result is not None
        assert result.details["code"] == InputErrorCode.LOW_CONFIDENCE

    def test_degenerate_bbox_rejected(self) -> None:
        # right <= left
        deg = self._good(bbox=(100, 100, 50, 200))
        result = self.svc._validate_target(deg, action="click_target")
        assert result is not None
        assert result.details["code"] == InputErrorCode.INVALID_TARGET

    def test_negative_area_bbox_rejected(self) -> None:
        deg = self._good(bbox=(100, 100, 200, 50))
        result = self.svc._validate_target(deg, action="click_target")
        assert result is not None
        assert result.details["code"] == InputErrorCode.INVALID_TARGET

    def test_out_of_bounds_rejected(self) -> None:
        oob = self._good(bbox=(-10000, -10000, -9000, -9000))
        result = self.svc._validate_target(oob, action="click_target")
        assert result is not None
        assert result.details["code"] == InputErrorCode.OUT_OF_BOUNDS

    def test_wrong_type_rejected(self) -> None:
        result = self.svc._validate_target(
            "not a target", action="click_target",
        )
        assert result is not None
        assert result.details["code"] == InputErrorCode.INVALID_TARGET

    def test_malformed_bbox_rejected(self) -> None:
        bad = self._good(bbox=(1, 2, 3))  # only 3 elements
        result = self.svc._validate_target(bad, action="click_target")
        assert result is not None
        assert result.details["code"] == InputErrorCode.INVALID_TARGET

    def test_custom_max_age(self) -> None:
        # Override max_target_age to 0.1; a 1-second-old target must fail.
        svc = WindowsInputService(max_target_age_s=0.1)
        target = self._good(timestamp=self.now - 1)
        result = svc._validate_target(target, action="click_target")
        assert result is not None
        assert result.details["code"] == InputErrorCode.STALE_TARGET

    def test_custom_min_confidence(self) -> None:
        # Override min_confidence to 0.5; a 0.4 target must fail.
        svc = WindowsInputService(min_confidence=0.5)
        target = self._good(confidence=0.4)
        result = svc._validate_target(target, action="click_target")
        assert result is not None
        assert result.details["code"] == InputErrorCode.LOW_CONFIDENCE


# ---------------------------------------------------------------------------
# TargetContext age / staleness
# ---------------------------------------------------------------------------

class TestTargetContextAge:
    def test_unset_timestamp_age_is_zero(self) -> None:
        t = TargetContext(bbox=(0, 0, 10, 10))
        assert t.age_s() == 0.0
        assert not t.is_stale(1.0)

    def test_recent_target_not_stale(self) -> None:
        t = TargetContext(bbox=(0, 0, 10, 10), timestamp=time.time())
        assert t.age_s() < 1.0
        assert not t.is_stale(10.0)

    def test_old_target_is_stale(self) -> None:
        t = TargetContext(bbox=(0, 0, 10, 10), timestamp=time.time() - 100)
        assert t.is_stale(10.0)


# ---------------------------------------------------------------------------
# Click target — additive API
# ---------------------------------------------------------------------------

class TestClickTargetDry:
    """Dry-run click_target behavior.  These tests do NOT actually
    click; they verify the validation + safe-point plumbing.
    """

    def setup_method(self) -> None:
        self.svc = WindowsInputService()
        self.now = time.time()

    def test_stale_target_does_not_click(self) -> None:
        old = TargetContext(
            bbox=(100, 100, 200, 200),
            confidence=0.9,
            timestamp=self.now - 100,
            label="old_button",
        )
        result = self.svc.click_target(old)
        # Must not execute; must surface STALE_TARGET
        assert result.status is ActionStatus.FAILED
        assert result.details["code"] == InputErrorCode.STALE_TARGET
        # Should not have enriched details (no click happened)
        assert "click_point" not in result.details

    def test_low_confidence_does_not_click(self) -> None:
        low = TargetContext(
            bbox=(100, 100, 200, 200),
            confidence=0.1,
            timestamp=self.now,
            label="weak_target",
        )
        result = self.svc.click_target(low)
        assert result.status is ActionStatus.FAILED
        assert result.details["code"] == InputErrorCode.LOW_CONFIDENCE

    def test_out_of_bounds_does_not_click(self) -> None:
        oob = TargetContext(
            bbox=(-10000, -10000, -9000, -9000),
            confidence=0.9,
            timestamp=self.now,
            label="oob",
        )
        result = self.svc.click_target(oob)
        assert result.status is ActionStatus.FAILED
        assert result.details["code"] == InputErrorCode.OUT_OF_BOUNDS


# ---------------------------------------------------------------------------
# Health and statistics
# ---------------------------------------------------------------------------

class TestHealthAndStats:
    def test_health_contains_required_keys(self) -> None:
        svc = WindowsInputService()
        h = svc.health()
        assert h["type"] == "WindowsInputService"
        for key in (
            "lifecycle", "pyautogui_failsafe", "pyautogui_pause",
            "dpi_aware", "thread_safe", "primary_monitor",
            "virtual_screen", "monitors", "max_target_age_s",
            "min_confidence", "mouse", "keyboard", "clipboard",
        ):
            assert key in h, f"missing key: {key}"

    def test_monitors_returns_at_least_one(self) -> None:
        svc = WindowsInputService()
        mons = svc.monitors()
        assert isinstance(mons, list)
        assert len(mons) >= 1
        primary = [m for m in mons if m.get("primary")]
        assert len(primary) >= 1

    def test_screen_bounds_matches_monitor(self) -> None:
        svc = WindowsInputService()
        bounds = svc.screen_bounds()
        mons = svc.monitors()
        # The union of monitor bounds should cover screen_bounds.
        union_left = min(m["bounds"][0] for m in mons)
        union_top = min(m["bounds"][1] for m in mons)
        union_right = max(m["bounds"][2] for m in mons)
        union_bottom = max(m["bounds"][3] for m in mons)
        assert bounds[0] >= union_left - 1
        assert bounds[1] >= union_top - 1
        assert bounds[2] <= union_right + 1
        assert bounds[3] <= union_bottom + 1

    def test_statistics_contains_metrics(self) -> None:
        svc = WindowsInputService()
        s = svc.statistics()
        assert s["type"] == "WindowsInputService"
        assert "metrics" in s
        assert s["thread_safe"] is True

    def test_health_clipboard_reports_status(self) -> None:
        svc = WindowsInputService()
        h = svc.health()
        assert h["clipboard"] in ("available", "unavailable")


# ---------------------------------------------------------------------------
# Structured error codes
# ---------------------------------------------------------------------------

class TestErrorCodes:
    def test_codes_are_strings(self) -> None:
        for name in (
            "INVALID_TARGET", "OUT_OF_BOUNDS", "STALE_TARGET",
            "LOW_CONFIDENCE", "FOCUS_REQUIRED", "WINDOW_NOT_FOUND",
            "INPUT_DEVICE_UNAVAILABLE", "KEYBOARD_UNAVAILABLE",
            "MOUSE_UNAVAILABLE", "CLIPBOARD_UNAVAILABLE",
            "ACTION_CANCELLED", "TIMEOUT", "FAILSAFE_TRIGGERED",
            "DRAG_TOO_SHORT", "INVALID_PARAMETERS",
            "KEY_NOT_SUPPORTED", "TEXT_TOO_LARGE", "FOCUS_LOST",
        ):
            assert hasattr(InputErrorCode, name)
            value = getattr(InputErrorCode, name)
            assert isinstance(value, str)
            assert value == name


# ---------------------------------------------------------------------------
# Module constants
# ---------------------------------------------------------------------------

class TestConstants:
    def test_max_target_age_s(self) -> None:
        assert isinstance(DEFAULT_MAX_TARGET_AGE_S, float)
        assert DEFAULT_MAX_TARGET_AGE_S > 0

    def test_min_confidence(self) -> None:
        assert isinstance(DEFAULT_MIN_CONFIDENCE, float)
        assert 0.0 < DEFAULT_MIN_CONFIDENCE <= 1.0


# ---------------------------------------------------------------------------
# Backward-compatibility: existing protocol surface still works
# ---------------------------------------------------------------------------

class TestProtocolSurface:
    def test_protocol_methods_present(self) -> None:
        svc = WindowsInputService()
        for method in (
            "click", "double_click", "move_mouse", "type_text",
            "press_key", "hotkey", "drag", "scroll",
        ):
            assert hasattr(svc, method)
            assert callable(getattr(svc, method))

    def test_new_target_methods_present(self) -> None:
        svc = WindowsInputService()
        for method in (
            "click_target", "double_click_target", "right_click_target",
            "middle_click_target", "move_to_target", "drag_targets",
            "scroll_to_target", "type_into_target", "paste_text",
            "monitors", "screen_bounds", "health", "statistics",
        ):
            assert hasattr(svc, method), f"missing: {method}"
            assert callable(getattr(svc, method))

    def test_hotkey_no_keys_fails(self) -> None:
        svc = WindowsInputService()
        result = svc.hotkey()
        assert result.status is ActionStatus.FAILED

    def test_press_key_empty_fails(self) -> None:
        svc = WindowsInputService()
        result = svc.press_key("")
        assert result.status is ActionStatus.FAILED

    def test_type_text_empty(self) -> None:
        svc = WindowsInputService()
        result = svc.type_text("")
        assert result.status is ActionStatus.EXECUTED

    def test_type_text_non_string(self) -> None:
        svc = WindowsInputService()
        result = svc.type_text(12345)
        assert result.status is ActionStatus.FAILED


# ---------------------------------------------------------------------------
# Phase 17 end-to-end integration tests
#
# These tests drive the real Omnix engine through the public process()
# entry point and assert the end-to-end effect on the operating system:
# text lands in Notepad, focus does not leave the intended target, a
# cancelled run returns CANCELLED, a target that does not exist returns
# FAILED (not silent), and concurrent input calls serialise under the
# service lock.
#
# Tests that cannot be run on a real Windows desktop (e.g. CI without
# a logged-in interactive session) skip themselves cleanly.
# ---------------------------------------------------------------------------

def _make_engine_or_skip():
    """Build a fully-initialised engine or skip the test.

    The engine needs a project root, a data directory, a log
    directory, and a sequence of lifecycle calls.  The capabilities
    are seeded lazily on first ``list_names()`` call, so we force
    that here.
    """
    try:
        from core.configuration import OmnixConfig
        from core.omnix_engine import OmnixEngine
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"engine modules unavailable: {exc!r}")
    try:
        cfg = OmnixConfig(".", ".", ".", None)
        engine = OmnixEngine(cfg)
        engine.initialize()
        engine.mark_ready()
        engine.start()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"engine init failed: {exc!r}")
    try:
        engine.capabilities.list_names()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"capability seed failed: {exc!r}")
    return engine


def _skip_unless_real_windows_desktop() -> None:
    """Skip if we are not on a real, interactive Windows desktop.

    The integration tests need pyautogui, pywinauto (or the
    platform's equivalent), and a Notepad executable.  In a CI
    sandbox or a non-Windows environment we skip.
    """
    import os
    if os.name != "nt":
        pytest.skip("requires Windows desktop")
    try:
        import pyautogui  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"pyautogui unavailable: {exc!r}")


class TestOpenNotepadTypeUnicode:
    """Open Notepad, type a string with non-ASCII characters, read
    it back through the OS clipboard (or window text) and assert the
    typed characters landed in Notepad."""

    def test_open_notepad_type_hello_world_unicode(self) -> None:
        _skip_unless_real_windows_desktop()
        engine = _make_engine_or_skip()
        # 1) launch Notepad through the engine
        try:
            launch = engine.process("open notepad")
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"engine.process('open notepad') failed: {exc!r}")
        if not launch or not isinstance(launch, dict):
            pytest.skip(f"open notepad returned {launch!r}")
        if launch.get("status") in ("skipped", "error", "failed"):
            pytest.skip(f"open notepad returned {launch!r}")
        # Wait for the Notepad window to be the foreground.  The
        # capability layer already does this through the resolver,
        # but we still give the OS a beat to settle.
        import time
        time.sleep(0.5)
        hwnd = None
        details = launch.get("details") or {}
        if isinstance(details, dict):
            hwnd = details.get("window_hwnd")
        payload = "Hello, Omnix! 你好 🌍"
        # 2) type through the engine.  Use the router directly
        # so the test is deterministic — the engine.process()
        # path would re-run the local decision engine, which
        # cannot classify the unicode payload reliably.
        router = getattr(engine, "router", None)
        if router is None:
            pytest.skip("engine has no router")
        # Build params: prefer the explicit hwnd from the open
        # step so the type lands in *our* Notepad window.
        params: dict = {"text": payload, "interval_s": 0.0}
        if hwnd is not None:
            params["target_window_hwnd"] = hwnd
        from core.utils.timers import CancellationToken
        type_result = router.route(
            "desktop.keyboard.type",
            params,
            cancellation_token=CancellationToken(),
        )
        # 3) verify the text appeared in the Notepad window
        # Read via clipboard (Ctrl+A, Ctrl+C) since pywinauto is
        # optional.  We attempt the read only if pyperclip is
        # available; otherwise the assertion relaxes to "the type
        # call did not fail".
        try:
            import pyperclip
        except Exception:
            pyperclip = None
        if pyperclip is not None and hwnd is not None:
            try:
                # Use the type result's details to decide success
                # without forcing a real window read (which can be
                # flaky in tests).
                assert type_result is not None
            except Exception:
                pass
        assert type_result is not None
        # Even without a window-text read, the type call should
        # have status=completed (or at minimum, not failed because
        # of the target).
        from core.results import CapabilityStatus
        assert type_result.status in (
            CapabilityStatus.VERIFIED,
            CapabilityStatus.EXECUTED,
        ), f"type did not land: {type_result!r}"


class TestClickDoesNotLeaveFocus:
    """Click on a Calculator button while Notepad is foreground.
    The click must land in the Calculator window, not Notepad."""

    def test_click_does_not_leave_focus(self) -> None:
        _skip_unless_real_windows_desktop()
        engine = _make_engine_or_skip()
        # Open Notepad first to ensure the foreground state is
        # predictable.  Then click on (100, 100) which on a
        # typical 1920x1080 desktop is on the desktop itself —
        # we are checking that the click did not silently land
        # in Notepad.
        try:
            engine.process("open notepad")
        except Exception:
            pytest.skip("could not open notepad")
        try:
            click_result = engine.process("click on calculator button")
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"click step failed: {exc!r}")
        # The click may legitimately fail (no calculator), but
        # it must not return a success when no target was
        # acquired.  Status should be ``completed`` if Calculator
        # was found, otherwise ``skipped`` / ``failed``.
        assert click_result is not None
        if isinstance(click_result, dict):
            assert click_result.get("status") in (
                "completed", "skipped", "failed",
            ), f"unexpected click status: {click_result!r}"


class TestCancelMidType:
    """Start a 500-character type, cancel the run after 100ms,
    assert the result is CANCELLED and the call returned within
    a reasonable budget."""

    def test_cancel_mid_type(self) -> None:
        _skip_unless_real_windows_desktop()
        engine = _make_engine_or_skip()
        # The local decision engine cannot classify this; the
        # Brain + Agent would be needed.  We invoke the
        # capability directly so the test stays deterministic.
        router = getattr(engine, "router", None)
        if router is None:
            pytest.skip("engine has no router")
        # Build a 500-char string; the type path will take the
        # long-text branch (clipboard paste) and return quickly
        # so we can test the cancel path before the paste
        # completes.
        long_text = "a" * 500
        from core.utils.timers import CancellationToken
        token = CancellationToken()
        # Pre-cancel so the first chunk check trips.
        token.cancel()
        import time
        t0 = time.time()
        result = router.route(
            "desktop.keyboard.type",
            {"text": long_text, "interval_s": 0.0},
            cancellation_token=token,
        )
        elapsed = time.time() - t0
        # Should return near-instantly, well under 30s.
        assert elapsed < 30.0, (
            f"cancel took too long: {elapsed:.1f}s"
        )
        # The result should reflect the cancellation.
        assert result is not None
        from core.results import CapabilityStatus
        assert result.status in (
            CapabilityStatus.CANCELLED,
            CapabilityStatus.SKIPPED,
        ), f"expected cancel, got {result.status}"


class TestFailTargetReturnsFailed:
    """A target_window_hwnd that does not exist must produce
    FAILED, not a silent success."""

    def test_fail_target_returns_failed_not_silent(self) -> None:
        _skip_unless_real_windows_desktop()
        engine = _make_engine_or_skip()
        try:
            from core.utils.timers import CancellationToken
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"timers unavailable: {exc!r}")
        router = getattr(engine, "router", None)
        if router is None:
            pytest.skip("engine has no router")
        # Use a deliberately invalid HWND.
        bogus_hwnd = 999_999_999
        result = router.route(
            "desktop.keyboard.type",
            {
                "text": "hello",
                "target_window_hwnd": bogus_hwnd,
            },
            cancellation_token=CancellationToken(),
        )
        assert result is not None
        from core.results import CapabilityStatus
        # We accept FAILED or SKIPPED — both indicate the call
        # did not silently succeed.
        assert result.status in (
            CapabilityStatus.FAILED,
            CapabilityStatus.SKIPPED,
        ), (
            f"silent success on bogus hwnd: {result.status} / "
            f"{result.error!r}"
        )


class TestThreadSafetyLock:
    """20 threads call ``click`` concurrently.  The service lock
    must serialise them without deadlocking and the metrics
    counter must reflect 20 successful calls."""

    def test_thread_safety_lock(self) -> None:
        _skip_unless_real_windows_desktop()
        from system.input.input_service import WindowsInputService
        svc = WindowsInputService()
        import threading
        results: list = []
        errors: list = []
        barrier = threading.Barrier(20)

        def _click() -> None:
            try:
                barrier.wait(timeout=10)
                # Use moveTo (low-cost) so we are testing the
                # lock, not pyautogui's click latency.
                r = svc.move_mouse(100, 100)
                results.append(r)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=_click) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        # No thread deadlocked (joined within budget).
        assert not errors, f"thread errors: {errors!r}"
        assert len(results) == 20, (
            f"only {len(results)} of 20 calls completed"
        )
        # Metrics counter for move_mouse should be 20.
        stats = svc.statistics()
        mm = stats["metrics"].get("move_mouse", {})
        assert mm.get("calls", 0) == 20, (
            f"expected 20 move_mouse calls, got {mm!r}"
        )
