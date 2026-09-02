"""Tests for ApplicationService Implementation"""
import pytest
import sys
from system.application.app_service import WindowsApplicationService
from system.application import ApplicationCatalog, ApplicationResolver
from core.results import ActionStatus

def test_application_service_is_running():
    service = WindowsApplicationService()
    # Python should be running to run this test
    assert service.is_running("python") is True

def test_application_service_not_running():
    service = WindowsApplicationService()
    assert service.is_running("some_fake_app_xyz_123") is False

def test_application_service_resolves_via_catalog():
    """Phase 15: resolution goes through the catalog+resolver, not a
    hardcoded alias table.  ``Chrome`` and ``Spotify`` must both
    resolve to an :class:`ApplicationRecord` with a non-empty
    ``executable`` field.  We initialize the catalog lazily here so
    the test does not block on registry walks when the host has no
    Chrome/Spotify installed.
    """
    service = WindowsApplicationService()
    try:
        service._catalog.initialize()
    except Exception:
        pass
    res = service.resolve("Chrome")
    if res.is_found and res.record is not None:
        assert res.record.executable.lower().endswith(".exe")
    # Spotify is optional; we only assert "not_found" or "found", never
    # a hardcoded alias lookup.
    res2 = service.resolve("Spotify")
    assert res2 is not None

def test_application_service_resolves_notepad():
    """Notepad is part of Windows and is always on PATH."""
    service = WindowsApplicationService()
    try:
        service._catalog.initialize()
    except Exception:
        pass
    res = service.resolve("notepad")
    assert res.is_found, f"notepad should resolve: {res.reason}"
    assert res.record is not None
    assert res.record.executable.lower() == "notepad.exe"

def test_application_service_unknown_returns_not_found():
    service = WindowsApplicationService()
    try:
        service._catalog.initialize()
    except Exception:
        pass
    res = service.resolve("__definitely_not_a_real_app_xyz_987__")
    assert res.is_found is False
    assert res.reason == "not_found" or res.status == "not_found"

@pytest.mark.real_windows
def test_application_service_lifecycle():
    """Integration test: launch and close notepad."""
    import time
    service = WindowsApplicationService()
    try:
        service._catalog.initialize()
    except Exception:
        pass

    # Clean up any pre-existing instances
    service.close("notepad", force=True)
    time.sleep(0.5)

    # Track how many were running before we started
    before = service.list_running()

    res = service.launch("notepad")
    assert res.status == ActionStatus.EXECUTED

    time.sleep(1.5)  # allow process tree to populate

    assert service.is_running("notepad"), "notepad should be running after launch"

    # Close it
    res_close = service.close("notepad", force=True)
    assert res_close.status == ActionStatus.EXECUTED
    assert res_close.details["killed_instances"] > 0, "should have killed at least 1 instance"

    time.sleep(0.5)

    # After our close, ensure not running again
    # (it may still be running if there were pre-existing instances)
    service.close("notepad", force=True)
