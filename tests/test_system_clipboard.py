"""Tests for ClipboardService Implementation"""
import pytest

from core.results import ActionStatus
from system.clipboard.clipboard_service import WindowsClipboardService


def test_service_instantiates():
    service = WindowsClipboardService()
    assert service is not None


def test_set_and_get_text():
    service = WindowsClipboardService()
    marker = "OMNIX_TEST_12345"
    res = service.set_text(marker)
    assert res.status == ActionStatus.EXECUTED
    assert service.get_text() == marker
    # Clean up
    service.clear()


def test_set_non_string():
    service = WindowsClipboardService()
    res = service.set_text(12345)
    assert res.status == ActionStatus.FAILED


def test_clear():
    service = WindowsClipboardService()
    service.set_text("to be cleared")
    res = service.clear()
    assert res.status == ActionStatus.EXECUTED
    assert service.get_text() == ""


def test_multiline_text():
    service = WindowsClipboardService()
    text = "line1\nline2\nline3"
    res = service.set_text(text)
    assert res.status == ActionStatus.EXECUTED
    assert service.get_text() == text
    service.clear()


def test_unicode_text():
    service = WindowsClipboardService()
    text = "Hello 世界 🌍"
    res = service.set_text(text)
    assert res.status == ActionStatus.EXECUTED
    assert service.get_text() == text
    service.clear()
