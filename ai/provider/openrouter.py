"""
Omnix V6 -- OpenRouter LLM provider (Phase 6D).

Implements the LLMProvider protocol for OpenRouter API.

Model precedence (enforced in :meth:`__init__` and :meth:`generate`):

    1. ``request.model`` (per-call override, highest priority)
    2. explicit provider ``model`` (passed in by the factory from
       ``OMNIX_LLM_MODEL`` or ``config.extra["llm_model"]``)
    3. first model in ``model_pool`` (default)

Error mapping (Phase 6D.1):

    * HTTP 401      -> :class:`AuthenticationError`   (NOT retried)
    * HTTP 429      -> :class:`RateLimitError`        (retried)
    * HTTP 5xx      -> :class:`UnavailableError`      (retried)
    * HTTP 4xx oth. -> :class:`InvalidRequestError`   (NOT retried)
    * timeout       -> :class:`TimeoutError_`         (retried)
    * network       -> :class:`UnavailableError`      (retried)
    * bad JSON      -> :class:`MalformedResponseError` (NOT retried)
    * missing ch.   -> :class:`MalformedResponseError` (NOT retried)
    * empty content -> :class:`MalformedResponseError` (NOT retried)

Retries are bounded by ``max_retries`` (clamped to ``[0, 5]``).
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any, Dict, Optional, Tuple

import requests

from .base import LLMProvider
from .contracts import LLMRequest, LLMResponse, LLMUsage, FinishReason
from .errors import (
    ProviderError,
    AuthenticationError,
    ConfigurationError_,
    InvalidRequestError,
    MalformedResponseError,
    RateLimitError,
    TimeoutError_,
    UnavailableError,
    CancelledError,
)

# Cap on retries.  The provider MUST NOT spin on a misconfigured client.
_MAX_RETRIES_CAP = 5

# Truncation length for raw response bodies / error context.  Prevents
# large HTML error pages or echoed provider secrets from being carried
# around in exception context.
_MAX_BODY_CHARS = 500


def _redact(value: str) -> str:
    """Scrub any ``Bearer ...`` token from a free-form string.

    Defensive: a misbehaving proxy could echo the ``Authorization``
    header back in a response body.  The provider never logs the key
    intentionally, but we still strip it on the way into error context
    so a redaction bug elsewhere cannot leak the key.
    """
    if not value:
        return value
    # Match "Bearer " followed by anything up to a whitespace, comma, or
    # end-of-string.  Case-insensitive for safety.
    import re
    return re.sub(r"(?i)bearer\s+[A-Za-z0-9._\-]+", "Bearer ***", value)


def _normalize_base_url(url: str) -> str:
    """Return the canonical OpenRouter base URL.

    The provider always posts to ``<base>/chat/completions``.  Accept
    both ``https://openrouter.ai/api/v1`` and the full endpoint
    ``https://openrouter.ai/api/v1/chat/completions`` so the user's
    environment is forgiving of either form.
    """
    if not url:
        return "https://openrouter.ai/api/v1"
    base = url.rstrip("/")
    suffix = "/chat/completions"
    if base.endswith(suffix):
        base = base[: -len(suffix)]
    return base.rstrip("/")


class OpenRouterProvider:
    """OpenRouter LLM provider.

    The provider is stateless with respect to Omnix: it takes an LLMRequest
    and returns an LLMResponse.  Provider-internal state (rate-limit counters,
    caches, etc.) is the provider's own concern.
    """

    name: str = "openrouter"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://openrouter.ai/api/v1",
        model: Optional[str] = None,
        model_pool: Tuple[str, ...] = (),
        timeout_s: Optional[float] = None,
        max_retries: int = 2,
    ) -> None:
        if not api_key:
            raise ConfigurationError_(
                "OpenRouter API key is required",
                context={"provider": "openrouter"},
            )
        self._api_key = api_key
        self._base_url = _normalize_base_url(base_url)
        # Determine the model to use: if model is explicitly provided, use it;
        # otherwise, if model_pool is not empty, use the first model in the pool.
        # Precedence level 2 (explicit) wins over level 3 (pool).
        if model is None and model_pool:
            model = model_pool[0]
        if not model:
            raise ConfigurationError_(
                "No model specified for OpenRouter request and no model pool configured",
                context={"provider": "openrouter"},
            )
        self._model = model
        self._model_pool = tuple(model_pool)
        self._timeout_s = timeout_s
        # Bound retries: never let a misconfigured caller spin forever.
        self._max_retries = max(0, min(int(max_retries), _MAX_RETRIES_CAP))
        self._lock = threading.RLock()
        self._call_count = 0
        self._error_count = 0
        self._last_request: Optional[LLMRequest] = None
        self._last_response: Optional[LLMResponse] = None
        self._last_model_used: Optional[str] = None

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        # Never include the API key in repr(); R-12 invariant.
        return (
            f"OpenRouterProvider(name={self.name!r}, "
            f"model={self._model!r}, api_key='***')"
        )

    def generate(self, request: LLMRequest) -> LLMResponse:
        """Generate a response from OpenRouter.

        Implements retry logic and error mapping.
        """
        if not isinstance(request, LLMRequest):
            raise InvalidRequestError(
                "OpenRouterProvider expected an LLMRequest",
                context={"got_type": type(request).__name__},
            )
        if not request.messages:
            raise InvalidRequestError(
                "LLMRequest must contain at least one message",
                context={"messages": 0},
            )

        # Precedence level 1: per-call request override wins over the
        # provider's resolved model (level 2/3).
        model = request.model or self._model

        with self._lock:
            self._call_count += 1
            self._last_request = request
            self._last_model_used = model

        # Prepare headers.  The key NEVER appears outside this provider.
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        # Prepare payload
        payload: Dict[str, Any] = {
            "model": model,
            "messages": [m.to_dict() for m in request.messages],
        }
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens
        if request.output_format.value == "json":
            payload["response_format"] = {"type": "json_object"}

        # Timeout: use request.timeout_s, then provider default, then 60s
        timeout = request.timeout_s or self._timeout_s or 60.0

        # Retry loop.  Bounded by self._max_retries (clamped at init).
        last_exception: Optional[Exception] = None
        for attempt in range(self._max_retries + 1):
            try:
                start = time.time()
                response = requests.post(
                    f"{self._base_url}/chat/completions",
                    headers=headers,
                    json=payload,
                    timeout=timeout,
                )
                elapsed = time.time() - start

                # Handle HTTP errors with the canonical provider-error
                # hierarchy.  Order matters: 401 and 429 must be matched
                # before the generic 4xx / 5xx buckets.
                if response.status_code >= 400:
                    last_exception = self._map_http_error(response)
                    # Non-retryable errors short-circuit immediately.
                    if isinstance(
                        last_exception,
                        (AuthenticationError, InvalidRequestError, MalformedResponseError, ConfigurationError_),
                    ):
                        with self._lock:
                            self._error_count += 1
                        raise last_exception
                    # Retryable error: record and back off.
                    if attempt >= self._max_retries:
                        with self._lock:
                            self._error_count += 1
                        raise last_exception
                    self._sleep_with_retry_after(last_exception, attempt)
                    continue

                # Parse successful response
                try:
                    data = response.json()
                except (ValueError, json.JSONDecodeError) as exc:
                    with self._lock:
                        self._error_count += 1
                    raise MalformedResponseError(
                        "OpenRouter returned invalid JSON",
                        cause=exc,
                        context={
                            "status": response.status_code,
                            "content_type": response.headers.get("Content-Type", ""),
                            "response": _redact(response.text[:_MAX_BODY_CHARS]),
                        },
                    ) from None

                if not data.get("choices"):
                    with self._lock:
                        self._error_count += 1
                    raise MalformedResponseError(
                        "OpenRouter response missing choices",
                        context={"response": data},
                    ) from None

                choice = data["choices"][0]
                content = choice.get("message", {}).get("content", "")
                if content is None:
                    content = ""

                finish_reason_str = choice.get("finish_reason", "stop")
                try:
                    finish_reason = FinishReason(finish_reason_str)
                except ValueError:
                    finish_reason = FinishReason.UNKNOWN

                # Empty content with a non-trivial finish reason is
                # often a content-filter or a transport-level bug.
                # Treat it as a malformed response so the Brain can
                # branch on it instead of silently passing "" through.
                if not content and finish_reason not in (
                    FinishReason.LENGTH,
                    FinishReason.CONTENT_FILTER,
                    FinishReason.CANCELLED,
                ):
                    with self._lock:
                        self._error_count += 1
                    raise MalformedResponseError(
                        "OpenRouter returned empty content",
                        context={
                            "finish_reason": finish_reason.value,
                            "model": model,
                        },
                    ) from None

                usage_data = data.get("usage", {}) or {}
                usage = LLMUsage(
                    prompt_tokens=usage_data.get("prompt_tokens"),
                    completion_tokens=usage_data.get("completion_tokens"),
                    total_tokens=usage_data.get("total_tokens"),
                    extra={k: v for k, v in usage_data.items() if k not in {"prompt_tokens", "completion_tokens", "total_tokens"}},
                )

                llm_response = LLMResponse(
                    content=content,
                    finish_reason=finish_reason,
                    model=model,
                    usage=usage,
                    provider="openrouter",
                    raw=data,
                    metadata={"elapsed_s": elapsed, "attempt": attempt},
                )

                with self._lock:
                    self._last_response = llm_response
                return llm_response

            except requests.exceptions.Timeout as exc:
                last_exception = TimeoutError_(
                    f"OpenRouter request timed out after {timeout}s",
                    context={"timeout_s": timeout, "attempt": attempt},
                )
            except requests.exceptions.RequestException as exc:
                # Subclasses of RequestException: ConnectionError,
                # ChunkedEncodingError, ProxyError, etc.  All map to
                # "provider unreachable" (UnavailableError).  Timeout
                # is matched above and short-circuits to TimeoutError_.
                last_exception = UnavailableError(
                    f"OpenRouter request failed: {exc!s}",
                    context={"error": str(exc), "attempt": attempt},
                )
            except (
                AuthenticationError,
                ConfigurationError_,
                InvalidRequestError,
                MalformedResponseError,
            ) as exc:
                # Non-retryable provider errors.  Re-raise immediately
                # without going through the back-off loop.
                with self._lock:
                    self._error_count += 1
                raise
            except (RateLimitError, UnavailableError, TimeoutError_, ProviderError) as exc:
                # Retryable provider errors.  Fall through to the
                # back-off logic below.
                last_exception = exc
            except Exception as exc:
                with self._lock:
                    self._error_count += 1
                raise MalformedResponseError(
                    f"Unexpected error during OpenRouter call: {exc!s}",
                    cause=exc,
                    context={"attempt": attempt},
                ) from None

            if attempt < self._max_retries:
                self._sleep_with_retry_after(last_exception, attempt)

        with self._lock:
            self._error_count += 1
        if last_exception is None:
            # Should be unreachable; raise a typed error instead of a bare
            # ProviderError so callers can branch on a known code.
            last_exception = ProviderError(
                "OpenRouter failed after retries",
                code="PROVIDER_RETRIES_EXHAUSTED",
                context={"max_retries": self._max_retries},
            )
        raise last_exception

    # ------------------------------------------------------------------
    # Helpers (error mapping, sleep, statistics)
    # ------------------------------------------------------------------

    def _map_http_error(self, response: Any) -> ProviderError:
        """Map an HTTP error response to a canonical provider error.

        Headers and bodies are redacted before they enter the error
        context, so a misbehaving proxy cannot leak the
        ``Authorization`` header back through this channel.
        """
        status = response.status_code
        body = _redact(response.text[:_MAX_BODY_CHARS]) if response.text else ""

        if status == 401:
            return AuthenticationError(
                "OpenRouter authentication failed",
                context={"status": status},
            )
        if status == 429:
            ctx: Dict[str, Any] = {"status": status}
            retry_after = response.headers.get("Retry-After")
            if retry_after is not None:
                ctx["retry_after"] = retry_after
            return RateLimitError(
                "OpenRouter rate limit exceeded",
                context=ctx,
            )
        if status == 403:
            # 403 is usually an authorisation / entitlement failure.
            return AuthenticationError(
                "OpenRouter access forbidden",
                context={"status": status},
            )
        if status == 404:
            return InvalidRequestError(
                f"OpenRouter resource not found: {status}",
                context={"status": status, "response": body},
            )
        if status >= 500:
            return UnavailableError(
                f"OpenRouter server error: {status}",
                context={"status": status, "response": body},
            )
        # Other 4xx
        return InvalidRequestError(
            f"OpenRouter invalid request: {status}",
            context={"status": status, "response": body},
        )

    def _sleep_with_retry_after(
        self, last_exception: Optional[Exception], attempt: int
    ) -> None:
        """Back off before the next retry, honouring ``Retry-After`` if set.

        Falls back to a small exponential schedule (0.25s, 0.5s, 1s, …)
        with a hard ceiling at 5s so a long-running run cannot sleep
        forever between retries.
        """
        sleep_s: Optional[float] = None
        if isinstance(last_exception, RateLimitError):
            try:
                retry_after = (last_exception.context or {}).get("retry_after")
                if retry_after is not None:
                    sleep_s = float(retry_after)
            except (TypeError, ValueError):
                sleep_s = None
        if sleep_s is None:
            sleep_s = min(0.25 * (2 ** attempt), 5.0)
        try:
            time.sleep(max(0.0, sleep_s))
        except Exception:  # noqa: BLE001
            pass

    def statistics(self) -> Dict[str, Any]:
        """Return provider-level statistics.

        The API key is never included.  The LLMRequest / LLMResponse
        projections come from their ``to_dict()`` methods, which
        already redact raw provider payloads.
        """
        with self._lock:
            return {
                "type": "OpenRouterProvider",
                "name": self.name,
                "model": self._model,
                "model_pool": list(self._model_pool),
                "call_count": self._call_count,
                "error_count": self._error_count,
                "last_request": self._last_request.to_dict() if self._last_request else None,
                "last_response": self._last_response.to_dict() if self._last_response else None,
                "last_model_used": self._last_model_used,
            }

    def health(self) -> Dict[str, Any]:
        """Canonical health surface (Phase 11.5).

        The OpenRouter provider is considered ``ok`` when it has an API
        key configured.  We do NOT issue a live HTTP probe from
        ``health()``; that path is owned by the ``--llm-health`` CLI
        command and the Phase 6D.1 dry-run tests.  ``health()`` is the
        cheap, always-available surface.
        """
        with self._lock:
            configured = bool(self._api_key)
            ok = configured
            reason = "" if configured else "no API key configured"
            return {
                "name": self.name,
                "ok": ok,
                "reason": reason,
                "stats": {
                    "model": self._model,
                    "call_count": self._call_count,
                    "error_count": self._error_count,
                },
            }

    def last_request(self) -> Optional[LLMRequest]:
        with self._lock:
            return self._last_request

    def last_response(self) -> Optional[LLMResponse]:
        with self._lock:
            return self._last_response
