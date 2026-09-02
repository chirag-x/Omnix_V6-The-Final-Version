"""
Omnix V6 — ApplicationService (resolver-driven).

The service no longer hardcodes per-application aliases.  It owns a
:class:`ApplicationCatalog` (built at boot from Registry + App Paths
+ Start Menu + PATH + processes) and a :class:`ApplicationResolver`
that translates user-facing names into :class:`ApplicationRecord`
objects.  ``launch`` is the only operation that may execute a
process; everything else is a thin wrapper around ``psutil`` or the
catalog.

Public surface
--------------

- :meth:`launch`  — resolve ``name`` and start the executable.
- :meth:`is_running` / :meth:`list_running` — process introspection.
- :meth:`focus` — delegates to the window service when injected,
  otherwise falls back to a process-presence check.
- :meth:`close`  — terminate matching processes (with ``force=True``).
- :meth:`resolve` — return a :class:`Resolution` for callers that
  need to distinguish "not found" from "ambiguous".

If the resolver returns ``not_found`` we surface a structured
``FAILED`` :class:`ActionResult` — the engine never silently falls
back to a hand-maintained alias table.
"""

from __future__ import annotations

import subprocess
from typing import Any, Dict, List, Optional, Tuple

import psutil

from core.execution.interfaces import ApplicationService
from core.results import ActionResult, ActionStatus
from core.errors import OmnixError
from core.lifecycle import LifecycleMixin, LifecycleState
from system.application.resolver import ApplicationResolver

from .catalog import ApplicationCatalog
from .models import Resolution


