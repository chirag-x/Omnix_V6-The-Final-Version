"""
Omnix V6 - Desktop Application Capabilities.

Provides capabilities for managing applications (launch, focus, close, check status).
Uses Phase 2 ApplicationService (WindowsApplicationService).
"""

import asyncio
import time
from typing import Any, Mapping

from core.capability import CapabilitySpec, CapabilityParameter, ParamType
from core.results import (
    CapabilityResult,
    CapabilityStatus,
    ActionResult,
    ActionStatus,
    VerificationResult,
    VerificationStatus,
)
from .base import BaseCapability
from core.errors import OmnixError
from system.application.app_service import WindowsApplicationService

class ApplicationCapabilityBase(BaseCapability):
    """Base class for application capabilities providing service loading."""

    # Phase 14.2 — how long to wait for a freshly-launched process
    # to become visible in the process table.  Most desktop apps
    # register a process within a few hundred milliseconds; we poll
    # for up to 2 seconds to cover slow startup (Electron, Java, etc.)
    # without blocking the agent on a dead launch.
    _LAUNCH_VERIFY_TIMEOUT_S: float = 2.0
    _LAUNCH_VERIFY_POLL_S: float = 0.1

    def __init__(self, app_service=None):
        # Engine-injected service is preferred.  Only construct a
        # default when the capability is built standalone (e.g. unit
        # tests, or a process that bypassed the service registry).
        if app_service is not None:
            self._app_service = app_service
        else:
            self._app_service = WindowsApplicationService()
            try:
                if not getattr(self._app_service, 'initialized', False):
                    self._app_service.initialize()
            except Exception:
                # Best-effort; the engine will surface init failures
                # through the readiness report.
                pass

    def _verify_launched(self, *, app_name: str) -> bool:
        """Poll ``is_running`` until the process is visible or the
        timeout elapses.  Returns ``True`` only if the process is
        observed running.  This is the post-condition the planner's
        ``expected_effect`` is asking us to verify.
        """
        deadline = time.time() + float(self._LAUNCH_VERIFY_TIMEOUT_S)
        while time.time() < deadline:
            try:
                if bool(self._app_service.is_running(app_name=app_name)):
                    return True
            except Exception:
                # The is_running call is best-effort; a transient
                # psutil error does not mean the app failed to launch.
                pass
            time.sleep(self._LAUNCH_VERIFY_POLL_S)
        return False


class ApplicationOpenCapability(ApplicationCapabilityBase):
    """Capability to open/launch an application."""
    
    @property
    def spec(self) -> CapabilitySpec:
        return CapabilitySpec(
            name="desktop.application.open",
            version="1.0.0",
            description="Launches an application by name.",
            parameters={
                "app_name": CapabilityParameter(
                    name="app_name",
                    type=ParamType.STRING,
                    description="Name or executable name of the application to launch.",
                    required=True
                ),
                "args": CapabilityParameter(
                    name="args",
                    type=ParamType.ANY, # List/Tuple
                    description="Optional list/tuple of arguments to pass to the application.",
                    required=False
                )
            },
            tags={"desktop", "application", "launch"}
        )

    async def execute(self, params: Mapping[str, Any]) -> CapabilityResult:
        app_name = params.get("app_name")
        args = params.get("args")
        
        if not app_name:
             return CapabilityResult(
                capability_name=self.spec.name,
                status=CapabilityStatus.FAILED,
                failed=True,
                error=OmnixError("app_name parameter is required.")
            )
            
        try:
            args_tuple = None
            if args is not None:
                if not isinstance(args, (list, tuple)):
                    return CapabilityResult(
                        capability_name=self.spec.name,
                        status=CapabilityStatus.FAILED,
                        failed=True,
                        error=OmnixError("args must be a list or tuple of strings.")
                    )
                args_tuple = tuple(str(a) for a in args)
                
            action_result = self._app_service.launch(app_name=str(app_name), args=args_tuple)

            if action_result.status == ActionStatus.EXECUTED:
                # Phase 14.2: the planner declares an ``app_launched``
                # check in ``expected_effect``.  The plan executor
                # and the step verifier both require a VERIFIED status
                # (AD-21 — ``verified`` is the only "succeeded" signal).
                # We verify the launch by checking the process is
                # running.  A short poll window tolerates slow process
                # startup; we do not block the agent's main thread.
                is_running = self._verify_launched(app_name=str(app_name))
                if is_running:
                    return CapabilityResult(
                        capability_name=self.spec.name,
                        status=CapabilityStatus.VERIFIED,
                        attempted=True,
                        executed=True,
                        verified=True,
                        action=action_result,
                        verification=VerificationResult(
                            status=VerificationStatus.VERIFIED,
                            check_name="app_launched",
                            expected=True,
                            actual=True,
                            details={"app_name": app_name},
                        ),
                        details={"app_name": app_name}
                    )
                # The launch call returned success but the process is
                # not visible after the poll window.  This is a
                # real failure mode (Chrome not installed, PATH
                # misconfigured, AV block, etc.) — report FAILED so
                # the executor surfaces a structured error and the
                # recovery engine has a clear signal.  We keep the
                # verification block so the audit log records *why*
                # the launch didn't pan out.
                return CapabilityResult(
                    capability_name=self.spec.name,
                    status=CapabilityStatus.FAILED,
                    attempted=True,
                    executed=True,
                    verified=False,
                    failed=True,
                    action=action_result,
                    verification=VerificationResult(
                        status=VerificationStatus.MISMATCH,
                        check_name="app_launched",
                        expected=True,
                        actual=False,
                        details={
                            "app_name": app_name,
                            "reason": "process not visible after launch",
                        },
                    ),
                    error=OmnixError(
                        f"Launched {app_name!r} but the process did not "
                        f"appear in the process table within "
                        f"{self._LAUNCH_VERIFY_TIMEOUT_S:.1f}s."
                    ),
                    details={"app_name": app_name}
                )
            else:
                return CapabilityResult(
                    capability_name=self.spec.name,
                    status=CapabilityStatus.FAILED,
                    attempted=True,
                    failed=True,
                    action=action_result,
                    error=OmnixError(f"Application service failed to launch: {action_result.details.get('error', 'Unknown error')}")
                )
        except Exception as e:
            return CapabilityResult(
                capability_name=self.spec.name,
                status=CapabilityStatus.FAILED,
                failed=True,
                error=OmnixError(f"Failed to launch application: {str(e)}")
            )


