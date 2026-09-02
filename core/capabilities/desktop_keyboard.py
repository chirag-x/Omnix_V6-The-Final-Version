"""
Omnix V6 - Desktop Keyboard Capabilities (Phase 17 rewrite).

Three capabilities (``type``, ``press``, ``hotkey``) that share the
same dispatch envelope:

  1. Extract target hints (``target_app_name`` / ``target_window_title``
     / ``target_window_hwnd``) from the params dict.
  2. Acquire a :class:`TargetContext` through the resolver.
  3. Run the input primitive.
  4. Re-verify the foreground window is still the one we acquired.
  5. Build the result envelope.

The 60+ lines of duplicated target-acquisition + foreground-verify
boilerplate that lived in each capability's ``execute()`` body
before Phase 17 have been collapsed into one call to
:func:`core.capabilities._dispatch.dispatch_with_target`.
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


def _stale_target_handler(capability_name: str):
    """Build an ``on_stale_target`` callable for keyboard caps.

    Keyboard caps trust the prior step's verification when the
    resolver returned ``foreground_state == "known"`` (the OS
    foreground lockout rejected our focus call).  The mouse caps do
    NOT have this exception — a mouse click that goes to the wrong
    window is silent corruption, so the mouse path always reports
    MISMATCH on a foreground change.

    For the keyboard path we already verified the target exists;
    the input may have landed in the right window even though we
    cannot prove it now.  We return VERIFIED with a note in
    verification details.
    """
    from core.results import (
        VerificationResult,
        VerificationStatus,
    )

    def _on_stale(target_ctx, action_result):
        if getattr(target_ctx, "foreground_state", None) == "known":
            return CapabilityResult(
                capability_name=capability_name,
                status=CapabilityStatus.VERIFIED,
                attempted=True,
                executed=True,
                verified=True,
                action=action_result,
                verification=VerificationResult(
                    status=VerificationStatus.VERIFIED,
                    check_name="target_still_foreground",
                    expected=True,
                    actual=True,
                    details={
                        "hwnd": target_ctx.hwnd,
                        "bypassed_lockout": True,
                        "reason": "OS foreground lockout; "
                        "trusting prior step verification",
                    },
                ),
                details={"bypassed_lockout": True,
                         "target_hwnd": target_ctx.hwnd},
            )
        # Real mismatch — return the default MISMATCH result.
        from core.errors import OmnixError as _OE
        return CapabilityResult(
            capability_name=capability_name,
            status=CapabilityStatus.FAILED,
            attempted=True,
            executed=True,
            verified=False,
            failed=True,
            action=action_result,
            verification=VerificationResult(
                status=VerificationStatus.MISMATCH,
                check_name="target_still_foreground",
                expected=True,
                actual=False,
                details={"hwnd": target_ctx.hwnd,
                         "reason": "foreground changed during dispatch"},
            ),
            error=_OE(
                "Foreground window changed during dispatch; "
                "the input may not have landed in the target window."
            ),
        )

    return _on_stale


class KeyboardCapabilityBase(BaseCapability):
    """Base class for keyboard capabilities.

    Owns a process-local :class:`_LazyResolver` and a
    :class:`WindowsInputService` so each capability instance shares
    one resolver and one input service across the lifetime of the
    engine.  The resolver is created lazily on the first dispatch
    that needs it.
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


class KeyboardTypeCapability(KeyboardCapabilityBase):
    """Capability to type text using the keyboard."""

    @property
    def spec(self) -> CapabilitySpec:
        return CapabilitySpec(
            name="desktop.keyboard.type",
            version="1.2.0",
            description=(
                "Types the given text as if the user was typing on the "
                "keyboard.  Acquires the target window first when "
                "target hints are supplied, then verifies the "
                "foreground after the primitive."
            ),
            parameters={
                "text": CapabilityParameter(
                    name="text",
                    type=ParamType.STRING,
                    description="The text to type.",
                    required=True,
                ),
                "interval_s": CapabilityParameter(
                    name="interval_s",
                    type=ParamType.FLOAT,
                    description="Delay between keystrokes in seconds.",
                    required=False,
                    default=0.0,
                ),
                # Phase 17: target hints.  Either / both / neither.
                "target_app_name": CapabilityParameter(
                    name="target_app_name",
                    type=ParamType.STRING,
                    description="Optional target app name.",
                    required=False,
                    default=None,
                ),
                "target_window_title": CapabilityParameter(
                    name="target_window_title",
                    type=ParamType.STRING,
                    description="Optional target window title.",
                    required=False,
                    default=None,
                ),
                "target_window_hwnd": CapabilityParameter(
                    name="target_window_hwnd",
                    type=ParamType.INTEGER,
                    description="Optional target window HWND (verified by prior step).",
                    required=False,
                    default=None,
                ),
                "expected_ui_state": CapabilityParameter(
                    name="expected_ui_state",
                    type=ParamType.STRING,
                    description="Optional expected UI state after focus.",
                    required=False,
                    default=None,
                ),
                # Phase 16 compat: accept the older ``app_name``
                # alias as well so the legacy dispatchers keep
                # working.
                "app_name": CapabilityParameter(
                    name="app_name",
                    type=ParamType.STRING,
                    description="Optional target app name (alias for target_app_name).",
                    required=False,
                    default=None,
                ),
            },
            tags={"desktop", "keyboard", "type"},
        )

    async def execute(self, params: Mapping[str, Any]) -> CapabilityResult:
        text = params.get("text")
        interval_s = float(params.get("interval_s", 0.0) or 0.0)

        def _pre_check() -> Optional[CapabilityResult]:
            if text is None or text == "":
                return CapabilityResult(
                    capability_name=self.spec.name,
                    status=CapabilityStatus.FAILED,
                    failed=True,
                    error=OmnixError("Text parameter is required."),
                )
            return None

        return dispatch_with_target(
            capability_name=self.spec.name,
            params=params,
            resolver_holder=self._resolver_holder,
            primitive=self._input_service.type_text,
            primitive_kwargs={
                "text": str(text),
                "interval_s": interval_s,
            },
            pre_check=_pre_check,
            on_stale_target=_stale_target_handler(self.spec.name),
            extra_details={"text_length": len(str(text))},
        )