class WindowsApplicationService(ApplicationService, LifecycleMixin):
    """Concrete :class:`ApplicationService` that delegates name
    resolution to the catalog/resolver and uses ``psutil`` + ``Popen``
    for process control.
    """

    def __init__(
        self,
        *,
        catalog: Optional[ApplicationCatalog] = None,
        resolver: Optional["ApplicationResolver"] = None,
        window_service: Optional[Any] = None,
    ) -> None:
        self._lifecycle_state: LifecycleState = LifecycleState.CREATED
        self._initialization_error: Optional[str] = None
        # Import here to avoid a circular import at module load.
        from .resolver import ApplicationResolver
        self._catalog = catalog or ApplicationCatalog()
        self._resolver = resolver or ApplicationResolver(self._catalog)
        self._window_service = window_service  # optional injection

    # ---------------------------------------------------------- lifecycle
    def _do_initialize(self) -> bool:
        # The catalog is itself a LifecycleMixin; if it isn't already
        # READY, initialize it now so the boot path is order-tolerant.
        if (
            isinstance(self._catalog, LifecycleMixin)
            and self._catalog.lifecycle_state != LifecycleState.READY
        ):
            return self._catalog.initialize()
        return True

    def _do_shutdown(self) -> None:
        if isinstance(self._catalog, LifecycleMixin):
            self._catalog.shutdown()

    def statistics(self) -> Dict[str, Any]:
        stats: Dict[str, Any] = {
            "type": "WindowsApplicationService",
            "lifecycle": self._lifecycle_state.value,
            "catalog": self._catalog.statistics()
            if isinstance(self._catalog, LifecycleMixin)
            else None,
        }
        return stats

    # ---------------------------------------------------------- public API
    def resolve(self, name: str) -> Resolution:
        return self._resolver.resolve(name)

    def is_installed(self, name: str) -> bool:
        return self._resolver.is_installed(name)

    def launch(
        self,
        app_name: str,
        *,
        args: Optional[Tuple[str, ...]] = None,
    ) -> ActionResult:
        res = self._resolver.resolve(app_name)
        if not res.is_found or res.record is None:
            return self._not_found_result("launch_application", app_name, res)
        rec = res.record
        # Prefer the absolute path the catalog recorded; if absent,
        # fall back to a PATH-based launch (Popen with shell=True).
        target = rec.executable_path or rec.executable
        try:
            flags: List[str] = [target]
            if args:
                flags.extend(args)
            proc = subprocess.Popen(
                flags,
                shell=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as exc:  # noqa: BLE001
            return ActionResult(
                status=ActionStatus.FAILED,
                action_name="launch_application",
                details={
                    "target_exe": target,
                    "app_name": app_name,
                    "source": rec.source,
                },
            ).with_error(
                OmnixError(
                    message=f"Failed to launch {target}: {exc}",
                    code="LAUNCH_FAILURE",
                )
            )
        return ActionResult(
            status=ActionStatus.EXECUTED,
            action_name="launch_application",
            details={
                "target_exe": target,
                "app_name": app_name,
                "source": rec.source,
                "proxy_pid": proc.pid,
                "resolution": "launched",
                "note": "Process started. Verification required.",
            },
        )

    def focus(self, app_name: str) -> ActionResult:
        # If a window service is injected, delegate.  Otherwise fall
        # back to a process-presence check.
        if self._window_service is not None:
            try:
                ws_result = self._window_service.focus(app_name)
                if ws_result is not None:
                    return ws_result
            except Exception:  # noqa: BLE001
                pass
        res = self._resolver.resolve(app_name)
        if not res.is_found or res.record is None:
            return self._not_found_result("focus_application", app_name, res)
        if self.is_running(app_name):
            return ActionResult(
                status=ActionStatus.EXECUTED,
                action_name="focus_application",
                details={
                    "target_exe": res.record.executable,
                    "app_name": app_name,
                    "note": "App is alive; window-level focus "
                            "requires WindowService",
                },
            )
        return ActionResult(
            status=ActionStatus.FAILED,
            action_name="focus_application",
            details={"app_name": app_name, "target_exe": res.record.executable},
        ).with_error(
            OmnixError(message="Application not running", code="APP_NOT_RUNNING")
        )

    def close(self, app_name: str, *, force: bool = False) -> ActionResult:
        res = self._resolver.resolve(app_name)
        if not res.is_found or res.record is None:
            return self._not_found_result("close_application", app_name, res)
        target_exe = res.record.executable
        killed = 0
        try:
            for proc in psutil.process_iter(["name", "pid"]):
                name = proc.info.get("name")
                if name and name.lower() == target_exe.lower():
                    if force:
                        proc.kill()
                    else:
                        proc.terminate()
                    killed += 1
        except psutil.AccessDenied:
            return ActionResult(
                status=ActionStatus.FAILED,
                action_name="close_application",
                details={"app_name": app_name, "target_exe": target_exe},
            ).with_error(
                OmnixError(
                    message="Access denied terminating process",
                    code="ACCESS_DENIED",
                )
            )
        except Exception as exc:  # noqa: BLE001
            return ActionResult(
                status=ActionStatus.FAILED,
                action_name="close_application",
                details={"app_name": app_name, "target_exe": target_exe},
            ).with_error(
                OmnixError(
                    message=f"Error closing {target_exe}: {exc}",
                    code="CLOSE_FAILURE",
                )
            )
        if killed > 0:
            return ActionResult(
                status=ActionStatus.EXECUTED,
                action_name="close_application",
                details={
                    "app_name": app_name,
                    "target_exe": target_exe,
                    "killed_instances": killed,
                    "force": force,
                },
            )
        return ActionResult(
            status=ActionStatus.FAILED,
            action_name="close_application",
            details={"app_name": app_name, "target_exe": target_exe},
        ).with_error(
            OmnixError(message="No instances found to close", code="NO_INSTANCES")
        )

    def is_running(self, app_name: str) -> bool:
        res = self._resolver.resolve(app_name)
        if not res.is_found or res.record is None:
            return False
        target_exe = res.record.executable.lower()
        try:
            for proc in psutil.process_iter(["name"]):
                name = proc.info.get("name")
                if name and name.lower() == target_exe:
                    return True
        except psutil.Error:
            return False
        return False

    def list_running(self) -> List[str]:
        running = set()
        try:
            for proc in psutil.process_iter(["name"]):
                name = proc.info.get("name")
                if name:
                    running.add(name.lower())
        except psutil.Error:
            pass
        return sorted(running)

    # ---------------------------------------------------------- helpers
    def _not_found_result(
        self,
        action_name: str,
        app_name: str,
        res: Resolution,
    ) -> ActionResult:
        return ActionResult(
            status=ActionStatus.FAILED,
            action_name=action_name,
            details={
                "app_name": app_name,
                "reason": res.reason or "not_found",
                "status": res.status,
            },
        ).with_error(
            OmnixError(
                message=f"Application not found: {app_name}",
                code="APP_NOT_FOUND",
            )
        )
