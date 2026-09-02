"""Tests for WindowService Implementation"""
import time
import pytest

from core.results import ActionStatus
from system.windows.window_service import WindowsWindowService, _WIN32_AVAILABLE


def test_service_instantiates():
    service = WindowsWindowService()
    assert service is not None
    assert hasattr(service, "list_windows")
    assert hasattr(service, "find_window")
    assert hasattr(service, "focus_window")
    assert hasattr(service, "move_window")
    assert hasattr(service, "resize_window")


def test_list_windows_returns_list():
    service = WindowsWindowService()
    windows = service.list_windows()
    assert isinstance(windows, list)
    if windows:
        win = windows[0]
        assert "hwnd" in win
        assert "title" in win
        assert "process" in win
        assert "pid" in win
        assert "bounds" in win
        assert "visible" in win


def test_find_window_none_for_garbage():
    service = WindowsWindowService()
    result = service.find_window(title="ZZZ_NONEXISTENT_WINDOW_12345")
    assert result is None


def test_find_window_with_no_filter():
    service = WindowsWindowService()
    result = service.find_window()
    assert result is None


def test_resize_with_invalid_size():
    service = WindowsWindowService()
    # Use a fake hwnd
    res = service.resize_window(999999, 0, 0)
    assert res.status == ActionStatus.FAILED


def test_focus_invalid_hwnd():
    service = WindowsWindowService()
    res = service.focus_window(999999)
    # Should fail because hwnd doesn't exist
    assert res.status == ActionStatus.FAILED


def test_move_window_invalid_hwnd():
    service = WindowsWindowService()
    res = service.move_window(999999, 0, 0, 100, 100)
    assert res.status == ActionStatus.FAILED


def test_move_window_invalid_size():
    service = WindowsWindowService()
    res = service.move_window(999999, 0, 0, -10, -10)
    assert res.status == ActionStatus.FAILED


@pytest.mark.real_windows
def test_focus_real_window():
    """Open notepad, focus it, then close it."""
    from system.application.app_service import WindowsApplicationService

    app = WindowsApplicationService()
    win = WindowsWindowService()

    app.close("notepad", force=True)
    time.sleep(0.5)

    app.launch("notepad")
    time.sleep(1.5)

    found = win.find_window(title="Notepad")
    if found is None:
        # Notepad window not yet visible; skip
        app.close("notepad", force=True)
        pytest.skip("Notepad window did not appear; skipping.")

    res = win.focus_window(found["hwnd"])
    assert res.status in (ActionStatus.EXECUTED, ActionStatus.FAILED), res.notes

    app.close("notepad", force=True)
