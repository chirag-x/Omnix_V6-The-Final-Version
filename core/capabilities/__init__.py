"""
Omnix V6 - Standard Capabilities.

Registers the built-in capabilities provided by the framework.
"""

from typing import Any, Optional

from core.capability_registry import CapabilityRegistry
from .filesystem import (
    FileReadCapability,
    FileWriteCapability,
    FileCreateCapability,
    FolderCreateCapability,
    FileDeleteCapability,
    DirectoryListCapability,
)
from .process import RunCommandCapability, ProcessIsRunningCapability
from .desktop_observation import ScreenSizeCapability, ForegroundWindowCapability, ScreenshotCapability
from .desktop_mouse import (
    MouseMoveCapability,
    MouseClickCapability,
    MouseDoubleClickCapability,
    MouseRightClickCapability,
    MouseScrollCapability,
    MouseDragCapability
)
from .desktop_keyboard import (
    KeyboardTypeCapability,
    KeyboardPressCapability,
    KeyboardHotkeyCapability
)
from .desktop_application import (
    ApplicationOpenCapability,
    ApplicationCloseCapability,
    ApplicationFocusCapability,
    ApplicationIsRunningCapability
)
from .desktop_window import (
    WindowListCapability,
    WindowFocusCapability,
    WindowMinimizeCapability,
    WindowMaximizeCapability,
    WindowRestoreCapability,
    WindowCloseCapability
)
from .browser_capabilities import (
    BrowserNavigateCapability,
    BrowserClickCapability,
    BrowserTypeCapability,
    BrowserExtractTextCapability,
    BrowserOpenCapability,
    BrowserCloseCapability,
    BrowserBackCapability,
    BrowserForwardCapability,
    BrowserReloadCapability,
    BrowserPressCapability,
    BrowserScrollCapability,
    BrowserHoverCapability,
    BrowserSelectCapability,
    BrowserWaitCapability,
    BrowserExtractPageCapability,
    BrowserDownloadCapability,
)
from core.services.browser_service import BrowserService


def _default_application_service():
    """Construct the canonical ApplicationService when one was not
    injected.  Lazy import keeps the capability package import-time
    cost low when the engine supplies a service through DI."""
    from system.application import WindowsApplicationService
    svc = WindowsApplicationService()
    try:
        svc.initialize()
    except Exception:
        # Engine will surface init failures through the readiness report.
        pass
    return svc


def _default_window_service():
    """Construct the canonical WindowService when one was not injected."""
    try:
        from core.services.window_service import WindowsWindowService
    except Exception:
        return None
    try:
        svc = WindowsWindowService()
        try:
            svc.initialize()
        except Exception:
            pass
        return svc
    except Exception:
        return None


def _default_input_service():
    """Construct the canonical InputService when one was not injected."""
    try:
        # Phase 17 fix: the InputService lives in ``system.input``,
        # not ``core.services``.  The previous import pointed at a
        # non-existent module and silently fell back to ``None``,
        # which caused the engine to register mouse/keyboard caps
        # with no service at all and silently dispatch through
        # ``pyautogui.position()`` blind.
        from system.input.input_service import WindowsInputService
    except Exception:
        return None
    try:
        svc = WindowsInputService()
        try:
            svc.initialize()
        except Exception:
            pass
        return svc
    except Exception:
        return None


