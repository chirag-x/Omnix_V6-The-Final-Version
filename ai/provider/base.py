"""
Omnix V6 — :class:`LLMProvider` protocol (Phase 5A).

The Brain (Phase 5B) will hold a single :class:`LLMProvider` and call
:meth:`LLMProvider.generate` to talk to a model.  Concrete providers
(:class:`MockProvider` for tests, future OpenAI-compatible provider,
future local model provider) all implement the same protocol so the
Brain does not change when the model changes.

Mandatory isolation rule
------------------------

The provider layer MUST NOT import or use any of:

    * :mod:`subprocess`
    * :mod:`pyautogui`
    * :mod:`win32gui` / :mod:`win32api`
    * :mod:`ctypes`
    * :mod:`core.capability_router`
    * any V6 *Windows service* (e.g. ``system.windows.*``,
      ``system.applications.*``)

This is enforced by :mod:`tests.test_provider_isolation`.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Protocol, runtime_checkable

from .contracts import LLMRequest, LLMResponse


@runtime_checkable
class LLMProvider(Protocol):
    """The single seam between the Brain and an LLM.

    Implementations are stateless with respect to Omnix: they take an
    :class:`LLMRequest` and return an :class:`LLMResponse`.  Any
    provider-internal state (rate-limit counters, caches, ..) is the
    provider's own concern.
    """

    name: str

    def generate(self, request: LLMRequest) -> LLMResponse:
        """Produce one :class:`LLMResponse` from a :class:`LLMRequest`.

        Implementations MUST raise :class:`ProviderError` (or a
        subclass) on any failure.  They MUST NOT raise a provider-
        specific exception (e.g. ``openai.error.OpenAIError``) out
        of the boundary.
        """
        ...

    def statistics(self) -> Dict[str, Any]:
        """Return a small dict of provider-level stats for debugging.

        Used by the Brain to surface "which provider / how many calls /
        how many errors" in the debug panel.  Implementations should
        not include any secret material in this dict.
        """
        ...

    def health(self) -> Dict[str, Any]:
        """Return a small dict describing the provider's liveness.

        The canonical shape (Phase 11.5) is::

            {
                "name":   str,    # provider name (matches LLMProvider.name)
                "ok":     bool,   # False if the provider is misconfigured / down
                "reason": str,    # short human sentence; "" when ok
                "stats":  dict,   # whatever statistics() returned
            }

        Implementations MUST NOT include any secret material in this
        dict.  Providers that have no way to introspect liveness
        (e.g. :class:`MockProvider`) should return ``{"ok": True}``.
        """
        ...
