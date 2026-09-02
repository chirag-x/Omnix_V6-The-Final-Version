"""
Omnix V6 - Desktop Observation Capabilities.

Provides capabilities for checking screen size, capturing screenshots, 
and finding foreground windows.
"""

import asyncio
import os
from typing import Any, Mapping

from core.capability import CapabilitySpec, CapabilityParameter, ParamType
from core.results import CapabilityResult, CapabilityStatus, ActionResult, ActionStatus
from .base import BaseCapability
from core.errors import OmnixError
from system.windows.window_service import WindowsWindowService

class ScreenSizeCapability(BaseCapability):
    """Capability to get the screen dimensions."""
    
    @property
    def spec(self) -> CapabilitySpec:
        return CapabilitySpec(
            name="desktop.screen_size",
            version="1.0.0",
            description="Returns the current primary screen resolution (width and height).",
            parameters={},
            tags={"desktop", "observation", "screen"}
        )

    async def execute(self, params: Mapping[str, Any]) -> CapabilityResult:
        try:
            import pyautogui
            width, height = pyautogui.size()
            
            return CapabilityResult(
                capability_name=self.spec.name,
                status=CapabilityStatus.VERIFIED,
                attempted=True,
                executed=True,
                verified=True,
                action=ActionResult(status=ActionStatus.EXECUTED, action_name=self.spec.name),
                details={"width": width, "height": height}
            )
        except ImportError:
            return CapabilityResult(
                capability_name=self.spec.name,
                status=CapabilityStatus.FAILED,
                failed=True,
                error=OmnixError("pyautogui is not installed or available.")
            )
        except Exception as e:
            return CapabilityResult(
                capability_name=self.spec.name,
                status=CapabilityStatus.FAILED,
                failed=True,
                error=OmnixError(f"Failed to get screen size: {str(e)}")
            )


class ForegroundWindowCapability(BaseCapability):
    """Capability to get information about the currently focused window."""
    
    def __init__(self, window_service=None):
        self._window_service = window_service or WindowsWindowService()
        if not getattr(self._window_service, 'initialized', False):
            self._window_service.initialize()
            
    @property
    def spec(self) -> CapabilitySpec:
        return CapabilitySpec(
            name="desktop.foreground_window",
            version="1.0.0",
            description="Returns information about the window currently in the foreground.",
            parameters={},
            tags={"desktop", "observation", "window", "focus"}
        )

    async def execute(self, params: Mapping[str, Any]) -> CapabilityResult:
        try:
            # We don't have a direct get_foreground_window in WindowService, 
            # so we'll use pywin32 win32gui directly here just for this observation
            import win32gui
            import win32process
            import psutil
            
            hwnd = win32gui.GetForegroundWindow()
            if not hwnd:
                return CapabilityResult(
                    capability_name=self.spec.name,
                    status=CapabilityStatus.VERIFIED,
                    attempted=True,
                    executed=True,
                    verified=True,
                    action=ActionResult(status=ActionStatus.EXECUTED, action_name=self.spec.name),
                    details={"has_foreground": False}
                )
                
            title = win32gui.GetWindowText(hwnd)
            rect = win32gui.GetWindowRect(hwnd)
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            
            process_name = ""
            try:
                process = psutil.Process(pid)
                process_name = process.name()
            except (psutil.NoSuchProcess, psutil.AccessDenied, Exception):
                pass
                
            return CapabilityResult(
                capability_name=self.spec.name,
                status=CapabilityStatus.VERIFIED,
                attempted=True,
                executed=True,
                verified=True,
                action=ActionResult(status=ActionStatus.EXECUTED, action_name=self.spec.name),
                details={
                    "has_foreground": True,
                    "hwnd": hwnd,
                    "title": title,
                    "rect": {"left": rect[0], "top": rect[1], "right": rect[2], "bottom": rect[3]},
                    "process_id": pid,
                    "process_name": process_name
                }
            )
            
        except ImportError as e:
             return CapabilityResult(
                capability_name=self.spec.name,
                status=CapabilityStatus.FAILED,
                failed=True,
                error=OmnixError(f"Required module for windows interaction is missing: {str(e)}")
            )
        except Exception as e:
            return CapabilityResult(
                capability_name=self.spec.name,
                status=CapabilityStatus.FAILED,
                failed=True,
                error=OmnixError(f"Failed to get foreground window: {str(e)}")
            )

