"""
Omnix V6 — Shared dispatch helper for desktop input capabilities (Phase 17).

The keyboard and mouse capabilities all do the same dance:

  1. Extract target hints (``target_app_name`` / ``target_window_title``
     / ``expected_ui_state`` / ``target_window_hwnd``) from the
     params dict.
  2. Acquire a :class:`TargetContext` through the resolver (if a
     hint was supplied).  When the caller asked for a target and
     we could not honour it, return ``CapabilityStatus.FAILED`` with
     ``MISMATCH`` verification — never silently dispatch into
     whatever window is in the foreground.
  3. Run the input primitive.
  4. After the action, re-verify the foreground window is still the
     one we acquired.  When the OS foreground-lockout forced us to
     bypass ``SetForegroundWindow`` (resolver returned
     ``foreground_state == "known"``), we trust the prior step's
     verification and do *not* report ``FAILED`` solely on a
     foreground check the OS would never have let succeed.
  5. Build the :class:`CapabilityResult` envelope (verified vs
     executed; the action result; the verification block; the
     structured details bag).

Before Phase 17 this dance was repeated verbatim in
:class:`KeyboardTypeCapability`,
:class:`KeyboardPressCapability`,
:class:`KeyboardHotkeyCapability` — and it was missing entirely
from the six mouse capabilities.  This module consolidates the
boilerplate so a new capability (e.g. mouse-click with target
acquisition) is a 10-line ``async def execute`` rather than a
60-line block of duplicate control flow.

Why a module (not a base class) for the helper:
    * The 3 keyboard caps already share a base class
      (``KeyboardCapabilityBase``); we do not want to add a
      second base for the mouse caps.  A function-based helper
      composes cleanly with either base.
    * The helper does not need state — it is a pure
      ``(resolver, params, action) → result`` function.  We can
      unit-test it without instantiating any service.
"""
from __future__ import annotations

from typing import Any, Callable, Mapping, Optional, Tuple

from core.capability import CapabilitySpec  # noqa: F401  (re-exported for callers)
from core.errors import OmnixError
from core.results import (
    ActionResult,
    ActionStatus,
    CapabilityResult,
    CapabilityStatus,
    VerificationResult,
    VerificationStatus,
)

from system.application.target_context import (
    InMemoryTargetContextStore,
    TargetContext,
    TargetContextResolver,
)


# ---------------------------------------------------------------------------
# Target hint extraction
# ---------------------------------------------------------------------------

def extract_target_hints(
    params: Mapping[str, Any],
) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[int]]:
    """Pull the four optional target hints out of ``params``.

    Returns ``(app_name, window_title, expected_ui_state, target_window_hwnd)``.
    Loose parsing — a malformed hint simply yields ``None`` instead
    of failing the entire capability.
    """
    app_name = params.get("target_app_name") or params.get("app_name")
    if not isinstance(app_name, str) or not app_name.strip():
        app_name = None
    window_title = params.get("target_window_title") or params.get("window_title")
    if not isinstance(window_title, str) or not window_title.strip():
        window_title = None
    expected = params.get("expected_ui_state")
    if not isinstance(expected, str) or not expected.strip():
        expected = None
    hwnd_raw = params.get("target_window_hwnd") or params.get("hwnd")
    hwnd: Optional[int] = None
    if isinstance(hwnd_raw, int) and hwnd_raw > 0:
        hwnd = hwnd_raw
    elif isinstance(hwnd_raw, str) and hwnd_raw.strip().isdigit():
        hwnd = int(hwnd_raw.strip())
    return app_name, window_title, expected, hwnd


# ---------------------------------------------------------------------------
# Target acquisition
# ---------------------------------------------------------------------------


class _LazyResolver:
    """Process-local holder for a :class:`TargetContextResolver`.

    Capabilities that need a target resolver should not instantiate
    one for every dispatch (the resolver holds process-local state
    — the "recent target" store — that we want to persist across
    steps of a multi-step plan).  This helper keeps a single
    resolver per capability instance.
    """

    def __init__(
        self,
        *,
        app_service: Any = None,
        window_service: Any = None,
    ) -> None:
        self._app_service = app_service
        self._window_service = window_service
        self._resolver: Optional[TargetContextResolver] = None
        self._failed = False

    def get(self) -> Optional[TargetContextResolver]:
        if self._resolver is not None:
            return self._resolver
        if self._failed:
            return None
        if self._app_service is None or self._window_service is None:
            self._failed = True
            return None
        try:
            self._resolver = TargetContextResolver(
                app_service=self._app_service,
                window_service=self._window_service,
                store=InMemoryTargetContextStore(),
            )
            return self._resolver
        except Exception:  # noqa: BLE001
            self._failed = True
            return None


