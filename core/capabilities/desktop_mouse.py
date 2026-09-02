"""
Omnix V6 - Desktop Mouse Capabilities (Phase 17 rewrite).

Six capabilities (``move``, ``click``, ``right_click``,
``double_click``, ``scroll``, ``drag``) that all acquire their
target window the same way the keyboard caps do.

Before Phase 17 these capabilities read ``pyautogui.position()`` or
a raw ``(x, y)`` from the caller and dispatched blind into whatever
window happened to be focused.  The "Notepad opens, text appears
in VS Code" failure mode was a direct consequence.

After Phase 17 every mouse capability spec carries the same four
target parameters as the keyboard caps, and every ``execute()``
runs through :func:`core.capabilities._dispatch.dispatch_with_target`.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

from core.capability import CapabilitySpec, CapabilityParameter, ParamType
from core.results import CapabilityResult, CapabilityStatus
from .base import BaseCapability
from core.errors import OmnixError
from system.input.input_service import WindowsInputService

from core.capabilities._dispatch import (
    _LazyResolver,
    dispatch_with_target,
)
from core.grounding.resolved_target import ResolvedTarget, TargetResolutionStatus
from core.grounding.target_resolver import TargetResolver
from vision.grounded_element import GroundedElement
from vision.observations.targets import TargetCandidate


def _mouse_target_params(params: Mapping[str, Any]) -> Mapping[str, Any]:
    """The mouse caps accept the same target hints as the keyboard
    caps.  Strip the (x, y) coordinate from the params before
    forwarding as the dispatch envelope's target hints.
    """
    return {
        k: v for k, v in params.items()
        if k not in ("x", "y", "amount", "button", "duration_s", "clicks",
                    "target", "resolved_target", "target_input")
    }


def _resolve_target_from_params(params: Mapping[str, Any]) -> Optional[ResolvedTarget]:
    """
    Extract and resolve a target from capability params.

    Looks for target/resolved_target/target_input in params. If found,
    creates a TargetResolver and resolves the target.

    Args:
        params: Capability parameters dict

    Returns:
        ResolvedTarget if successful, None if no target or resolution failed
    """
    # Check for target in params (accept multiple aliases)
    target_input = (params.get("target") or
                   params.get("resolved_target") or
                   params.get("target_input"))

    if target_input is None:
        return None

    # Create resolver with no screen bounds (mouse caps don't need them)
    resolver = TargetResolver(
        screen_width=None,
        screen_height=None,
    )

    result = resolver.resolve(target_input)

    if result.status == TargetResolutionStatus.RESOLVED:
        return result.target

    # Resolution failed - return None and let caller handle
    return None


def _get_target_or_coords(params: Mapping[str, Any]) -> Optional[ResolvedTarget]:
    """
    Get either a resolved target (if target param provided) or create
    one from x/y params.

    Returns:
        ResolvedTarget if target or x/y provided, None otherwise
    """
    # First try to resolve from target param
    resolved = _resolve_target_from_params(params)
    if resolved is not None:
        return resolved

    # Fall back to x/y params
    x = params.get("x")
    y = params.get("y")
    if x is not None and y is not None:
        try:
            return ResolvedTarget.coordinate(
                x=int(x),
                y=int(y),
                source="coordinate",
            )
        except (ValueError, TypeError):
            return None

    return None


class MouseCapabilityBase(BaseCapability):
    """Base class for mouse capabilities.

    Same shape as :class:`KeyboardCapabilityBase` — owns a single
    :class:`WindowsInputService` and a process-local
    :class:`_LazyResolver`.
    """

    def __init__(
        self,
        input_service: Any = None,
        *,
        app_service: Any = None,
        window_service: Any = None,
    ) -> None:
        if input_service is not None:
            self._input_service = input_service
        else:
            self._input_service = WindowsInputService()
            try:
                if not getattr(self._input_service, "initialized", False):
                    self._input_service.initialize()
            except Exception:
                pass
        self._resolver_holder = _LazyResolver(
            app_service=app_service,
            window_service=window_service,
        )


class MouseMoveCapability(MouseCapabilityBase):
    """Capability to move the mouse pointer to specific coordinates."""

    @property
    def spec(self) -> CapabilitySpec:
        return CapabilitySpec(
            name="desktop.mouse.move",
            version="1.2.0",
            description=(
                "Moves the mouse pointer to the specified x,y coordinates or target.  "
                "Accepts either (x, y) or a target (ResolvedTarget, GroundedElement, etc.). "
                "When a target hint is supplied, the target window is "
                "focused first so the move is into a known foreground."
            ),
            parameters={
                "x": CapabilityParameter(
                    name="x", type=ParamType.INTEGER,
                    description="Absolute X coordinate to move to (or use target instead).",
                    required=False, default=None,
                ),
                "y": CapabilityParameter(
                    name="y", type=ParamType.INTEGER,
                    description="Absolute Y coordinate to move to (or use target instead).",
                    required=False, default=None,
                ),
                "target": CapabilityParameter(
                    name="target", type=ParamType.ANY,
                    description="Target object (ResolvedTarget, GroundedElement, TargetCandidate, or dict with x/y/bbox).",
                    required=False, default=None,
                ),
                "target_app_name": CapabilityParameter(
                    name="target_app_name", type=ParamType.STRING,
                    description="Optional target app name.",
                    required=False, default=None,
                ),
                "target_window_title": CapabilityParameter(
                    name="target_window_title", type=ParamType.STRING,
                    description="Optional target window title.",
                    required=False, default=None,
                ),
                "target_window_hwnd": CapabilityParameter(
                    name="target_window_hwnd", type=ParamType.INTEGER,
                    description="Optional target window HWND.",
                    required=False, default=None,
                ),
            },
            tags={"desktop", "mouse", "move"},
        )

    async def execute(self, params: Mapping[str, Any]) -> CapabilityResult:
        # Stage 18.6: Try to get target first (precedence over x/y)
        resolved_target = _get_target_or_coords(params)

        if resolved_target is not None:
            # Use resolved target coordinates
            x = resolved_target.center_x
            y = resolved_target.center_y
        else:
            # Fall back to direct x/y params
            x = params.get("x")
            y = params.get("y")

        def _pre_check() -> Optional[CapabilityResult]:
            if x is None or y is None:
                return CapabilityResult(
                    capability_name=self.spec.name,
                    status=CapabilityStatus.FAILED,
                    failed=True,
                    error=OmnixError("Either (x, y) or target parameter is required."),
                )
            return None

        return dispatch_with_target(
            capability_name=self.spec.name,
            params=_mouse_target_params(params),
            resolver_holder=self._resolver_holder,
            primitive=self._input_service.move_mouse,
            primitive_kwargs={"x": int(x), "y": int(y)},
            pre_check=_pre_check,
            extra_details={"x": x, "y": y},
        )


class MouseClickCapability(MouseCapabilityBase):
    """Capability to click the mouse at specified coordinates."""

    @property
    def spec(self) -> CapabilitySpec:
        return CapabilitySpec(
            name="desktop.mouse.click",
            version="1.2.0",
            description=(
                "Clicks the left mouse button at (x, y) or target.  "
                "Accepts either (x, y) or a target object. When a "
                "target hint is supplied, the target window is "
                "focused first so the click lands in the intended "
                "window, not whatever happens to be in the "
                "foreground."
            ),
            parameters={
                "x": CapabilityParameter(
                    name="x", type=ParamType.INTEGER,
                    description="Optional X coordinate to click (or use target).",
                    required=False, default=None,
                ),
                "y": CapabilityParameter(
                    name="y", type=ParamType.INTEGER,
                    description="Optional Y coordinate to click (or use target).",
                    required=False, default=None,
                ),
                "target": CapabilityParameter(
                    name="target", type=ParamType.ANY,
                    description="Target object (ResolvedTarget, GroundedElement, TargetCandidate, or dict with x/y/bbox).",
                    required=False, default=None,
                ),
                "button": CapabilityParameter(
                    name="button", type=ParamType.STRING,
                    description="Button to click (left, right, middle).",
                    required=False, default="left",
                ),
                "clicks": CapabilityParameter(
                    name="clicks", type=ParamType.INTEGER,
                    description="Number of clicks.",
                    required=False, default=1,
                ),
                "target_app_name": CapabilityParameter(
                    name="target_app_name", type=ParamType.STRING,
                    description="Optional target app name.",
                    required=False, default=None,
                ),
                "target_window_title": CapabilityParameter(
                    name="target_window_title", type=ParamType.STRING,
                    description="Optional target window title.",
                    required=False, default=None,
                ),
                "target_window_hwnd": CapabilityParameter(
                    name="target_window_hwnd", type=ParamType.INTEGER,
                    description="Optional target window HWND.",
                    required=False, default=None,
                ),
            },
            tags={"desktop", "mouse", "click"},
        )

    async def execute(self, params: Mapping[str, Any]) -> CapabilityResult:
        # Stage 18.6: Try to get target first (precedence over x/y)
        resolved_target = _get_target_or_coords(params)

        if resolved_target is not None:
            x = resolved_target.center_x
            y = resolved_target.center_y
        else:
            x = params.get("x")
            y = params.get("y")

        button = str(params.get("button", "left"))
        clicks = int(params.get("clicks", 1) or 1)

        def _pre_check() -> Optional[CapabilityResult]:
            if (x is None) != (y is None):
                return CapabilityResult(
                    capability_name=self.spec.name,
                    status=CapabilityStatus.FAILED,
                    failed=True,
                    error=OmnixError(
                        "Both x and y must be provided, or neither "
                        "to click at current cursor position."
                    ),
                )
            return None

        if x is None or y is None:
            # Use the current cursor position via pyautogui.
            # win32api.GetCursorPos fails with "Access is denied"
            # on hardened terminals.
            import pyautogui
            x_cur, y_cur = pyautogui.position()
            x_int, y_int = int(x_cur), int(y_cur)
        else:
            x_int, y_int = int(x), int(y)

        return dispatch_with_target(
            capability_name=self.spec.name,
            params=_mouse_target_params(params),
            resolver_holder=self._resolver_holder,
            primitive=self._input_service.click,
            primitive_kwargs={
                "x": x_int, "y": y_int,
                "button": button, "clicks": clicks,
            },
            pre_check=_pre_check,
            extra_details={
                "x": x_int, "y": y_int,
                "button": button, "clicks": clicks,
            },
        )


class MouseRightClickCapability(MouseCapabilityBase):
    """Capability to right-click the mouse."""

    @property
    def spec(self) -> CapabilitySpec:
        return CapabilitySpec(
            name="desktop.mouse.right_click",
            version="1.2.0",
            description="Right-clicks the mouse at (x, y) or target. Accepts either (x, y) or a target object.",
            parameters={
                "x": CapabilityParameter(
                    name="x", type=ParamType.INTEGER,
                    description="Optional X coordinate to right-click (or use target).",
                    required=False, default=None,
                ),
                "y": CapabilityParameter(
                    name="y", type=ParamType.INTEGER,
                    description="Optional Y coordinate to right-click (or use target).",
                    required=False, default=None,
                ),
                "target": CapabilityParameter(
                    name="target", type=ParamType.ANY,
                    description="Target object (ResolvedTarget, GroundedElement, TargetCandidate, or dict with x/y/bbox).",
                    required=False, default=None,
                ),
                "target_app_name": CapabilityParameter(
                    name="target_app_name", type=ParamType.STRING,
                    description="Optional target app name.",
                    required=False, default=None,
                ),
                "target_window_title": CapabilityParameter(
                    name="target_window_title", type=ParamType.STRING,
                    description="Optional target window title.",
                    required=False, default=None,
                ),
                "target_window_hwnd": CapabilityParameter(
                    name="target_window_hwnd", type=ParamType.INTEGER,
                    description="Optional target window HWND.",
                    required=False, default=None,
                ),
            },
            tags={"desktop", "mouse", "click", "right-click"},
        )

    async def execute(self, params: Mapping[str, Any]) -> CapabilityResult:
        # Stage 18.6: Try to get target first (precedence over x/y)
        resolved_target = _get_target_or_coords(params)

        if resolved_target is not None:
            x = resolved_target.center_x
            y = resolved_target.center_y
        else:
            x = params.get("x")
            y = params.get("y")

        def _pre_check() -> Optional[CapabilityResult]:
            if (x is None) != (y is None):
                return CapabilityResult(
                    capability_name=self.spec.name,
                    status=CapabilityStatus.FAILED,
                    failed=True,
                    error=OmnixError(
                        "Both x and y must be provided, or neither "
                        "to right-click at current cursor position."
                    ),
                )
            return None

        if x is None or y is None:
            import pyautogui
            x_cur, y_cur = pyautogui.position()
            x_int, y_int = int(x_cur), int(y_cur)
        else:
            x_int, y_int = int(x), int(y)

        return dispatch_with_target(
            capability_name=self.spec.name,
            params=_mouse_target_params(params),
            resolver_holder=self._resolver_holder,
            primitive=self._input_service.click,
            primitive_kwargs={
                "x": x_int, "y": y_int,
                "button": "right", "clicks": 1,
            },
            pre_check=_pre_check,
            extra_details={"x": x_int, "y": y_int, "button": "right"},
        )


class MouseDoubleClickCapability(MouseCapabilityBase):
    """Capability to double-click the mouse."""

    @property
    def spec(self) -> CapabilitySpec:
        return CapabilitySpec(
            name="desktop.mouse.double_click",
            version="1.2.0",
            description="Double-clicks the left mouse button at (x, y) or target. Accepts either (x, y) or a target object.",
            parameters={
                "x": CapabilityParameter(
                    name="x", type=ParamType.INTEGER,
                    description="Optional X coordinate to double-click (or use target).",
                    required=False, default=None,
                ),
                "y": CapabilityParameter(
                    name="y", type=ParamType.INTEGER,
                    description="Optional Y coordinate to double-click (or use target).",
                    required=False, default=None,
                ),
                "target": CapabilityParameter(
                    name="target", type=ParamType.ANY,
                    description="Target object (ResolvedTarget, GroundedElement, TargetCandidate, or dict with x/y/bbox).",
                    required=False, default=None,
                ),
                "target_app_name": CapabilityParameter(
                    name="target_app_name", type=ParamType.STRING,
                    description="Optional target app name.",
                    required=False, default=None,
                ),
                "target_window_title": CapabilityParameter(
                    name="target_window_title", type=ParamType.STRING,
                    description="Optional target window title.",
                    required=False, default=None,
                ),
                "target_window_hwnd": CapabilityParameter(
                    name="target_window_hwnd", type=ParamType.INTEGER,
                    description="Optional target window HWND.",
                    required=False, default=None,
                ),
            },
            tags={"desktop", "mouse", "click", "double-click"},
        )

    async def execute(self, params: Mapping[str, Any]) -> CapabilityResult:
        # Stage 18.6: Try to get target first (precedence over x/y)
        resolved_target = _get_target_or_coords(params)

        if resolved_target is not None:
            x = resolved_target.center_x
            y = resolved_target.center_y
        else:
            x = params.get("x")
            y = params.get("y")

        def _pre_check() -> Optional[CapabilityResult]:
            if (x is None) != (y is None):
                return CapabilityResult(
                    capability_name=self.spec.name,
                    status=CapabilityStatus.FAILED,
                    failed=True,
                    error=OmnixError(
                        "Both x and y must be provided, or neither "
                        "to double-click at current cursor position."
                    ),
                )
            return None

        if x is None or y is None:
            import pyautogui
            x_cur, y_cur = pyautogui.position()
            x_int, y_int = int(x_cur), int(y_cur)
        else:
            x_int, y_int = int(x), int(y)

        return dispatch_with_target(
            capability_name=self.spec.name,
            params=_mouse_target_params(params),
            resolver_holder=self._resolver_holder,
            primitive=self._input_service.click,
            primitive_kwargs={
                "x": x_int, "y": y_int,
                "button": "left", "clicks": 2,
            },
            pre_check=_pre_check,
            extra_details={"x": x_int, "y": y_int, "clicks": 2},
        )


class MouseDragCapability(MouseCapabilityBase):
    """Capability to drag the mouse from current position to (x, y)."""

    @property
    def spec(self) -> CapabilitySpec:
        return CapabilitySpec(
            name="desktop.mouse.drag",
            version="1.2.0",
            description=(
                "Drags the mouse to (x, y) or target while holding the button.  "
                "Accepts either (x, y) or a target object. When a "
                "target hint is supplied, the target window is "
                "focused first so the drag begins in the right window."
            ),
            parameters={
                "x": CapabilityParameter(
                    name="x", type=ParamType.INTEGER,
                    description="Absolute X coordinate to drag to (or use target).",
                    required=False, default=None,
                ),
                "y": CapabilityParameter(
                    name="y", type=ParamType.INTEGER,
                    description="Absolute Y coordinate to drag to (or use target).",
                    required=False, default=None,
                ),
                "target": CapabilityParameter(
                    name="target", type=ParamType.ANY,
                    description="Target object (ResolvedTarget, GroundedElement, TargetCandidate, or dict with x/y/bbox).",
                    required=False, default=None,
                ),
                "button": CapabilityParameter(
                    name="button", type=ParamType.STRING,
                    description="Button to hold (left, right, middle).",
                    required=False, default="left",
                ),
                "duration_s": CapabilityParameter(
                    name="duration_s", type=ParamType.FLOAT,
                    description="Drag duration in seconds.",
                    required=False, default=0.5,
                ),
                "target_app_name": CapabilityParameter(
                    name="target_app_name", type=ParamType.STRING,
                    description="Optional target app name.",
                    required=False, default=None,
                ),
                "target_window_title": CapabilityParameter(
                    name="target_window_title", type=ParamType.STRING,
                    description="Optional target window title.",
                    required=False, default=None,
                ),
                "target_window_hwnd": CapabilityParameter(
                    name="target_window_hwnd", type=ParamType.INTEGER,
                    description="Optional target window HWND.",
                    required=False, default=None,
                ),
            },
            tags={"desktop", "mouse", "drag"},
        )

    async def execute(self, params: Mapping[str, Any]) -> CapabilityResult:
        # Stage 18.6: Try to get target first (precedence over x/y)
        resolved_target = _get_target_or_coords(params)

        if resolved_target is not None:
            x = resolved_target.center_x
            y = resolved_target.center_y
        else:
            x = params.get("x")
            y = params.get("y")

        button = str(params.get("button", "left"))
        duration_s = float(params.get("duration_s", 0.5) or 0.5)

        def _pre_check() -> Optional[CapabilityResult]:
            if x is None or y is None:
                return CapabilityResult(
                    capability_name=self.spec.name,
                    status=CapabilityStatus.FAILED,
                    failed=True,
                    error=OmnixError("Both x and y parameters are required."),
                )
            return None

        import pyautogui
        start_x, start_y = pyautogui.position()

        return dispatch_with_target(
            capability_name=self.spec.name,
            params=_mouse_target_params(params),
            resolver_holder=self._resolver_holder,
            primitive=self._input_service.drag,
            primitive_kwargs={
                "x1": int(start_x), "y1": int(start_y),
                "x2": int(x), "y2": int(y),
                "button": button, "duration_s": duration_s,
            },
            pre_check=_pre_check,
            extra_details={
                "x": x, "y": y, "button": button,
                "duration_s": duration_s,
            },
        )


class MouseScrollCapability(MouseCapabilityBase):
    """Capability to scroll the mouse wheel."""

    @property
    def spec(self) -> CapabilitySpec:
        return CapabilitySpec(
            name="desktop.mouse.scroll",
            version="1.2.0",
            description="Scrolls the mouse wheel at (x, y) or target. Accepts either (x, y) or a target object.",
            parameters={
                "amount": CapabilityParameter(
                    name="amount", type=ParamType.INTEGER,
                    description=(
                        "Amount to scroll. Positive for up, "
                        "negative for down."
                    ),
                    required=True,
                ),
                "vertical": CapabilityParameter(
                    name="vertical", type=ParamType.BOOLEAN,
                    description="True for vertical scroll, False for horizontal.",
                    required=False, default=True,
                ),
                "x": CapabilityParameter(
                    name="x", type=ParamType.INTEGER,
                    description="X coordinate for scroll (or use target).",
                    required=False, default=None,
                ),
                "y": CapabilityParameter(
                    name="y", type=ParamType.INTEGER,
                    description="Y coordinate for scroll (or use target).",
                    required=False, default=None,
                ),
                "target": CapabilityParameter(
                    name="target", type=ParamType.ANY,
                    description="Target object (ResolvedTarget, GroundedElement, TargetCandidate, or dict with x/y/bbox).",
                    required=False, default=None,
                ),
                "target_app_name": CapabilityParameter(
                    name="target_app_name", type=ParamType.STRING,
                    description="Optional target app name.",
                    required=False, default=None,
                ),
                "target_window_title": CapabilityParameter(
                    name="target_window_title", type=ParamType.STRING,
                    description="Optional target window title.",
                    required=False, default=None,
                ),
                "target_window_hwnd": CapabilityParameter(
                    name="target_window_hwnd", type=ParamType.INTEGER,
                    description="Optional target window HWND.",
                    required=False, default=None,
                ),
            },
            tags={"desktop", "mouse", "scroll"},
        )

    async def execute(self, params: Mapping[str, Any]) -> CapabilityResult:
        # Stage 18.6: Try to get target first (precedence over x/y)
        resolved_target = _get_target_or_coords(params)

        if resolved_target is not None:
            x = resolved_target.center_x
            y = resolved_target.center_y
        else:
            x = params.get("x")
            y = params.get("y")

        amount = int(params.get("amount", 0) or 0)
        vertical = bool(params.get("vertical", True))

        def _pre_check() -> Optional[CapabilityResult]:
            if (x is None) != (y is None):
                return CapabilityResult(
                    capability_name=self.spec.name,
                    status=CapabilityStatus.FAILED,
                    failed=True,
                    error=OmnixError(
                        "Both x and y must be provided, or neither "
                        "to scroll at current cursor position."
                    ),
                )
            return None

        if x is None or y is None:
            # Use the current cursor position via pyautogui.
            import pyautogui
            x_cur, y_cur = pyautogui.position()
            x_int, y_int = int(x_cur), int(y_cur)
        else:
            x_int, y_int = int(x), int(y)

        return dispatch_with_target(
            capability_name=self.spec.name,
            params=_mouse_target_params(params),
            resolver_holder=self._resolver_holder,
            primitive=self._input_service.scroll,
            primitive_kwargs={
                "x": x_int, "y": y_int,
                "clicks": amount, "vertical": vertical,
            },
            pre_check=_pre_check,
            extra_details={
                "x": x_int, "y": y_int,
                "amount": amount, "vertical": vertical,
            },
        )