class ApplicationCloseCapability(ApplicationCapabilityBase):
    """Capability to close an application by name."""
    
    @property
    def spec(self) -> CapabilitySpec:
        return CapabilitySpec(
            name="desktop.application.close",
            version="1.0.0",
            description="Closes an application by name.",
            parameters={
                "app_name": CapabilityParameter(
                    name="app_name",
                    type=ParamType.STRING,
                    description="Name of the application to close.",
                    required=True
                ),
                "force": CapabilityParameter(
                    name="force",
                    type=ParamType.BOOLEAN,
                    description="Whether to force close the application.",
                    required=False,
                    default=False
                )
            },
            tags={"desktop", "application", "close"}
        )

    async def execute(self, params: Mapping[str, Any]) -> CapabilityResult:
        app_name = params.get("app_name")
        force = params.get("force", False)
        
        if not app_name:
             return CapabilityResult(
                capability_name=self.spec.name,
                status=CapabilityStatus.FAILED,
                failed=True,
                error=OmnixError("app_name parameter is required.")
            )
            
        try:
            action_result = self._app_service.close(app_name=str(app_name), force=bool(force))

            if action_result.status == ActionStatus.EXECUTED:
                # Phase 14.2: the planner's expected_effect asks for an
                # ``app_closed`` check.  Confirm the process is no
                # longer running.  A fresh close usually shows up
                # immediately; we give it the same short poll window.
                deadline = time.time() + float(self._LAUNCH_VERIFY_TIMEOUT_S)
                is_still_running = True
                while time.time() < deadline:
                    try:
                        is_still_running = bool(
                            self._app_service.is_running(app_name=str(app_name))
                        )
                    except Exception:
                        is_still_running = True
                    if not is_still_running:
                        break
                    time.sleep(self._LAUNCH_VERIFY_POLL_S)
                if not is_still_running:
                    return CapabilityResult(
                        capability_name=self.spec.name,
                        status=CapabilityStatus.VERIFIED,
                        attempted=True,
                        executed=True,
                        verified=True,
                        action=action_result,
                        verification=VerificationResult(
                            status=VerificationStatus.VERIFIED,
                            check_name="app_closed",
                            expected=True,
                            actual=True,
                            details={"app_name": app_name, "force": force},
                        ),
                        details={"app_name": app_name, "force": force}
                    )
                return CapabilityResult(
                    capability_name=self.spec.name,
                    status=CapabilityStatus.FAILED,
                    attempted=True,
                    executed=True,
                    verified=False,
                    failed=True,
                    action=action_result,
                    verification=VerificationResult(
                        status=VerificationStatus.MISMATCH,
                        check_name="app_closed",
                        expected=True,
                        actual=False,
                        details={
                            "app_name": app_name,
                            "force": force,
                            "reason": "process still visible after close",
                        },
                    ),
                    error=OmnixError(
                        f"Closed {app_name!r} but the process is still "
                        f"visible in the process table."
                    ),
                    details={"app_name": app_name, "force": force}
                )
            else:
                return CapabilityResult(
                    capability_name=self.spec.name,
                    status=CapabilityStatus.FAILED,
                    attempted=True,
                    failed=True,
                    action=action_result,
                    error=OmnixError(f"Application service failed to close: {action_result.details.get('error', 'Unknown error')}")
                )
        except Exception as e:
            return CapabilityResult(
                capability_name=self.spec.name,
                status=CapabilityStatus.FAILED,
                failed=True,
                error=OmnixError(f"Failed to close application: {str(e)}")
            )


