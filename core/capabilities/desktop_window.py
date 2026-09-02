"""
Omnix V6 - Desktop Window Capabilities.

Provides capabilities for managing windows (list, focus, minimize, maximize, restore, close).
Uses Phase 2 WindowService (WindowsWindowService).
"""

import asyncio
from typing import Any, Mapping

from core.capability import CapabilitySpec, CapabilityParameter, ParamType
from core.results import CapabilityResult, CapabilityStatus, ActionResult, ActionStatus
from .base import BaseCapability
from core.errors import OmnixError
from system.windows.window_service import WindowsWindowService

class WindowCapabilityBase(BaseCapability):
    """Base class for window capabilities providing service loading."""

    def __init__(self, window_service=None):
        if window_service is not None:
            self._window_service = window_service
        else:
            self._window_service = WindowsWindowService()
            try:
                if not getattr(self._window_service, 'initialized', False):
                    self._window_service.initialize()
            except Exception:
                pass


class WindowListCapability(WindowCapabilityBase):
    """Capability to list visible windows on the desktop."""
    
    @property
    def spec(self) -> CapabilitySpec:
        return CapabilitySpec(
            name="desktop.window.list",
            version="1.0.0",
            description="Lists visible windows on the desktop.",
            parameters={},
            tags={"desktop", "window", "list", "observation"}
        )

    async def execute(self, params: Mapping[str, Any]) -> CapabilityResult:
        try:
            windows = self._window_service.list_windows()
            
            return CapabilityResult(
                capability_name=self.spec.name,
                status=CapabilityStatus.VERIFIED,
                attempted=True,
                executed=True,
                verified=True,
                action=ActionResult(status=ActionStatus.EXECUTED, action_name=self.spec.name),
                details={"windows": windows, "count": len(windows)}
            )
        except Exception as e:
            return CapabilityResult(
                capability_name=self.spec.name,
                status=CapabilityStatus.FAILED,
                failed=True,
                error=OmnixError(f"Failed to list windows: {str(e)}")
            )


class WindowFocusCapability(WindowCapabilityBase):
    """Capability to focus a window by its HWND."""
    
    @property
    def spec(self) -> CapabilitySpec:
        return CapabilitySpec(
            name="desktop.window.focus",
            version="1.0.0",
            description="Brings a window to the foreground (focuses it) by its HWND.",
            parameters={
                "hwnd": CapabilityParameter(
                    name="hwnd",
                    type=ParamType.INTEGER,
                    description="The window handle (HWND) of the window to focus.",
                    required=True
                )
            },
            tags={"desktop", "window", "focus"}
        )

    async def execute(self, params: Mapping[str, Any]) -> CapabilityResult:
        hwnd = params.get("hwnd")
        
        if hwnd is None:
             return CapabilityResult(
                capability_name=self.spec.name,
                status=CapabilityStatus.FAILED,
                failed=True,
                error=OmnixError("hwnd parameter is required.")
            )
            
        try:
            action_result = self._window_service.focus_window(hwnd=int(hwnd))
            
            if action_result.status == ActionStatus.EXECUTED:
                return CapabilityResult(
                    capability_name=self.spec.name,
                    status=CapabilityStatus.EXECUTED,
                    attempted=True,
                    executed=True,
                    verified=False,
                    action=action_result,
                    details={"hwnd": hwnd}
                )
            else:
                return CapabilityResult(
                    capability_name=self.spec.name,
                    status=CapabilityStatus.FAILED,
                    attempted=True,
                    failed=True,
                    action=action_result,
                    error=OmnixError(f"Window service failed to focus window: {action_result.details.get('error', 'Unknown error')}")
                )
        except Exception as e:
            return CapabilityResult(
                capability_name=self.spec.name,
                status=CapabilityStatus.FAILED,
                failed=True,
                error=OmnixError(f"Failed to focus window: {str(e)}")
            )