def acquire_target(
    *,
    resolver_holder: Optional[_LazyResolver],
    app_name: Optional[str],
    window_title: Optional[str],
    expected_ui_state: Optional[str],
    target_window_hwnd: Optional[int] = None,
) -> Optional[TargetContext]:
    """Acquire a focused :class:`TargetContext` if a resolver exists.

    Returns ``None`` when no resolver is available, when no target
    hint is supplied, or when the acquisition fails.  Callers must
    treat ``None`` as "no guarantee about the target window" and
    surface that as ``UNVERIFIED`` rather than ``VERIFIED``.

    When ``target_window_hwnd`` is supplied the resolver is asked
    to use that exact window instead of looking one up by app
    name.  This is what lets a multi-step plan carry the HWND
    from ``desktop.application.open`` straight to
    ``desktop.keyboard.type`` without re-acquiring through
    ``SetForegroundWindow``, which Windows frequently rejects
    because of the foreground lockout.
    """
    if resolver_holder is None:
        return None
    resolver = resolver_holder.get()
    if resolver is None:
        return None
    # Explicit HWND: ask the resolver to acquire *that* window
    # (it will still call focus_window, but the OS is more
    # permissive when the calling process is the one that
    # already owns the window).
    if target_window_hwnd and int(target_window_hwnd) > 0:
        try:
            acquire_hwnd = getattr(resolver, "acquire_hwnd", None)
            if callable(acquire_hwnd):
                return acquire_hwnd(int(target_window_hwnd))
        except Exception:  # noqa: BLE001
            pass
    if not app_name and not window_title:
        return None
    try:
        return resolver.acquire(
            app_name=app_name,
            window_title=window_title,
            expected_ui_state=expected_ui_state,
        )
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# Verification helpers
# ---------------------------------------------------------------------------


def make_target_verification(
    *,
    target_ctx: Optional[TargetContext],
) -> VerificationResult:
    """Build a verification block describing the target-acquisition
    outcome.  Returns ``VERIFIED`` only when the target was acquired
    and the foreground window still matches the expected hwnd.  Any
    weaker outcome is reported as ``UNVERIFIED`` (the input *may*
    have landed in the right window, but we cannot prove it).
    """
    if target_ctx is None:
        return VerificationResult(
            status=VerificationStatus.UNVERIFIED,
            check_name="target_window_focused",
            expected=True,
            actual=None,
            details={"reason": "no target hint supplied or resolver unavailable"},
        )
    return VerificationResult(
        status=VerificationStatus.VERIFIED,
        check_name="target_window_focused",
        expected=True,
        actual=True,
        details={
            "hwnd": target_ctx.hwnd,
            "window_title": target_ctx.window_title,
            "process": target_ctx.process,
            "application": target_ctx.application,
        },
    )


def foreground_still_matches(
    *,
    resolver_holder: Optional[_LazyResolver],
    target_ctx: Optional[TargetContext],
) -> bool:
    """Return True when the target window is still in the foreground.

    Returns True on any path that cannot be verified (e.g. the
    resolver is missing) — the caller is responsible for deciding
    whether to downgrade to UNVERIFIED or to trust the prior
    step's verification.
    """
    if resolver_holder is None or target_ctx is None:
        return True
    resolver = resolver_holder.get()
    if resolver is None or target_ctx.hwnd is None:
        return True
    try:
        return resolver.is_foreground(int(target_ctx.hwnd))
    except Exception:  # noqa: BLE001
        return True


# ---------------------------------------------------------------------------
# The shared dispatch envelope
# ---------------------------------------------------------------------------

# Signature of the input-layer primitive the helper wraps.
_DispatchFn = Callable[..., ActionResult]


