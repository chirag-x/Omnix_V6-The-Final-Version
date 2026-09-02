"""
Omnix V6 — Error Model.

Defines the single, typed error hierarchy used by every subsystem.

Design constraints (V6 architecture):
    - R-7 / AD-7: All errors must be recoverable & inspectable.  No bare
      `Exception`, no `pass`-on-error, no silently swallowed tracebacks.
    - R-8 / AD-8: An action that "succeeded with a partial state" is still
      a failure.  Errors must carry the actual state the world was in
      when the failure was detected.
    - R-10 / AD-10: Errors are first-class data — code, message, cause,
      context — and they propagate through structured result objects
      (see ``core.results``) rather than being lost in logs.
    - R-17 / AD-17: ``loguru`` is the only logger.  No ``logging`` import
      in the engine core.

Hierarchy:

    OmnixError                    (root)
    ├── ConfigurationError        — bad config / missing .env / schema violation
    ├── DependencyError           — a required subsystem/service is unavailable
    ├── CapabilityError           — capability is unknown, disabled, or unsafe
    ├── ValidationError           — input or pre-condition failed validation
    ├── ExecutionError            — the action itself failed (real failure)
    ├── TimeoutError              — operation exceeded its deadline
    ├── ObservationError          — sensing the world failed (vision, OCR, etc.)
    ├── VerificationError         — post-action check failed
    └── RecoveryError             — recovery attempt itself failed
"""

from __future__ import annotations

from typing import Any, Dict, Optional


class OmnixError(Exception):
    """Root of the Omnix error hierarchy.

    Every Omnix-raised error must subclass this.  Catching ``Exception``
    in the engine is forbidden (R-7): always catch ``OmnixError``.

    Attributes
    ----------
    code:
        Stable, machine-readable identifier (e.g. ``"CAPABILITY_UNKNOWN"``).
        Logging/alerting routes on this string, never on the human message.
    message:
        Human-readable description.  Suitable for surfacing to the user
        via TTS / console, but not for routing.
    cause:
        The original exception, if this error wraps a lower-level failure.
    context:
        Free-form key/value bag carrying the world-state snapshot the
        caller needs to understand the failure (e.g. ``{"window": "Notepad"}``).
    """

    def __init__(
        self,
        message: str,
        *,
        code: str = "OMNIX_ERROR",
        cause: Optional[BaseException] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.message: str = message
        self.code: str = code
        self.cause: Optional[BaseException] = cause
        self.context: Dict[str, Any] = dict(context) if context else {}

    # ------------------------------------------------------------------ repr
    def __repr__(self) -> str:  # pragma: no cover - debug aid
        ctx = f", context={self.context!r}" if self.context else ""
        cause = f", cause={self.cause!r}" if self.cause else ""
        return f"{type(self).__name__}(code={self.code!r}, message={self.message!r}{cause}{ctx})"

    # -------------------------------------------------------------- to_dict
    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a JSON-safe dict for structured logging / result objects."""
        return {
            "type": type(self).__name__,
            "code": self.code,
            "message": self.message,
            "cause": repr(self.cause) if self.cause else None,
            "context": dict(self.context),
        }


# ---------------------------------------------------------------------------
# Concrete error types
# ---------------------------------------------------------------------------

class ConfigurationError(OmnixError):
    """A configuration value is missing, malformed, or inconsistent."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "CONFIGURATION_ERROR",
        cause: Optional[BaseException] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message, code=code, cause=cause, context=context)


class DependencyError(OmnixError):
    """A required subsystem / service / capability is not available.

    The engine uses this when a subsystem's ``initialize()`` returned
    ``False`` or the registry could not resolve a required name.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str = "DEPENDENCY_ERROR",
        cause: Optional[BaseException] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message, code=code, cause=cause, context=context)


class CapabilityError(OmnixError):
    """A capability is unknown, disabled, or refused for safety reasons.

    Distinct from ``ExecutionError``: the capability was never invoked
    because the router rejected the request.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str = "CAPABILITY_ERROR",
        cause: Optional[BaseException] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message, code=code, cause=cause, context=context)