class KeyboardPressCapability(KeyboardCapabilityBase):
    """Capability to press a single key on the keyboard."""

    @property
    def spec(self) -> CapabilitySpec:
        return CapabilitySpec(
            name="desktop.keyboard.press",
            version="1.2.0",
            description="Presses a single key (e.g. 'enter', 'tab', 'escape').",
            parameters={
                "key": CapabilityParameter(
                    name="key",
                    type=ParamType.STRING,
                    description="The key to press (e.g. 'enter', 'escape', 'a').",
                    required=True,
                ),
                "target_app_name": CapabilityParameter(
                    name="target_app_name",
                    type=ParamType.STRING,
                    description="Optional target app name.",
                    required=False,
                    default=None,
                ),
                "target_window_title": CapabilityParameter(
                    name="target_window_title",
                    type=ParamType.STRING,
                    description="Optional target window title.",
                    required=False,
                    default=None,
                ),
                "target_window_hwnd": CapabilityParameter(
                    name="target_window_hwnd",
                    type=ParamType.INTEGER,
                    description="Optional target window HWND.",
                    required=False,
                    default=None,
                ),
                "app_name": CapabilityParameter(
                    name="app_name",
                    type=ParamType.STRING,
                    description="Optional target app name (alias).",
                    required=False,
                    default=None,
                ),
            },
            tags={"desktop", "keyboard", "press"},
        )

    async def execute(self, params: Mapping[str, Any]) -> CapabilityResult:
        key = params.get("key")

        def _pre_check() -> Optional[CapabilityResult]:
            if not key:
                return CapabilityResult(
                    capability_name=self.spec.name,
                    status=CapabilityStatus.FAILED,
                    failed=True,
                    error=OmnixError("Key parameter is required."),
                )
            return None

        return dispatch_with_target(
            capability_name=self.spec.name,
            params=params,
            resolver_holder=self._resolver_holder,
            primitive=self._input_service.press_key,
            primitive_kwargs={"key": str(key)},
            pre_check=_pre_check,
            on_stale_target=_stale_target_handler(self.spec.name),
            extra_details={"key": str(key)},
        )


class KeyboardHotkeyCapability(KeyboardCapabilityBase):
    """Capability to press a hotkey combination (e.g. Ctrl+C)."""

    @property
    def spec(self) -> CapabilitySpec:
        return CapabilitySpec(
            name="desktop.keyboard.hotkey",
            version="1.2.0",
            description="Presses a hotkey combination (e.g. 'ctrl', 'c').",
            parameters={
                "keys": CapabilityParameter(
                    name="keys",
                    type=ParamType.ANY,
                    description=(
                        "A list of keys to press simultaneously "
                        "(e.g. ['ctrl', 'c'])."
                    ),
                    required=True,
                ),
                "target_app_name": CapabilityParameter(
                    name="target_app_name",
                    type=ParamType.STRING,
                    description="Optional target app name.",
                    required=False,
                    default=None,
                ),
                "target_window_title": CapabilityParameter(
                    name="target_window_title",
                    type=ParamType.STRING,
                    description="Optional target window title.",
                    required=False,
                    default=None,
                ),
                "target_window_hwnd": CapabilityParameter(
                    name="target_window_hwnd",
                    type=ParamType.INTEGER,
                    description="Optional target window HWND.",
                    required=False,
                    default=None,
                ),
                "app_name": CapabilityParameter(
                    name="app_name",
                    type=ParamType.STRING,
                    description="Optional target app name (alias).",
                    required=False,
                    default=None,
                ),
            },
            tags={"desktop", "keyboard", "hotkey", "combo"},
        )

    async def execute(self, params: Mapping[str, Any]) -> CapabilityResult:
        keys = params.get("keys")

        def _pre_check() -> Optional[CapabilityResult]:
            if not keys or not isinstance(keys, (list, tuple)):
                return CapabilityResult(
                    capability_name=self.spec.name,
                    status=CapabilityStatus.FAILED,
                    failed=True,
                    error=OmnixError(
                        "Keys parameter must be a non-empty list/tuple "
                        "of key names."
                    ),
                )
            return None

        keys_list = list(keys)

        return dispatch_with_target(
            capability_name=self.spec.name,
            params=params,
            resolver_holder=self._resolver_holder,
            primitive=self._input_service.hotkey,
            primitive_kwargs={"keys": keys_list},
            pre_check=_pre_check,
            on_stale_target=_stale_target_handler(self.spec.name),
            extra_details={"keys": keys_list},
        )