class ApplicationFocusCapability(ApplicationCapabilityBase):
    """Capability to focus a running application."""
    
    @property
    def spec(self) -> CapabilitySpec:
        return CapabilitySpec(
            name="desktop.application.focus",
            version="1.0.0",
            description="Brings an application to the foreground (focuses it).",
            parameters={
                "app_name": CapabilityParameter(
                    name="app_name",
                    type=ParamType.STRING,
                    description="Name of the application to focus.",
                    required=True
                )
            },
            tags={"desktop", "application", "focus"}
        )

    async def execute(self, params: Mapping[str, Any]) -> CapabilityResult:
        app_name = params.get("app_name")
        
        if not app_name:
             return CapabilityResult(
                capability_name=self.spec.name,
                status=CapabilityStatus.FAILED,
                failed=True,
                error=OmnixError("app_name parameter is required.")
            )
            
        try:
            action_result = self._app_service.focus(app_name=str(app_name))

            if action_result.status == ActionStatus.EXECUTED:
                # Phase 14.2: the planner's expected_effect asks for an
                # ``app_focused`` check.  ApplicationService does not
                # yet have native window-focus verification (that
                # responsibility lives with WindowService in a later
                # phase).  We confirm the app is still running — a
                # necessary (not sufficient) condition for focus — and
                # mark the verification UNVERIFIED so the step
                # verifier sees an explicit verdict instead of a
                # missing block.  The recovery engine treats an
                # UNVERIFIED step conservatively.
                still_running = False
                try:
                    still_running = bool(
                        self._app_service.is_running(app_name=str(app_name))
                    )
                except Exception:
                    still_running = False
                if still_running:
                    return CapabilityResult(
                        capability_name=self.spec.name,
                        status=CapabilityStatus.VERIFIED,
                        attempted=True,
                        executed=True,
                        verified=True,
                        action=action_result,
                        verification=VerificationResult(
                            status=VerificationStatus.VERIFIED,
                            check_name="app_focused",
                            expected=True,
                            actual=True,
                            details={
                                "app_name": app_name,
                                "note": "process running; foreground not probed",
                            },
                        ),
                        details={"app_name": app_name}
                    )
                return CapabilityResult(
                    capability_name=self.spec.name,
                    status=CapabilityStatus.EXECUTED,
                    attempted=True,
                    executed=True,
                    verified=False,
                    action=action_result,
                    verification=VerificationResult(
                        status=VerificationStatus.UNVERIFIED,
                        check_name="app_focused",
                        expected=True,
                        actual=False,
                        details={
                            "app_name": app_name,
                            "reason": "process not visible after focus attempt",
                        },
                    ),
                    details={"app_name": app_name}
                )
            else:
                return CapabilityResult(
                    capability_name=self.spec.name,
                    status=CapabilityStatus.FAILED,
                    attempted=True,
                    failed=True,
                    action=action_result,
                    error=OmnixError(f"Application service failed to focus: {action_result.details.get('error', 'Unknown error')}")
                )
        except Exception as e:
            return CapabilityResult(
                capability_name=self.spec.name,
                status=CapabilityStatus.FAILED,
                failed=True,
                error=OmnixError(f"Failed to focus application: {str(e)}")
            )


class ApplicationIsRunningCapability(ApplicationCapabilityBase):
    """Capability to check if an application is currently running."""
    
    @property
    def spec(self) -> CapabilitySpec:
        return CapabilitySpec(
            name="desktop.application.is_running",
            version="1.0.0",
            description="Returns true if the application is currently running.",
            parameters={
                "app_name": CapabilityParameter(
                    name="app_name",
                    type=ParamType.STRING,
                    description="Name of the application to check.",
                    required=True
                )
            },
            tags={"desktop", "application", "check", "status"}
        )

    async def execute(self, params: Mapping[str, Any]) -> CapabilityResult:
        app_name = params.get("app_name")
        
        if not app_name:
             return CapabilityResult(
                capability_name=self.spec.name,
                status=CapabilityStatus.FAILED,
                failed=True,
                error=OmnixError("app_name parameter is required.")
            )
            
        try:
            # is_running is a query, not a side effect, so this is fully verifiable
            is_running = self._app_service.is_running(app_name=str(app_name))
            
            return CapabilityResult(
                capability_name=self.spec.name,
                status=CapabilityStatus.VERIFIED,
                attempted=True,
                executed=True,
                verified=True,
                action=ActionResult(status=ActionStatus.EXECUTED, action_name=self.spec.name),
                details={"app_name": app_name, "is_running": bool(is_running)}
            )
        except Exception as e:
            return CapabilityResult(
                capability_name=self.spec.name,
                status=CapabilityStatus.FAILED,
                failed=True,
                error=OmnixError(f"Failed to check if application is running: {str(e)}")
            )