class WindowControlCapability(WindowCapabilityBase):
    """Base for window control capabilities (minimize, maximize, restore, close)."""
    
    def _control_window(self, hwnd: int, action_name: str) -> CapabilityResult:
        try:
            import win32gui
            import win32con
            from system.windows.window_service import _is_window

            if not _is_window(int(hwnd)):
                 return CapabilityResult(
                    capability_name=self.spec.name,
                    status=CapabilityStatus.FAILED,
                    failed=True,
                    error=OmnixError(f"Invalid window handle: {hwnd}")
                )
            
            if action_name == "minimize":
                win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
            elif action_name == "maximize":
                win32gui.ShowWindow(hwnd, win32con.SW_MAXIMIZE)
            elif action_name == "restore":
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            elif action_name == "close":
                win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
            else:
                 return CapabilityResult(
                    capability_name=self.spec.name,
                    status=CapabilityStatus.FAILED,
                    failed=True,
                    error=OmnixError(f"Unknown window action: {action_name}")
                )
            
            return CapabilityResult(
                capability_name=self.spec.name,
                status=CapabilityStatus.EXECUTED,
                attempted=True,
                executed=True,
                verified=False,  # Cannot confirm window state without further observation
                action=ActionResult(status=ActionStatus.EXECUTED, action_name=self.spec.name),
                details={"hwnd": hwnd, "action": action_name}
            )
        except ImportError:
            return CapabilityResult(
                capability_name=self.spec.name,
                status=CapabilityStatus.FAILED,
                failed=True,
                error=OmnixError("win32gui is not available.")
            )
        except Exception as e:
            return CapabilityResult(
                capability_name=self.spec.name,
                status=CapabilityStatus.FAILED,
                failed=True,
                error=OmnixError(f"Failed to {action_name} window: {str(e)}")
            )

    def _validate_hwnd(self, params: Mapping[str, Any]) -> Any:
        hwnd = params.get("hwnd")
        if hwnd is None:
            return None
        return int(hwnd)


class WindowMinimizeCapability(WindowControlCapability):
    """Capability to minimize a window by its HWND."""
    
    @property
    def spec(self) -> CapabilitySpec:
        return CapabilitySpec(
            name="desktop.window.minimize",
            version="1.0.0",
            description="Minimizes a window by its HWND.",
            parameters={
                "hwnd": CapabilityParameter(
                    name="hwnd",
                    type=ParamType.INTEGER,
                    description="The window handle (HWND) of the window to minimize.",
                    required=True
                )
            },
            tags={"desktop", "window", "minimize"}
        )

    async def execute(self, params: Mapping[str, Any]) -> CapabilityResult:
        hwnd = self._validate_hwnd(params)
        if hwnd is None:
             return CapabilityResult(
                capability_name=self.spec.name,
                status=CapabilityStatus.FAILED,
                failed=True,
                error=OmnixError("hwnd parameter is required.")
            )
        return self._control_window(hwnd, "minimize")


class WindowMaximizeCapability(WindowControlCapability):
    """Capability to maximize a window by its HWND."""
    
    @property
    def spec(self) -> CapabilitySpec:
        return CapabilitySpec(
            name="desktop.window.maximize",
            version="1.0.0",
            description="Maximizes a window by its HWND.",
            parameters={
                "hwnd": CapabilityParameter(
                    name="hwnd",
                    type=ParamType.INTEGER,
                    description="The window handle (HWND) of the window to maximize.",
                    required=True
                )
            },
            tags={"desktop", "window", "maximize"}
        )

    async def execute(self, params: Mapping[str, Any]) -> CapabilityResult:
        hwnd = self._validate_hwnd(params)
        if hwnd is None:
             return CapabilityResult(
                capability_name=self.spec.name,
                status=CapabilityStatus.FAILED,
                failed=True,
                error=OmnixError("hwnd parameter is required.")
            )
        return self._control_window(hwnd, "maximize")


class WindowRestoreCapability(WindowControlCapability):
    """Capability to restore a window by its HWND."""
    
    @property
    def spec(self) -> CapabilitySpec:
        return CapabilitySpec(
            name="desktop.window.restore",
            version="1.0.0",
            description="Restores a window from minimized/maximized state by its HWND.",
            parameters={
                "hwnd": CapabilityParameter(
                    name="hwnd",
                    type=ParamType.INTEGER,
                    description="The window handle (HWND) of the window to restore.",
                    required=True
                )
            },
            tags={"desktop", "window", "restore"}
        )

    async def execute(self, params: Mapping[str, Any]) -> CapabilityResult:
        hwnd = self._validate_hwnd(params)
        if hwnd is None:
             return CapabilityResult(
                capability_name=self.spec.name,
                status=CapabilityStatus.FAILED,
                failed=True,
                error=OmnixError("hwnd parameter is required.")
            )
        return self._control_window(hwnd, "restore")


class WindowCloseCapability(WindowControlCapability):
    """Capability to close a window by its HWND."""
    
    @property
    def spec(self) -> CapabilitySpec:
        return CapabilitySpec(
            name="desktop.window.close",
            version="1.0.0",
            description="Closes a window by its HWND by sending a WM_CLOSE message.",
            parameters={
                "hwnd": CapabilityParameter(
                    name="hwnd",
                    type=ParamType.INTEGER,
                    description="The window handle (HWND) of the window to close.",
                    required=True
                )
            },
            tags={"desktop", "window", "close"}
        )

    async def execute(self, params: Mapping[str, Any]) -> CapabilityResult:
        hwnd = self._validate_hwnd(params)
        if hwnd is None:
             return CapabilityResult(
                capability_name=self.spec.name,
                status=CapabilityStatus.FAILED,
                failed=True,
                error=OmnixError("hwnd parameter is required.")
            )
        return self._control_window(hwnd, "close")
