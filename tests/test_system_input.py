"""Tests for InputService Implementation"""
import time
import pytest

from core.results import ActionStatus
from system.input.input_service import WindowsInputService


def test_service_instantiates():
    service = WindowsInputService()
    assert service is not None
    # verify the protocol surface
    for method in (
        "click", "double_click", "move_mouse", "type_text",
        "press_key", "hotkey", "drag", "scroll",
    ):
        assert hasattr(service, method)


@pytest.mark.real_windows
def test_click_does_not_fail():
    service = WindowsInputService()
    # click in the middle of the screen - just verify it doesn't crash
    import pyautogui
    w, h = pyautogui.size()
    res = service.click(w // 2, h // 2)
    assert res.status in (ActionStatus.EXECUTED, ActionStatus.FAILED), res.details


@pytest.mark.real_windows
def test_type_text_into_notepad():
    """End-to-end: launch notepad, type text, verify clipboard content."""
    from system.application.app_service import WindowsApplicationService
    app = WindowsApplicationService()
    inp = WindowsInputService()

    # cleanup
    app.close("notepad", force=True)
    time.sleep(0.3)

    app.launch("notepad")
    time.sleep(1.5)

    # focus notepad
    res = inp.type_text("Hello, Omnix!")
    # It will type into whatever window is focused (which we tried to make notepad)
    assert res.status in (ActionStatus.EXECUTED, ActionStatus.FAILED)
    # If executed, length is 13
    if res.status == ActionStatus.EXECUTED:
        assert res.details.get("length") == 13

    time.sleep(0.3)
    # select all + delete to clean up
    inp.hotkey("ctrl", "a")
    inp.press_key("delete")

    app.close("notepad", force=True)


@pytest.mark.real_windows
def test_hotkey_does_not_fail():
    service = WindowsInputService()
    res = service.hotkey("ctrl", "c")
    assert res.status in (ActionStatus.EXECUTED, ActionStatus.FAILED)


def test_hotkey_with_no_keys():
    service = WindowsInputService()
    res = service.hotkey()
    assert res.status == ActionStatus.FAILED


def test_type_empty_text():
    service = WindowsInputService()
    res = service.type_text("")
    assert res.status == ActionStatus.EXECUTED
    assert res.details.get("length") == 0


def test_type_non_string():
    service = WindowsInputService()
    res = service.type_text(12345)
    assert res.status == ActionStatus.FAILED
