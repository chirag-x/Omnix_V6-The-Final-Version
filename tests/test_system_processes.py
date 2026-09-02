"""Tests for ProcessService Implementation"""
import time
import pytest

from core.results import ActionStatus
from system.processes.process_service import (
    WindowsProcessService,
    NON_OVERRIDABLE_PROTECTED,
)


def test_service_instantiates():
    service = WindowsProcessService()
    assert service is not None


def test_is_process_running_for_python():
    service = WindowsProcessService()
    # We are running this test under Python, so it should be running
    assert service.is_process_running("python") is True


def test_is_process_running_for_fake():
    service = WindowsProcessService()
    assert service.is_process_running("zzz_fake_xyz_123.exe") is False


def test_pid_for_python():
    service = WindowsProcessService()
    pid = service.pid_for("python")
    assert pid is not None
    assert pid > 0


def test_pid_for_fake():
    service = WindowsProcessService()
    assert service.pid_for("zzz_fake_xyz_123.exe") is None


def test_list_processes():
    service = WindowsProcessService()
    procs = service.list_processes()
    assert isinstance(procs, list)
    assert len(procs) > 0
    p = procs[0]
    assert "pid" in p
    assert "name" in p


def test_kill_nonexistent_pid():
    service = WindowsProcessService()
    res = service.kill(999999)
    assert res.status == ActionStatus.FAILED


def test_kill_self_is_rejected():
    import os
    service = WindowsProcessService()
    res = service.kill(os.getpid(), force=True)
    assert res.status == ActionStatus.FAILED
    assert res.details.get("reason") == "self_kill"


def test_kill_non_overridable_protected():
    """System processes cannot be killed, ever."""
    service = WindowsProcessService()
    # Find a System process (PID 4) or csrss
    import psutil
    target_pid = None
    target_name = None
    for proc in psutil.process_iter(["name"]):
        n = (proc.info.get("name") or "").lower()
        if n in {"system", "csrss.exe", "lsass.exe"}:
            target_pid = proc.pid
            target_name = n
            break
    if target_pid is None:
        pytest.skip("Could not find a system-critical process to test against.")
    res = service.kill(target_pid, force=True)
    assert res.status == ActionStatus.FAILED
    assert res.details.get("reason") == "non_overridable_protected"


def test_protected_list_blocks_kill_without_force():
    service = WindowsProcessService(protected_processes=["python.exe"], enable_protection=True)
    pid = service.pid_for("python")
    if pid is None:
        pytest.skip("No python process running")
    res = service.kill(pid, force=False)
    assert res.status == ActionStatus.FAILED
    assert res.details.get("reason") in ["protected_process", "self_kill"]


@pytest.mark.real_windows
def test_kill_real_process_lifecycle():
    """Launch notepad, find PID, kill it, confirm gone."""
    from system.application.app_service import WindowsApplicationService

    app = WindowsApplicationService()
    proc_svc = WindowsProcessService(protected_processes=[])

    # cleanup first
    app.close("notepad", force=True)
    time.sleep(0.5)

    app.launch("notepad")
    time.sleep(1.5)

    pid = proc_svc.pid_for("notepad.exe")
    if pid is None:
        pytest.skip("notepad.exe not found after launch")
    assert proc_svc.is_process_running("notepad.exe")

    res = proc_svc.kill(pid, force=True)
    assert res.status == ActionStatus.EXECUTED

    time.sleep(0.5)
    # notepad may respawn? actually no, single instance
    # We just check it was killed (may have spawned subprocesses like notepad.exe)
    # Best we can do: check the specific PID is gone
    import psutil
    assert not psutil.pid_exists(pid)
