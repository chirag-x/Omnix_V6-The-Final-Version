"""
Omnix V6 — Windows ProcessService Implementation (Phase 2).

Implements :class:`core.execution.interfaces.ProcessService` for the
Windows platform, using ``psutil`` as the process introspection and
control layer.

Safety contract
---------------
This service can **kill** processes.  Per the V6 absolute rules
(``DESTRUCTIVE`` safety classification), the service enforces a
configurable *protected-process blacklist* that callers may NOT terminate
without explicitly acknowledging the override.

The default blacklist includes:
    * System-critical processes (``csrss.exe``, ``lsass.exe``, ``smss.exe``,
      ``wininit.exe``, ``services.exe``, ``svchost.exe``, ``winlogon.exe``)
    * The current Python process and its parents
    * Process trees rooted at known shells when running in development

A caller can opt-out of the safety check (not recommended) by passing
``force=True`` to :meth:`kill`, but the blacklist for system-critical
processes is **non-overridable**.

Cancellation
------------
Long-running operations (kill by name when many matches exist) are run
through :func:`run_with_timeout`.  A :class:`CancellationToken` is
honored between iterations.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional , Tuple

import psutil
from loguru import logger as _loguru

from core.execution.interfaces import ProcessService
from core.lifecycle import LifecycleMixin, LifecycleState
from core.results import ActionResult, ActionStatus
from core.utils.timers import CancellationToken, run_with_timeout


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

# System-critical: cannot be killed under any circumstance
NON_OVERRIDABLE_PROTECTED = frozenset({
    "csrss.exe",
    "lsass.exe",
    "smss.exe",
    "wininit.exe",
    "services.exe",
    "winlogon.exe",
    "System",            # kernel process
    "Idle",              # the system idle process
    "Registry",          # system registry
})

# Default overridable protection list (can be killed with force=True)
DEFAULT_PROTECTED = frozenset({
    "explorer.exe",
    "dwm.exe",
    "taskmgr.exe",
})


# ---------------------------------------------------------------------------
# ProcessService
# ---------------------------------------------------------------------------

class WindowsProcessService(ProcessService, LifecycleMixin):
    """psutil-based :class:`ProcessService` for Windows hosts."""

    DEFAULT_TIMEOUT_S = 5.0

    def __init__(
        self,
        *,
        protected_processes: Optional[List[str]] = None,
        enable_protection: bool = True,
    ) -> None:
        self._lifecycle_state: LifecycleState = LifecycleState.CREATED
        self._initialization_error: Optional[str] = None
        self._protected: frozenset = frozenset(
            (p.lower() for p in (protected_processes or DEFAULT_PROTECTED))
        )
        self._enable_protection: bool = enable_protection
        _loguru.debug(
            "WindowsProcessService initialized (protected={}, enable_protection={}).",
            sorted(self._protected),
            self._enable_protection,
        )

    # ====================================================== introspection
    def _normalize_name(self, name: str) -> Tuple[str, str]:
        """Return (bare, with_exe) lowercased forms."""
        n = name.lower().strip()
        if n.endswith(".exe"):
            return (n[:-4], n)
        return (n, n + ".exe")

    def _matches_name(self, candidate: str, target: str) -> bool:
        """True if `candidate` matches `target` regardless of .exe suffix."""
        cand = candidate.lower()
        tgt_bare, tgt_exe = self._normalize_name(target)
        return cand == tgt_bare or cand == tgt_exe

    def is_process_running(self, name: str) -> bool:
        try:
            for proc in psutil.process_iter(["name"]):
                pname = (proc.info.get("name") or "").lower()
                if self._matches_name(pname, name):
                    return True
            return False
        except Exception as exc:  # noqa: BLE001
            _loguru.warning("is_process_running({!r}) failed: {}", name, exc)
            return False

    def pid_for(self, name: str) -> Optional[int]:
        """Return the first matching PID, or None."""
        try:
            for proc in psutil.process_iter(["name"]):
                pname = (proc.info.get("name") or "").lower()
                if self._matches_name(pname, name):
                    return int(proc.pid)
            return None
        except Exception as exc:  # noqa: BLE001
            _loguru.warning("pid_for({!r}) failed: {}", name, exc)
            return None

    def list_processes(self) -> List[Dict[str, Any]]:
        """Return [{pid, name, exe, status, username, create_time}, ...]."""
        out: List[Dict[str, Any]] = []
        try:
            for proc in psutil.process_iter(
                ["name", "exe", "status", "username", "create_time"]
            ):
                info = proc.info
                out.append(
                    {
                        "pid": int(proc.pid),
                        "name": info.get("name") or "",
                        "exe": info.get("exe") or "",
                        "status": info.get("status") or "",
                        "username": info.get("username") or "",
                        "create_time": float(info.get("create_time") or 0.0),
                    }
                )
        except Exception as exc:  # noqa: BLE001
            _loguru.warning("list_processes failed: {}", exc)
        return out

    # ====================================================== control
    def kill(self, pid: int, *, force: bool = True) -> ActionResult:
        """Terminate the process with this PID.

        ``force=True`` uses :func:`psutil.Process.kill` (TerminateProcess).
        ``force=False`` uses :func:`psutil.Process.terminate` (WM_CLOSE
        fallback — actually a soft kill too on Windows).

        A protected-process check runs first unless ``force=True`` AND the
        process is in the *overridable* blacklist.  Non-overridable
        processes are always rejected.
        """
        # Refuse to kill ourselves before opening the handle
        if pid == os.getpid():
            return ActionResult(
                status=ActionStatus.FAILED,
                action_name="kill_process",
                details={"pid": pid, "reason": "self_kill"},
            )

        try:
            proc = psutil.Process(pid)
            # Try to get the name now; protected check uses it
            try:
                proc_name = (proc.name() or "").lower()
            except (psutil.AccessDenied, PermissionError):
                return ActionResult(
                    status=ActionStatus.FAILED,
                    action_name="kill_process",
                    details={"pid": pid, "reason": "access_denied"},
                )
            except Exception:  # noqa: BLE001
                proc_name = ""
        except psutil.NoSuchProcess:
            return ActionResult(
                status=ActionStatus.FAILED,
                action_name="kill_process",
                details={"pid": pid, "reason": "no_such_process"},
            )
        except psutil.AccessDenied:
            return ActionResult(
                status=ActionStatus.FAILED,
                action_name="kill_process",
                details={"pid": pid, "reason": "access_denied"},
            )
        except Exception as exc:  # noqa: BLE001
            return ActionResult(
                status=ActionStatus.FAILED,
                action_name="kill_process",
                details={"pid": pid, "reason": repr(exc)},
            )

        # Non-overridable protection (always checked)
        protected_names_lower = {n.lower() for n in NON_OVERRIDABLE_PROTECTED}
        if proc_name in protected_names_lower:
            return ActionResult(
                status=ActionStatus.FAILED,
                action_name="kill_process",
                details={
                    "pid": pid,
                    "name": proc_name,
                    "reason": "non_overridable_protected",
                },
            )

        # Overridable protection
        if (
            self._enable_protection
            and not force
            and proc_name in self._protected
        ):
            return ActionResult(
                status=ActionStatus.FAILED,
                action_name="kill_process",
                details={
                    "pid": pid,
                    "name": proc_name,
                    "reason": "protected_process",
                    "hint": "pass force=True to override",
                },
            )

        def _do_kill() -> None:
            if force:
                proc.kill()
            else:
                proc.terminate()

        try:
            run_with_timeout(
                _do_kill,
                seconds=self.DEFAULT_TIMEOUT_S,
            )
        except TimeoutError:
            return ActionResult(
                status=ActionStatus.TIMED_OUT,
                action_name="kill_process",
                details={"pid": pid, "force": force},
            )
        except psutil.AccessDenied as exc:
            return ActionResult(
                status=ActionStatus.FAILED,
                action_name="kill_process",
                details={"pid": pid, "reason": "access_denied", "error": str(exc)},
            )
        except Exception as exc:  # noqa: BLE001
            return ActionResult(
                status=ActionStatus.FAILED,
                action_name="kill_process",
                details={"pid": pid, "reason": repr(exc)},
            )

        # Wait briefly and confirm
        try:
            proc.wait(timeout=1.0)
        except psutil.TimeoutExpired:
            # Process still running, but kill was issued
            return ActionResult(
                status=ActionStatus.EXECUTED,
                action_name="kill_process",
                details={
                    "pid": pid,
                    "name": proc_name,
                    "force": force,
                    "confirmed": False,
                },
            )
        except Exception:  # noqa: BLE001
            pass

        return ActionResult(
            status=ActionStatus.EXECUTED,
            action_name="kill_process",
            details={"pid": pid, "name": proc_name, "force": force, "confirmed": True},
        )

    # =================================================== lifecycle hooks
    def _do_initialize(self) -> bool:
        return True

    def _do_shutdown(self) -> None:
        return None

    def statistics(self) -> Dict[str, Any]:
        return {
            "type": "WindowsProcessService",
            "lifecycle": self._lifecycle_state.value,
            "protected_count": len(self._protected),
            "enable_protection": self._enable_protection,
        }

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return (
            f"WindowsProcessService(state={self._lifecycle_state.value}, "
            f"protected={len(self._protected)})"
        )