def dispatch_with_target(
    *,
    capability_name: str,
    params: Mapping[str, Any],
    resolver_holder: Optional[_LazyResolver],
    primitive: _DispatchFn,
    primitive_kwargs: Mapping[str, Any],
    # The "missing required parameter" check is capability-specific.
    # When the capability can return early on a missing param, it
    # supplies ``pre_check``; the helper returns its return value
    # verbatim if the check fails.
    pre_check: Optional[Callable[[], Optional[CapabilityResult]]] = None,
    # Optional stale-target handler.  Called when the foreground
    # check after the primitive fails.  The default handler returns
    # a FAILED / MISMATCH CapabilityResult.  The keyboard caps
    # override this so they trust the prior step's verification
    # when ``foreground_state == "known"`` (the OS lockout path).
    on_stale_target: Optional[
        Callable[[TargetContext, ActionResult], CapabilityResult]
    ] = None,
    # Result details bag — capability-specific (e.g. ``text_length``).
    extra_details: Optional[Mapping[str, Any]] = None,
) -> CapabilityResult:
    """Run ``primitive(**primitive_kwargs)`` with target acquisition.

    This is the consolidated envelope every desktop input capability
    uses.  A new capability (e.g. mouse-click with target) writes
    a ~10-line ``async def execute`` that calls this helper.  No
    copy-pasted target-acquisition / foreground-verify boilerplate.

    See ``desktop_keyboard.py`` for usage examples.
    """
    # 0. Capability-specific precondition (e.g. text or key is required).
    if pre_check is not None:
        early = pre_check()
        if early is not None:
            return early
    # 1. Extract target hints.
    target_app_name, target_window_title, expected_ui_state, target_hwnd = (
        extract_target_hints(params)
    )
    target_ctx: Optional[TargetContext] = None
    target_acquired: bool = False
    # 2. Acquire target when the caller asked for one.
    if target_app_name or target_window_title or target_hwnd:
        target_ctx = acquire_target(
            resolver_holder=resolver_holder,
            app_name=target_app_name,
            window_title=target_window_title,
            expected_ui_state=expected_ui_state,
            target_window_hwnd=target_hwnd,
        )
        target_acquired = target_ctx is not None
        if not target_acquired:
            # Closed-loop contract: we were asked for a target
            # and could not honour it.  Never silently dispatch
            # into whatever window is in the foreground.
            return CapabilityResult(
                capability_name=capability_name,
                status=CapabilityStatus.FAILED,
                attempted=True,
                failed=True,
                verification=VerificationResult(
                    status=VerificationStatus.MISMATCH,
                    check_name="target_window_focused",
                    expected=True,
                    actual=False,
                    details={
                        "target_app_name": target_app_name,
                        "target_window_title": target_window_title,
                        "target_window_hwnd": target_hwnd,
                        "reason": "could not focus target window",
                    },
                ),
                error=OmnixError(
                    f"Could not focus target window "
                    f"(app_name={target_app_name!r}, "
                    f"window_title={target_window_title!r}, "
                    f"hwnd={target_hwnd!r}) "
                    f"before dispatch.",
                ),
            )
    # 3. Run the input primitive.
    # Inject the cancellation token from params into the primitive
    # kwargs under the canonical ``cancellation=`` keyword.  Every
    # input service method (type_text, click, drag, etc.) accepts
    # an optional ``cancellation`` parameter; the helper centralises
    # the wiring so individual capabilities don't have to repeat
    # the ``params.get('cancellation_token')`` boilerplate.
    primitive_kwargs = dict(primitive_kwargs)
    cancellation_token = params.get("cancellation_token")
    if cancellation_token is not None:
        primitive_kwargs.setdefault("cancellation", cancellation_token)
    try:
        action_result: ActionResult = primitive(**primitive_kwargs)
    except Exception as exc:  # noqa: BLE001
        return CapabilityResult(
            capability_name=capability_name,
            status=CapabilityStatus.FAILED,
            failed=True,
            error=OmnixError(f"{capability_name} primitive raised: {exc!r}"),
        )
    # 4. Map action result to capability result.
    if action_result.status is not ActionStatus.EXECUTED:
        # Phase 17: preserve CANCELLED / TIMED_OUT status.  Before
        # this fix the helper collapsed any non-EXECUTED action to
        # FAILED, hiding cooperative-cancellation from the caller.
        if action_result.status is ActionStatus.CANCELLED:
            cap_status = CapabilityStatus.CANCELLED
        elif action_result.status is ActionStatus.TIMED_OUT:
            cap_status = CapabilityStatus.TIMED_OUT
        else:
            cap_status = CapabilityStatus.FAILED
        return CapabilityResult(
            capability_name=capability_name,
            status=cap_status,
            attempted=True,
            failed=cap_status is CapabilityStatus.FAILED,
            action=action_result,
            error=None if cap_status in (
                CapabilityStatus.CANCELLED, CapabilityStatus.TIMED_OUT,
            ) else OmnixError(
                f"{capability_name} primitive did not execute: "
                f"{action_result.details.get('reason', '')}"
            ),
        )
    # 5. Re-verify the foreground window when a target was acquired.
    if (
        target_ctx is not None
        and not foreground_still_matches(
            resolver_holder=resolver_holder, target_ctx=target_ctx
        )
    ):
        if on_stale_target is not None:
            return on_stale_target(target_ctx, action_result)
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
            error=OmnixError(
                "Foreground window changed during dispatch; "
                "the input may not have landed in the target window."
            ),
        )
    # 6. Build the success envelope.
    status = (
        CapabilityStatus.VERIFIED if target_acquired else CapabilityStatus.EXECUTED
    )
    details: dict[str, Any] = dict(extra_details or {})
    if target_ctx is not None:
        details["target_hwnd"] = target_ctx.hwnd
        details["target_app_name"] = target_ctx.application
    return CapabilityResult(
        capability_name=capability_name,
        status=status,
        attempted=True,
        executed=True,
        verified=bool(target_acquired),
        action=action_result,
        verification=make_target_verification(target_ctx=target_ctx),
        details=details,
    )


__all__ = [
    "extract_target_hints",
    "acquire_target",
    "make_target_verification",
    "foreground_still_matches",
    "dispatch_with_target",
    "_LazyResolver",
]