def register_standard_capabilities(
    registry: CapabilityRegistry,
    *,
    browser_service: BrowserService = None,
    application_service: Any = None,
    input_service: Any = None,
    window_service: Any = None,
) -> None:
    """Register all built-in capabilities with the given registry.

    All service parameters are **optional**: when supplied, the
    relevant capabilities are wired in (Phase 8 + Phase 15).  When
    ``None``, the capability is either skipped (browser) or falls back
    to a default engine-owned instance (application / input / window).
    This keeps callers (tests, hosts) that do not want a particular
    service unaffected.
    """

    # Resolve canonical services.  The engine's wiring path
    # (register_standard_capabilities is called once at boot)
    # is responsible for passing the same instance here that
    # ``self.services`` holds, so there is exactly one
    # ApplicationService / InputService / WindowService per engine.
    app_service = application_service or _default_application_service()
    inp_service = input_service or _default_input_service()
    win_service = window_service or _default_window_service()

    # Filesystem
    registry.register(FileReadCapability())
    registry.register(FileWriteCapability())
    # Phase 12: extended filesystem surface.
    registry.register(FileCreateCapability())
    registry.register(FolderCreateCapability())
    registry.register(FileDeleteCapability())
    registry.register(DirectoryListCapability())

    # Process
    # IMPORTANT SECURITY WARNING: RunCommandCapability allows arbitrary code execution.
    # It must be controlled by safety systems.
    registry.register(RunCommandCapability())
    # Phase 12: read-only process observation (verification).
    registry.register(ProcessIsRunningCapability())

    # Desktop Observation
    registry.register(ScreenSizeCapability())
    registry.register(ForegroundWindowCapability())
    registry.register(ScreenshotCapability())

    # Desktop Mouse — use injected InputService when available.
    if inp_service is not None:
        registry.register(MouseMoveCapability(inp_service))
        registry.register(MouseClickCapability(inp_service))
        registry.register(MouseDoubleClickCapability(inp_service))
        registry.register(MouseRightClickCapability(inp_service))
        registry.register(MouseScrollCapability(inp_service))
        registry.register(MouseDragCapability(inp_service))
    else:
        registry.register(MouseMoveCapability())
        registry.register(MouseClickCapability())
        registry.register(MouseDoubleClickCapability())
        registry.register(MouseRightClickCapability())
        registry.register(MouseScrollCapability())
        registry.register(MouseDragCapability())

    # Desktop Keyboard
    if inp_service is not None:
        registry.register(KeyboardTypeCapability(inp_service))
        registry.register(KeyboardPressCapability(inp_service))
        registry.register(KeyboardHotkeyCapability(inp_service))
    else:
        registry.register(KeyboardTypeCapability())
        registry.register(KeyboardPressCapability())
        registry.register(KeyboardHotkeyCapability())

    # Desktop Application — use injected ApplicationService.
    registry.register(ApplicationOpenCapability(app_service))
    registry.register(ApplicationCloseCapability(app_service))
    registry.register(ApplicationFocusCapability(app_service))
    registry.register(ApplicationIsRunningCapability(app_service))

    # Desktop Window
    if win_service is not None:
        registry.register(WindowListCapability(win_service))
        registry.register(WindowFocusCapability(win_service))
        registry.register(WindowMinimizeCapability(win_service))
        registry.register(WindowMaximizeCapability(win_service))
        registry.register(WindowRestoreCapability(win_service))
        registry.register(WindowCloseCapability(win_service))
    else:
        registry.register(WindowListCapability())
        registry.register(WindowFocusCapability())
        registry.register(WindowMinimizeCapability())
        registry.register(WindowMaximizeCapability())
        registry.register(WindowRestoreCapability())
        registry.register(WindowCloseCapability())

    # Browser (Phase 8) — only registered when a BrowserService is
    # supplied.  Callers who do not want a browser simply omit the
    # argument and the capabilities are silently skipped.
    if browser_service is not None:
        registry.register(BrowserNavigateCapability(browser_service))
        registry.register(BrowserClickCapability(browser_service))
        registry.register(BrowserTypeCapability(browser_service))
        registry.register(BrowserExtractTextCapability(browser_service))
        registry.register(BrowserOpenCapability(browser_service))
        registry.register(BrowserCloseCapability(browser_service))
        registry.register(BrowserBackCapability(browser_service))
        registry.register(BrowserForwardCapability(browser_service))
        registry.register(BrowserReloadCapability(browser_service))
        registry.register(BrowserPressCapability(browser_service))
        registry.register(BrowserScrollCapability(browser_service))
        registry.register(BrowserHoverCapability(browser_service))
        registry.register(BrowserSelectCapability(browser_service))
        registry.register(BrowserWaitCapability(browser_service))
        registry.register(BrowserExtractPageCapability(browser_service))
        registry.register(BrowserDownloadCapability(browser_service))
