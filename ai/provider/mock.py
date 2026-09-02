"""
Omnix V6 — :class:`MockProvider` (Phase 5A).

A deterministic, in-process LLM provider used for tests, development,
and the manual CLI when no real key is configured.  It never touches
the network.

The provider's behaviour is fully programmable via the ``responder``
constructor argument.  When no responder is supplied, it echoes the
last user message wrapped in a ``<mock>...</mock>`` block and tracks
simple token statistics.

Isolation: :class:`MockProvider` imports nothing from Windows
automation.  Tests in :mod:`tests.test_provider_isolation` enforce
this at the package level.
"""

from __future__ import annotations

import threading
from typing import Any, Callable, Dict, Optional

from .base import LLMProvider
from .contracts import (
    FinishReason,
    LLMMessage,
    LLMRequest,
    LLMResponse,
    LLMUsage,
    MessageRole,
)
from .errors import (
    CancelledError,
    ConfigurationError_,
    InvalidRequestError,
    MalformedResponseError,
    ProviderError,
    TimeoutError_,
)


# A responder maps a request to either a response or raises.
Responder = Callable[[LLMRequest], LLMResponse]


def _default_responder(request: LLMRequest) -> LLMResponse:
    """Echo the last user message wrapped in a ``<mock>`` block.

    Deterministic and side-effect-free.
    """
    user_messages = request.user_messages
    if not user_messages:
        content = "<mock>hello</mock>"
    else:
        last = user_messages[-1].content
        content = f"<mock>{last}</mock>"
    # Naive "token" count: split on whitespace.
    prompt_tokens = sum(len(m.content.split()) for m in request.messages)
    completion_tokens = len(content.split())
    return LLMResponse(
        content=content,
        finish_reason=FinishReason.STOP,
        model=request.model or "mock-model",
        usage=LLMUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
        ),
        provider="mock",
        metadata={"echoed_user_messages": len(user_messages)},
    )


class MockProvider:
    """A deterministic fake LLM provider.

    The provider is thread-safe.  It is the canonical implementation
    used by the manual V6 CLI when no real key is configured, and by
    every Phase 5A test.
    """

    name: str = "mock"

    def __init__(
        self,
        *,
        responder: Optional[Responder] = None,
        default_latency_s: float = 0.0,
    ) -> None:
        self._responder: Responder = responder or _default_responder
        self._default_latency_s = float(default_latency_s)
        self._lock = threading.RLock()
        self._call_count = 0
        self._error_count = 0
        self._cancelled = False
        self._last_request: Optional[LLMRequest] = None

    # ============================================================ api
    def generate(self, request: LLMRequest) -> LLMResponse:
        # basic input validation
        if not isinstance(request, LLMRequest):
            raise InvalidRequestError(
                "MockProvider expected an LLMRequest",
                context={"got_type": type(request).__name__},
            )
        if not request.messages:
            raise InvalidRequestError(
                "LLMRequest must contain at least one message",
                context={"messages": 0},
            )

        with self._lock:
            self._call_count += 1
            if self._cancelled:
                raise CancelledError(
                    "MockProvider has been cancelled",
                    context={"call_count": self._call_count},
                )
            self._last_request = request

        # Simulate latency (best effort; no I/O).
        if self._default_latency_s > 0:
            import time
            time.sleep(self._default_latency_s)

        # Apply the per-request timeout if specified.  Phase 5A uses
        # a simple wall-clock check; Phase 5B can replace this with a
        # cancellable future.
        if request.timeout_s is not None and request.timeout_s > 0:
            # Mock latency is bounded by default_latency_s, so the
            # only way to provoke a real timeout is to set
            # default_latency_s > request.timeout_s from the test.
            if self._default_latency_s > request.timeout_s:
                with self._lock:
                    self._error_count += 1
                raise TimeoutError_(
                    "MockProvider simulated timeout",
                    context={
                        "timeout_s": request.timeout_s,
                        "latency_s": self._default_latency_s,
                    },
                )

        try:
            response = self._responder(request)
        except ProviderError:
            with self._lock:
                self._error_count += 1
            raise
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self._error_count += 1
            raise MalformedResponseError(
                "Responder raised an unexpected exception",
                cause=exc,
                context={"responder": getattr(self._responder, "__name__", repr(self._responder))},
            )

        if not isinstance(response, LLMResponse):
            with self._lock:
                self._error_count += 1
            raise MalformedResponseError(
                "Responder did not return an LLMResponse",
                context={"got_type": type(response).__name__},
            )
        if not response.content:
            # treat empty content as a malformed response so the Brain
            # can branch on it (Phase 5B).
            with self._lock:
                self._error_count += 1
            raise MalformedResponseError(
                "Responder returned an empty content string",
                context={"provider": self.name},
            )
        return response

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "type": "MockProvider",
                "name": self.name,
                "call_count": self._call_count,
                "error_count": self._error_count,
                "cancelled": self._cancelled,
            }

    def health(self) -> Dict[str, Any]:
        """Canonical health surface (Phase 11.5).

        :class:`MockProvider` is always considered live; it is offline
        by design and never touches the network.  We surface a small
        structured dict so the engine's :class:`HealthMonitor` and the
        CLI ``/health`` command can name the provider consistently
        with the other (real) providers.
        """
        stats = self.statistics()
        return {
            "name": self.name,
            "ok": True,
            "reason": "",
            "stats": stats,
        }

    # ======================================================= helpers
    def cancel(self) -> None:
        """Mark the provider cancelled.  Subsequent calls raise :class:`CancelledError`."""
        with self._lock:
            self._cancelled = True

    def uncancel(self) -> None:
        with self._lock:
            self._cancelled = False

    def last_request(self) -> Optional[LLMRequest]:
        with self._lock:
            return self._last_request