class ScreenshotCapability(BaseCapability):
    """Capability to capture a screenshot of the desktop.

    Phase 17 / System 3 upgrade:
      * The result details now include ``width`` and ``height``
        in addition to ``path``, so the vision layer can
        populate :class:`ScreenshotMetadata` without a
        second capability call.
      * The result details include ``monitor_id`` so
        :func:`vision.safety.coordinates.validate_coordinates`
        can route the bounding-box check to the right monitor.
      * An optional ``monitor_id`` parameter is accepted; when
        set, the screenshot is clipped to the bounds of the
        named monitor.  Multi-monitor hosts return a
        per-monitor image rather than a stitched virtual
        desktop.  When omitted, behaviour is unchanged
        (whole virtual desktop).

    The capability is still **observation-only**; it does not
    modify the screen or call any input capability.
    """

    @property
    def spec(self) -> CapabilitySpec:
        return CapabilitySpec(
            name="desktop.screenshot",
            version="1.1.0",
            description="Captures a screenshot of the desktop and saves it to a file. ONLY an observation capability.",
            parameters={
                 "path": CapabilityParameter(
                    name="path",
                    type=ParamType.PATH,
                    description="Absolute path to save the screenshot image (e.g. .png).",
                    required=True
                ),
                 "monitor_id": CapabilityParameter(
                    name="monitor_id",
                    type=ParamType.STRING,
                    description="Optional monitor id (from vision.screen.monitor). When set, the screenshot is clipped to that monitor's physical bounds. When omitted, the entire virtual desktop is captured.",
                    required=False
                ),
            },
            tags={"desktop", "observation", "screenshot", "multimonitor"}
        )

    async def execute(self, params: Mapping[str, Any]) -> CapabilityResult:
        path = params.get("path")
        if not path:
             return CapabilityResult(
                capability_name=self.spec.name,
                status=CapabilityStatus.FAILED,
                failed=True,
                error=OmnixError("Path parameter is required.")
            )

        if not os.path.isabs(path):
            return CapabilityResult(
                capability_name=self.spec.name,
                status=CapabilityStatus.FAILED,
                failed=True,
                error=OmnixError(f"Path must be absolute: {path}")
            )

        monitor_id_param = params.get("monitor_id")

        try:
            # Running synchronous pyautogui call in an executor thread
            loop = asyncio.get_running_loop()

            def _take_screenshot():
                import pyautogui
                import os
                # Ensure directory exists
                os.makedirs(os.path.dirname(path), exist_ok=True)

                # Resolve monitor region, if requested.  When
                # the host is multi-monitor, pyautogui.screenshot
                # accepts ``region=(left, top, width, height)``.
                region = None
                effective_monitor_id = None
                try:
                    from vision.screen.monitor import (
                        enumerate_monitors,
                        get_monitor_by_id,
                    )
                    monitors = enumerate_monitors()
                    if monitor_id_param:
                        m = get_monitor_by_id(str(monitor_id_param))
                        if m is not None:
                            l, t, r, b = m.bounds_physical_px
                            region = (int(l), int(t), int(r - l), int(b - t))
                            effective_monitor_id = m.monitor_id
                    elif len(monitors) > 1:
                        # Multi-monitor host: default to the
                        # primary monitor's bounds so the
                        # screenshot is not a stitched virtual
                        # desktop.  The vision layer will
                        # re-acquire per-monitor shots as
                        # needed.
                        for m in monitors:
                            if m.is_primary:
                                l, t, r, b = m.bounds_physical_px
                                region = (int(l), int(t), int(r - l), int(b - t))
                                effective_monitor_id = m.monitor_id
                                break
                except Exception:
                    # Monitor enumeration is best-effort; the
                    # screenshot still works without it.
                    region = None
                    effective_monitor_id = None

                if region is not None:
                    pyautogui.screenshot(path, region=region)
                else:
                    pyautogui.screenshot(path)
                return effective_monitor_id

            effective_monitor_id = await loop.run_in_executor(None, _take_screenshot)

            # Read the actual size off the saved PNG so the
            # caller does not have to trust our local math.
            width = 0
            height = 0
            try:
                from PIL import Image  # type: ignore
                with Image.open(path) as img:
                    width, height = img.size
            except Exception:
                # Fall back to pyautogui.size() if PIL is
                # not available.
                try:
                    import pyautogui
                    width, height = pyautogui.size()
                except Exception:
                    pass

            details: dict = {"path": path, "width": int(width), "height": int(height)}
            if effective_monitor_id:
                details["monitor_id"] = effective_monitor_id
            elif monitor_id_param:
                details["monitor_id"] = str(monitor_id_param)

            return CapabilityResult(
                capability_name=self.spec.name,
                status=CapabilityStatus.VERIFIED,
                attempted=True,
                executed=True,
                verified=True,
                action=ActionResult(status=ActionStatus.EXECUTED, action_name=self.spec.name),
                details=details
            )
        except ImportError:
            return CapabilityResult(
                capability_name=self.spec.name,
                status=CapabilityStatus.FAILED,
                failed=True,
                error=OmnixError("pyautogui is not installed or available.")
            )
        except Exception as e:
            return CapabilityResult(
                capability_name=self.spec.name,
                status=CapabilityStatus.FAILED,
                failed=True,
                error=OmnixError(f"Failed to capture screenshot: {str(e)}")
            )