class ValidationError(OmnixError):
    """An input or pre-condition failed validation.

    Used by the capability router to reject malformed parameters before
    any side effect occurs.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str = "VALIDATION_ERROR",
        cause: Optional[BaseException] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message, code=code, cause=cause, context=context)


class ExecutionError(OmnixError):
    """The action itself failed during execution.

    This is the canonical "I tried, and the world did not move as
    expected" error.  Per R-8, ``context`` should carry the world state
    the action was operating on so the recovery layer can inspect it.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str = "EXECUTION_ERROR",
        cause: Optional[BaseException] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message, code=code, cause=cause, context=context)


class TimeoutError(OmnixError):  # noqa: A001 - shadowing builtin is intentional here
    """Operation exceeded its deadline.

    R-9 requires timeouts to be a first-class concept.  Subsystems must
    raise this (not return ``None`` and hope the caller notices).
    """

    def __init__(
        self,
        message: str,
        *,
        code: str = "TIMEOUT_ERROR",
        cause: Optional[BaseException] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message, code=code, cause=cause, context=context)


class ObservationError(OmnixError):
    """Sensing the world failed (vision, OCR, screen capture, etc.)."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "OBSERVATION_ERROR",
        cause: Optional[BaseException] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message, code=code, cause=cause, context=context)


class VerificationError(OmnixError):
    """Post-action verification failed.

    The action may have run; what matters is that the world does not
    reflect the expected post-condition.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str = "VERIFICATION_ERROR",
        cause: Optional[BaseException] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message, code=code, cause=cause, context=context)


class RecoveryError(OmnixError):
    """A recovery attempt itself failed.

    The recovery layer must wrap its own failures in this so the
    engine can distinguish "could not recover" from "recovered fine".
    """

    def __init__(
        self,
        message: str,
        *,
        code: str = "RECOVERY_ERROR",
        cause: Optional[BaseException] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message, code=code, cause=cause, context=context)


# ---------------------------------------------------------------------------
# Input subsystem errors (Phase 17)
# ---------------------------------------------------------------------------
#
# These are *ExecutionError* subclasses with stable codes so callers
# (capability layer, recovery engine, audit log) can branch on the
# category rather than parsing free-form text.
#
# Naming convention: code is ``INPUT_<REASON>``.  The classes are
# also re-exported as ``InputError``, ``FocusError``,
# ``TargetStaleError``, ``CancellationError`` for compatibility with
# code that was written against the peak-upgrade design.


class InputError(ExecutionError):
    """A generic input-action error from the InputService layer.

    Use the more specific subclasses (``FocusError``,
    ``TargetStaleError``, ``CancellationError``) when the failure
    category is known.  Fall back to ``InputError`` for everything
    else (e.g. mouse-button failure, keyboard mapping error).
    """

    def __init__(
        self,
        message: str,
        *,
        code: str = "INPUT_ERROR",
        cause: Optional[BaseException] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message, code=code, cause=cause, context=context)


class FocusError(InputError):
    """The target window could not be brought to the foreground.

    Distinct from ``TargetStaleError`` (the target existed but its
    foreground-state changed *during* the action) — this is raised
    *before* the primitive runs, when the resolver could not focus
    the target window.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str = "FOCUS_ERROR",
        cause: Optional[BaseException] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message, code=code, cause=cause, context=context)


class TargetStaleError(FocusError):
    """The target window's foreground-state changed *during* the action.

    The primitive may have run, but the input may not have landed
    in the target window.  Callers should treat this as a
    verification mismatch (the action executed but the world-state
    no longer matches the expected target).
    """

    def __init__(
        self,
        message: str,
        *,
        code: str = "TARGET_STALE_ERROR",
        cause: Optional[BaseException] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message, code=code, cause=cause, context=context)


class CancellationError(ExecutionError):
    """The input action was cancelled before it could complete.

    Distinct from ``TimeoutError``: a cancellation is *cooperative*
    — the caller asked us to stop, we honoured it within the
    chunk-check window.  The action may have partially run (some
    keystrokes may have been delivered, some clicks may have
    landed).  Callers should treat this as a recoverable terminal
    state, not as a hard failure.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str = "CANCELLATION_ERROR",
        cause: Optional[BaseException] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message, code=code, cause=cause, context=context)
