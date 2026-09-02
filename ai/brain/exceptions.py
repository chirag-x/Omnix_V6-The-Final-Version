"""
Omnix V6 — Brain / Planner exceptions (Phase 5C+5D).

The Brain and Planner never raise bare ``Exception`` (R-7).  All
failures the orchestration layer could trigger are expressed as
typed errors that carry a stable ``code`` for routing and a
``context`` bag for inspection.

The hierarchy is intentionally separate from :class:`core.errors.OmnixError`
so the Brain package does not pull in engine internals.  Callers that
need a unified error type can wrap a :class:`BrainError` in an
``OmnixError`` at the engine boundary.
"""

from __future__ import annotations

from typing import Any, Dict, Optional


class BrainError(Exception):
    """Root of the Brain / Planner error hierarchy.

    Every error carries a stable, machine-readable ``code`` so callers
    (orchestrator, CLI) can branch on it.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str = "BRAIN_ERROR",
        cause: Optional[BaseException] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.message: str = message
        self.code: str = code
        self.cause: Optional[BaseException] = cause
        self.context: Dict[str, Any] = dict(context) if context else {}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": type(self).__name__,
            "code": self.code,
            "message": self.message,
            "cause": repr(self.cause) if self.cause else None,
            "context": dict(self.context),
        }

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        ctx = f", context={self.context!r}" if self.context else ""
        cause = f", cause={self.cause!r}" if self.cause else ""
        return f"{type(self).__name__}(code={self.code!r}, message={self.message!r}{cause}{ctx})"


# ---------------------------------------------------------------------------
# Provider errors
# ---------------------------------------------------------------------------

class ProviderFailure(BrainError):
    """The LLM provider returned a structured failure (auth, timeout, ...).

    The Planner never raises a raw provider exception (e.g. an
    ``openai.error.RateLimitError``).  The provider is responsible for
    the boundary conversion; the Planner surfaces the structured
    failure as a :class:`ProviderFailure`.
    """

    def __init__(
        self,
        message: str = "LLM provider call failed",
        *,
        cause: Optional[BaseException] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message, code="BRAIN_PROVIDER_FAILURE", cause=cause, context=context)


class ProviderTimeout(BrainError):
    """The provider did not respond within the deadline."""

    def __init__(
        self,
        message: str = "LLM provider call exceeded its timeout",
        *,
        cause: Optional[BaseException] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message, code="BRAIN_PROVIDER_TIMEOUT", cause=cause, context=context)


class ProviderCancelled(BrainError):
    """The provider call was cancelled before completion."""

    def __init__(
        self,
        message: str = "LLM provider call was cancelled",
        *,
        cause: Optional[BaseException] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message, code="BRAIN_PROVIDER_CANCELLED", cause=cause, context=context)


class ProviderMalformedResponse(BrainError):
    """The provider returned a response that could not be parsed."""

    def __init__(
        self,
        message: str = "LLM provider returned a malformed response",
        *,
        cause: Optional[BaseException] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(
            message,
            code="BRAIN_PROVIDER_MALFORMED",
            cause=cause,
            context=context,
        )


# ---------------------------------------------------------------------------
# Plan / validation errors
# ---------------------------------------------------------------------------

class MalformedPlanPayload(BrainError):
    """The planner output (LLM or otherwise) was not a valid plan dict."""

    def __init__(
        self,
        message: str = "Plan payload is malformed",
        *,
        cause: Optional[BaseException] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(
            message,
            code="BRAIN_PLAN_MALFORMED",
            cause=cause,
            context=context,
        )


class UnknownCapabilityError(BrainError):
    """The plan references a capability that is not in the registry."""

    def __init__(
        self,
        message: str = "Plan references an unknown capability",
        *,
        cause: Optional[BaseException] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(
            message,
            code="BRAIN_UNKNOWN_CAPABILITY",
            cause=cause,
            context=context,
        )


class InvalidArgumentError(BrainError):
    """A plan step has an invalid argument (missing, wrong type, ...)."""

    def __init__(
        self,
        message: str = "Plan step has an invalid argument",
        *,
        cause: Optional[BaseException] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(
            message,
            code="BRAIN_INVALID_ARGUMENT",
            cause=cause,
            context=context,
        )


class PlanSizeExceeded(BrainError):
    """The plan is larger than the configured size bound."""

    def __init__(
        self,
        message: str = "Plan exceeds the configured size limit",
        *,
        cause: Optional[BaseException] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(
            message,
            code="BRAIN_PLAN_SIZE_EXCEEDED",
            cause=cause,
            context=context,
        )


class InvalidDependencyError(BrainError):
    """A plan step has a self-dependency, a cycle, or a missing dependency."""

    def __init__(
        self,
        message: str = "Plan has an invalid dependency",
        *,
        cause: Optional[BaseException] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(
            message,
            code="BRAIN_INVALID_DEPENDENCY",
            cause=cause,
            context=context,
        )


class InvalidTimeoutError(BrainError):
    """A plan step has an invalid (negative, infinite, ...) timeout."""

    def __init__(
        self,
        message: str = "Plan step has an invalid timeout",
        *,
        cause: Optional[BaseException] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(
            message,
            code="BRAIN_INVALID_TIMEOUT",
            cause=cause,
            context=context,
        )


class InvalidExpectedEffectError(BrainError):
    """An ExpectedEffect is structurally invalid (no check name, etc.)."""

    def __init__(
        self,
        message: str = "Plan step has an invalid expected effect",
        *,
        cause: Optional[BaseException] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(
            message,
            code="BRAIN_INVALID_EFFECT",
            cause=cause,
            context=context,
        )


class SafetyClassificationError(BrainError):
    """A plan step claims a safety classification that the capability does
    not have, or attempts to downgrade a dangerous operation."""

    def __init__(
        self,
        message: str = "Plan step has an invalid safety classification",
        *,
        cause: Optional[BaseException] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(
            message,
            code="BRAIN_SAFETY_VIOLATION",
            cause=cause,
            context=context,
        )


class ClarificationRequired(BrainError):
    """The intent was a CLARIFY / UNKNOWN; the Brain cannot plan from it.

    This is not a hard failure: callers (e.g. the orchestrator) are
    expected to surface the question to the user.
    """

    def __init__(
        self,
        message: str = "Clarification required before planning",
        *,
        cause: Optional[BaseException] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(
            message,
            code="BRAIN_CLARIFICATION_REQUIRED",
            cause=cause,
            context=context,
        )


class CannotPlanError(BrainError):
    """The Brain could not produce a plan for a reason other than provider
    failure.  The most common cause is "no capability exists to do
    this"."""

    def __init__(
        self,
        message: str = "Cannot plan for this goal",
        *,
        cause: Optional[BaseException] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(
            message,
            code="BRAIN_CANNOT_PLAN",
            cause=cause,
            context=context,
        )


class CancelledError(BrainError):
    """The Brain / Planner call was cancelled."""

    def __init__(
        self,
        message: str = "Brain / Planner call was cancelled",
        *,
        cause: Optional[BaseException] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(
            message,
            code="BRAIN_CANCELLED",
            cause=cause,
            context=context,
        )


class InvalidVisionMetadataError(BrainError):
    """A plan step declared vision grounding metadata that is malformed,
    uses an unknown ``pre_action`` kind, prefers an unknown strategy,
    asks to skip grounding on a step that requires it, or otherwise
    violates the Planner → Vision contract (Phase 7.3).
    """

    def __init__(
        self,
        message: str = "Plan step has invalid vision grounding metadata",
        *,
        cause: Optional[BaseException] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(
            message,
            code="BRAIN_INVALID_VISION_METADATA",
            cause=cause,
            context=context,
        )


class InvalidBrowserMetadataError(BrainError):
    """A plan step declared browser grounding metadata that is malformed,
    uses an unknown locator kind, an unknown target strategy, an
    invalid session id, or otherwise violates the Planner → Browser
    contract (Phase 8).

    The browser contract is the closed-set counterpart to the vision
    contract: target-bearing browser steps (``browser.click``,
    ``browser.type``, ``browser.extract_text``) must declare how the
    target will be located — DOM-first, accessibility, explicit
    locator, or — only as a last resort — vision fallback.  The
    planner is forbidden from inventing a strategy outside the closed
    set or from asking the executor to run arbitrary JavaScript.
    """

    def __init__(
        self,
        message: str = "Plan step has invalid browser grounding metadata",
        *,
        cause: Optional[BaseException] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(
            message,
            code="BRAIN_INVALID_BROWSER_METADATA",
            cause=cause,
            context=context,
        )
