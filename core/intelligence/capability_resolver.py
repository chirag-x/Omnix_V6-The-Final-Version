import logging
from typing import Any, Dict, Optional, Tuple

from core.capability_registry import CapabilityRegistry
from core.capability import CapabilitySpec
from core.orchestration import Intent, IntentKind

logger = logging.getLogger(__name__)

class CapabilityResolver:
    """The Capability Resolver (Phase 22).
    
    Responsible for mapping a structured Intent to the best matching capability
    registered in the system.
    """

    def __init__(self, registry: CapabilityRegistry) -> None:
        self._registry = registry

    def resolve(self, intent: Intent, *, implicit_target: Optional[str] = None) -> Optional[Tuple[str, Dict[str, Any], Optional[Dict[str, Any]]]]:
        """Resolve an Intent to a capability.
        
        Returns a tuple of:
          - capability_name (str)
          - param_overrides (Dict)
          - expected_effect (Optional[Dict])
        Or None if no native capability can resolve this intent.
        """
        kind = intent.kind
        params = intent.parameters or {}

        # 1. Direct App controls
        if kind == IntentKind.OPEN_APPLICATION:
            return "desktop.application.open", {}, {
                "check_name": "app_launched",
                "expected": True,
                "timeout_s": 30.0,
                "description": "the named application process is running",
            }
        
        if kind == IntentKind.CLOSE_APPLICATION:
            return "desktop.application.close", {}, {
                "check_name": "app_closed",
                "expected": True,
                "timeout_s": 15.0,
                "description": "the named application process has exited",
            }

        if kind == IntentKind.FOCUS_APPLICATION:
            return "desktop.application.focus", {}, {
                "check_name": "app_focused",
                "expected": True,
                "timeout_s": 5.0,
                "description": "the named application is the foreground window",
            }

        # 2. Control Application (Typing, etc)
        if kind == IntentKind.CONTROL_APPLICATION:
            action = params.get("action", "")
            if action == "type":
                # Ensure the capability exists
                if self._registry.get("desktop.keyboard.type"):
                    return "desktop.keyboard.type", {}, None
            elif action == "press":
                if self._registry.get("desktop.keyboard.press"):
                    return "desktop.keyboard.press", {}, None
            # Fallback to is_running
            return "desktop.application.is_running", {}, None

        # 3. File operations
        if kind == IntentKind.FILE_FIND:
            return "file.read", {}, None
        if kind == IntentKind.FILE_DELETE:
            if "path" in params:
                return "file.delete", {}, None

        # 4. Windows
        if kind == IntentKind.WINDOW_MANAGE:
            return "desktop.window.list", {}, None
        if kind == IntentKind.QUERY_STATUS:
            return "desktop.foreground_window", {}, None

        # 5. UI Targets
        if kind == IntentKind.UI_CLICK_TARGET:
            return "desktop.mouse.click", {}, None

        # 6. Browser Intents
        if kind == IntentKind.BROWSER_NAVIGATE:
            action = params.get("action", "")
            if action == "search":
                # Special hybrid fallback: browser search.
                # If we don't have a direct "browser.search" capability, we might need
                # a multi-step sequence, but the Resolver itself just returns the best capability
                # if one exists. Let's see if browser.search exists.
                if self._registry.get("browser.search"):
                    return "browser.search", {}, None
                # If not, we return None and the NativePlanner might do something, or escalate.
                return None
            else:
                if "url" in params:
                    return "browser.navigate", {}, None

        return None
