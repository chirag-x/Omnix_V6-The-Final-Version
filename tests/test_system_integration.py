"""
Integration tests for the full Phase 2 System subsystem.

These tests register the system services with the
:class:`core.service_registry.ServiceRegistry` and exercise them
through the registry, mirroring how the engine would.
"""
import time
import pytest

from core.service_registry import ServiceRegistry
from core.results import ActionStatus
from system import (
    WindowsApplicationService,
    WindowsClipboardService,
    WindowsFilesystemService,
    WindowsInputService,
    WindowsProcessService,
    WindowsWindowService,
)


@pytest.fixture
def registry():
    """Build a registry with every system service registered."""
    reg = ServiceRegistry()
    reg.register(
        WindowsApplicationService(),
        name="application",
        dependencies=(),
    )
    reg.register(
        WindowsWindowService(),
        name="window",
        dependencies=(),
    )
    reg.register(
        WindowsProcessService(protected_processes=[]),
        name="process",
        dependencies=(),
    )
    reg.register(
        WindowsInputService(failsafe=True, pause=0.0),
        name="input",
        dependencies=(),
    )
    reg.register(
        WindowsClipboardService(),
        name="clipboard",
        dependencies=(),
    )
    reg.register(
        WindowsFilesystemService(),
        name="filesystem",
        dependencies=(),
    )
    reg.initialize_all()
    yield reg
    reg.shutdown_all()


def test_registry_boots_all_services(registry):
    names = registry.list_names()
    assert "application" in names
    assert "window" in names
    assert "process" in names
    assert "input" in names
    assert "clipboard" in names
    assert "filesystem" in names

    for name in names:
        assert registry.is_initialized(name), f"{name} not initialized"


def test_registry_health_snapshot(registry):
    health = registry.health()
    assert "services" in health
    for name in [
        "application", "window", "process",
        "input", "clipboard", "filesystem",
    ]:
        assert name in health["services"]
        entry = health["services"][name]
        assert entry.get("initialized") is True


def test_services_emit_statistics(registry):
    for name in [
        "application", "window", "process",
        "input", "clipboard", "filesystem",
    ]:
        svc = registry.resolve(name)
        stats = svc.statistics()
        assert isinstance(stats, dict)
        assert stats.get("type")
        assert stats.get("lifecycle")


def test_clipboard_round_trip_via_registry(registry):
    clip = registry.resolve("clipboard")
    marker = "OMNIX_REG_TEST"
    res = clip.set_text(marker)
    assert res.status == ActionStatus.EXECUTED
    assert clip.get_text() == marker
    clip.clear()


def test_filesystem_round_trip_via_registry(registry):
    import tempfile
    from pathlib import Path

    fs = registry.resolve("filesystem")
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "reg_test.txt"
        res = fs.write_text(str(target), "registered")
        assert res.status == ActionStatus.EXECUTED
        assert target.read_text(encoding="utf-8") == "registered"
        assert fs.read_text(str(target)) == "registered"


def test_application_launch_close_via_registry(registry):
    app = registry.resolve("application")
    proc = registry.resolve("process")

    # cleanup first
    app.close("notepad", force=True)
    time.sleep(0.3)

    app.launch("notepad")
    time.sleep(1.5)

    assert app.is_running("notepad")
    pid = proc.pid_for("notepad.exe")
    assert pid is not None

    # close via app service
    res = app.close("notepad", force=True)
    assert res.status == ActionStatus.EXECUTED

    time.sleep(0.3)


def test_full_system_test_summary(registry):
    """Smoke test: every service responds to a representative call."""
    app = registry.resolve("application")
    win = registry.resolve("window")
    proc = registry.resolve("process")
    clip = registry.resolve("clipboard")
    fs = registry.resolve("filesystem")
    inp = registry.resolve("input")

    # Application
    assert app.is_running("python") is True
    assert app.list_running()  # at least one running process

    # Window
    assert isinstance(win.list_windows(), list)

    # Process
    assert proc.is_process_running("python")
    assert proc.list_processes()

    # Clipboard
    res = clip.set_text("smoke")
    assert res.status == ActionStatus.EXECUTED
    assert clip.get_text() == "smoke"
    clip.clear()

    # Filesystem
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        target = f"{tmp}/smoke.txt"
        assert fs.write_text(target, "ok").status == ActionStatus.EXECUTED
        assert fs.read_text(target) == "ok"

    # Input: just check the service is alive
    assert inp.statistics().get("type") == "WindowsInputService"
