"""
Omnix V6 — LLM provider seam (Phase 5A).

The future Omnix Brain calls into an :class:`LLMProvider`.  This
subpackage defines:

    * :class:`LLMRequest` / :class:`LLMResponse` / :class:`LLMUsage` /
      :class:`LLMMessage` — typed request/response contracts.
    * :class:`LLMProvider` — the protocol every concrete provider
      implements.
    * :class:`ProviderError` and its concrete subclasses — structured
      failures (auth, timeout, rate limit, etc.).
    * :class:`MockProvider` — a deterministic fake for tests and
      development.
    * :func:`get_provider` — provider selection from configuration.

Mandatory isolation rule (R-21 of the LLM layer): **the provider
subpackage MUST NOT import or use any of**:

    * :mod:`subprocess`
    * :mod:`pyautogui`
    * :mod:`win32gui` / :mod:`win32api`
    * :mod:`ctypes`
    * :mod:`core.capability_router`
    * any V6 *Windows service* (e.g. ``system.windows.*``,
      ``system.applications.*``)

The provider produces *data*; it does not execute actions.  Tests in
:mod:`tests.test_provider_isolation` enforce this.
"""

from .base import LLMProvider
from .contracts import (
    FinishReason,
    LLMMessage,
    LLMRequest,
    LLMResponse,
    LLMUsage,
    MessageRole,
    OutputFormat,
)
from .errors import (
    AuthenticationError,
    CancelledError,
    ConfigurationError_,
    InvalidRequestError,
    MalformedResponseError,
    ModelUnavailableError,
    ProviderError,
    RateLimitError,
    TimeoutError_,
    UnavailableError,
)
from .mock import MockProvider
from .openrouter import OpenRouterProvider
from .selection import get_provider, register_provider

__all__ = [
    "LLMProvider",
    "LLMRequest",
    "LLMResponse",
    "LLMMessage",
    "LLMUsage",
    "MessageRole",
    "FinishReason",
    "OutputFormat",
    "ProviderError",
    "AuthenticationError",
    "TimeoutError_",
    "RateLimitError",
    "UnavailableError",
    "InvalidRequestError",
    "MalformedResponseError",
    "ModelUnavailableError",
    "CancelledError",
    "ConfigurationError_",
    "MockProvider",
    "OpenRouterProvider",
    "get_provider",
    "register_provider",
]
