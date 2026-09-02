"""
Omnix V6 — Structured provider errors (Phase 5A).

Every provider failure surfaces as a :class:`ProviderError` subclass.
Raw provider-specific exceptions (e.g. ``openai.error.RateLimitError``)
MUST be caught at the provider boundary and wrapped, so the Brain
never has to know which provider raised what.

Design constraints:

    * Every error carries a stable, machine-readable ``code``
      (R-7/AD-7).
    * Secrets (API keys, tokens) MUST NOT appear in :attr:`message`
      or :attr:`context`.  Providers must redact before constructing
      the error.
    * The :class:`ProviderError` hierarchy is intentionally separate
      from :class:`core.errors.OmnixError` so the provider layer does
      not pull in engine internals.
"""

from __future__ import annotations

from typing import Any, Dict, Optional


class ProviderError(Exception):
    """Root of the provider error hierarchy."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "PROVIDER_ERROR",
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
# Concrete provider errors
# ---------------------------------------------------------------------------

class AuthenticationError(ProviderError):
    """The provider rejected the credentials."""

    def __init__(
        self,
        message: str = "Provider authentication failed",
        *,
        cause: Optional[BaseException] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message, code="PROVIDER_AUTH_FAILED", cause=cause, context=context)


class TimeoutError_(ProviderError):  # noqa: A001 - intentional
    """The provider did not respond within the deadline.

    Named with a trailing underscore so the name is still importable
    without shadowing the Python builtin in callers' namespaces.
    """

    def __init__(
        self,
        message: str = "Provider call exceeded its timeout",
        *,
        cause: Optional[BaseException] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message, code="PROVIDER_TIMEOUT", cause=cause, context=context)


class RateLimitError(ProviderError):
    """The provider returned a rate-limit signal."""

    def __init__(
        self,
        message: str = "Provider rate limit exceeded",
        *,
        cause: Optional[BaseException] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message, code="PROVIDER_RATE_LIMITED", cause=cause, context=context)


class UnavailableError(ProviderError):
    """The provider is unreachable or refusing connections."""

    def __init__(
        self,
        message: str = "Provider is currently unavailable",
        *,
        cause: Optional[BaseException] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message, code="PROVIDER_UNAVAILABLE", cause=cause, context=context)


class InvalidRequestError(ProviderError):
    """The request did not pass the provider's input validation."""

    def __init__(
        self,
        message: str = "Provider rejected the request as invalid",
        *,
        cause: Optional[BaseException] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message, code="PROVIDER_INVALID_REQUEST", cause=cause, context=context)


class MalformedResponseError(ProviderError):
    """The provider returned a response that could not be parsed."""

    def __init__(
        self,
        message: str = "Provider returned a malformed response",
        *,
        cause: Optional[BaseException] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message, code="PROVIDER_MALFORMED_RESPONSE", cause=cause, context=context)


class ModelUnavailableError(ProviderError):
    """The requested model is not available on this provider."""

    def __init__(
        self,
        message: str = "Requested model is not available on this provider",
        *,
        cause: Optional[BaseException] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message, code="PROVIDER_MODEL_UNAVAILABLE", cause=cause, context=context)


class CancelledError(ProviderError):
    """The call was cancelled before the provider completed it."""

    def __init__(
        self,
        message: str = "Provider call was cancelled",
        *,
        cause: Optional[BaseException] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message, code="PROVIDER_CANCELLED", cause=cause, context=context)


class ConfigurationError_(ProviderError):  # noqa: A001
    """The provider was misconfigured (missing key, bad model name, ...)."""

    def __init__(
        self,
        message: str = "Provider is misconfigured",
        *,
        cause: Optional[BaseException] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message, code="PROVIDER_CONFIG_INVALID", cause=cause, context=context)
