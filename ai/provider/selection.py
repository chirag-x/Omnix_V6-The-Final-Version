"""
Omnix V6 — Provider selection (Phase 5A).

Provider selection is configuration-driven.  The configuration shape is:

    OMNIX_LLM_PROVIDER  = "mock" (default) | "openai" | future
    OMNIX_LLM_MODEL     = model id (optional; provider may default)

For Phase 5A only :class:`MockProvider` is wired.  The
:func:`get_provider` factory is the single entry point the Brain will
use in Phase 5B.  When an unknown provider name is supplied, the
factory raises a :class:`ProviderConfigurationError` (NOT a generic
``Exception``) so the Brain can branch on a structured failure.

The factory never imports Windows automation.  It only depends on:

    * :mod:`core.configuration` — for the existing V6 config object
    * :mod:`ai.provider.*`       — for the provider implementations
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, Optional, Tuple

from .base import LLMProvider
from .contracts import LLMRequest, LLMResponse, FinishReason, LLMUsage
from .errors import ConfigurationError_ as ProviderConfigurationError
from .mock import MockProvider
from .openrouter import OpenRouterProvider


# ---------------------------------------------------------------------------
# Env / config resolution
# ---------------------------------------------------------------------------

PROVIDER_ENV_VAR = "OMNIX_LLM_PROVIDER"
MODEL_ENV_VAR = "OMNIX_LLM_MODEL"

# Provider name -> factory.  Phase 5A only knows mock; future phases
# may add "openai", "groq", "local" without touching the Brain.
#
# The factories must accept the resolved model override (from
# :func:`_resolve_model_name`) via the ``model`` keyword and pass it
# through to the provider so the precedence is honoured:
#
#     request.model
#       > explicit provider model (OMNIX_LLM_MODEL / config.extra)
#       > first model in pool
#
# :class:`OpenRouterProvider` already implements that precedence at
# ``__init__`` and at ``generate()`` time; this factory just has to
# forward the resolved value.
_PROVIDER_REGISTRY: Dict[str, Any] = {
    "mock": lambda config, **kwargs: MockProvider(responder=smart_mock_responder),
    "openrouter": lambda config, **kwargs: OpenRouterProvider(
        api_key=config.openrouter_keys[0] if config.openrouter_keys else "",
        base_url=config.openrouter_url,
        model=kwargs.get("model"),
        model_pool=config.openrouter_model_pool,
        timeout_s=None,
        max_retries=2,
    ),
}


# Phase 14.2: the engine's default ``mock`` provider used to echo the
# user input wrapped in ``<mock>...</mock>``, which the Intent
# Interpreter then rejected as malformed JSON.  That made every
# end-to-end "Open Chrome" smoke test fail before any capability ran.
# This responder is the *engine's default* mock — it produces a
# valid, schema-conformant JSON intent for the canonical command
# patterns the LLM layer is expected to handle.  Tests that need
# bespoke behaviour pass their own responder to ``MockProvider``
# directly.  This is the same layering real OpenRouter / OpenAI
# providers use: the *factory* picks a sensible default, callers
# override.
_SMART_INTENT_PATTERNS = (
    # ("regex", "intent_kind", "param_name", "regex_group_name")
    # The param_name is the key the V6 intent spec expects; the
    # regex_group_name is the named capture in the pattern.
    (re.compile(r"\bopen\s+(?P<app>[\w\.\- ]+?)\s*$", re.IGNORECASE),
     "open_application", "app_name", "app"),
    (re.compile(r"\blaunch\s+(?P<app>[\w\.\- ]+?)\s*$", re.IGNORECASE),
     "open_application", "app_name", "app"),
    (re.compile(r"\bstart\s+(?P<app>[\w\.\- ]+?)\s*$", re.IGNORECASE),
     "open_application", "app_name", "app"),
    (re.compile(r"\bclose\s+(?P<app>[\w\.\- ]+?)\s*$", re.IGNORECASE),
     "close_application", "app_name", "app"),
    (re.compile(r"\bquit\s+(?P<app>[\w\.\- ]+?)\s*$", re.IGNORECASE),
     "close_application", "app_name", "app"),
    (re.compile(r"\bfocus\s+(?P<app>[\w\.\- ]+?)\s*$", re.IGNORECASE),
     "focus_application", "app_name", "app"),
    (re.compile(r"\bswitch\s+to\s+(?P<app>[\w\.\- ]+?)\s*$", re.IGNORECASE),
     "focus_application", "app_name", "app"),
    (re.compile(r"\bsay\s+(?P<text>.+?)\s*$", re.IGNORECASE),
     "dialogue", "text", "text"),
    (re.compile(r"\bhello\b", re.IGNORECASE),
     "inform", None, None),
)

# Phase 14.2: a compound request is two or more action clauses joined
# by an explicit coordinator ("and", "then", or a semicolon).  The
# pre-fix responder silently swallowed everything after ``and`` — the
# classic root cause of "Open Notepad and type Hello World" losing
# the typing half.  We split into ordered clauses *before* the
# single-intent pattern table runs, so each clause is independently
# classified and the union of results forms the multi-step plan.
_COMPOUND_SPLITTER = re.compile(
    r"\s+(?:and|then|after\s+that|,\s+and|;)\s+",
    re.IGNORECASE,
)
# Clauses that are too short or too generic to be a real action — we
# drop them rather than emit a bogus intent.
_COMPOUND_MIN_CLAUSE_CHARS = 2


def smart_mock_responder(request: LLMRequest) -> LLMResponse:
    """Engine default mock responder.

    Walks the last user message through a small pattern table and
    emits a schema-valid :class:`Intent` JSON object.  The patterns
    cover the canonical V6 commands used in development (open, close,
    focus, launch, quit, start, switch-to) plus greetings.

    Phase 14.2 addition: compound commands joined by ``and`` /
    ``then`` / ``;`` are split into ordered clauses BEFORE the
    single-intent pattern table runs.  Each clause is independently
    classified; the union forms a ``compound_request`` intent.  The
    deterministic planner then expands the compound into a multi-step
    plan — restoring the "type Hello World" half of
    "Open Notepad and type Hello World" that the prior responder was
    silently swallowing.

    Unmatched text is wrapped in an ``inform`` intent with
    ``information=<text>`` so the Brain still has something to
    work with rather than failing the whole pipeline on a single
    unrecognised phrase.

    The responder is intentionally narrow: it exists so the *engine*
    can be exercised end-to-end with ``OMNIX_LLM_PROVIDER=mock``
    without a network round-trip.  Real providers (OpenRouter,
    OpenAI) are still the source of truth in production.
    """
    user_messages = request.user_messages
    text = (user_messages[-1].content if user_messages else "") or ""
    text_clean = text.strip()

    # ------------------------------------------------------------------
    # Phase 14.2: compound-request detection
    # ------------------------------------------------------------------
    # Split the user utterance on explicit coordinators BEFORE the
    # single-intent pattern table runs, so the trailing clauses of
    # compound commands are not silently dropped.  Each clause must
    # still satisfy the minimum length and the union must have at
    # least 2 surviving clauses — otherwise we treat the original
    # text as a single intent and fall through to the normal path.
    clauses = _split_compound(text_clean)
    if len(clauses) >= 2:
        steps = [c.strip() for c in clauses if c.strip()]
        if all(len(s) >= _COMPOUND_MIN_CLAUSE_CHARS for s in steps):
            payload = {
                "kind": "compound_request",
                "objective": text_clean.lower(),
                "parameters": {"steps": steps},
                "confidence": 0.9,
                "source_text": text,
            }
            return LLMResponse(
                content=json.dumps(payload),
                finish_reason=FinishReason.STOP,
                model=request.model or "mock-smart",
                usage=LLMUsage(
                    prompt_tokens=0, completion_tokens=0, total_tokens=0,
                ),
                provider="mock",
                metadata={
                    "responder": "smart_mock_responder",
                    "compound": True,
                    "clause_count": len(steps),
                },
            )

    for pattern, kind, param_name, group_name in _SMART_INTENT_PATTERNS:
        m = pattern.search(text_clean)
        if not m:
            continue
        if param_name is None or group_name is None:
            payload: Dict[str, Any] = {
                "kind": kind,
                "objective": text_clean.lower(),
                "parameters": {"information": text_clean},
                "confidence": 0.9,
                "source_text": text,
            }
        else:
            value = m.group(group_name).strip().rstrip(".,!?")
            payload = {
                "kind": kind,
                "objective": text_clean.lower(),
                "parameters": {param_name: value},
                "confidence": 0.9,
                "source_text": text,
            }
        return LLMResponse(
            content=json.dumps(payload),
            finish_reason=FinishReason.STOP,
            model=request.model or "mock-smart",
            usage=LLMUsage(
                prompt_tokens=0, completion_tokens=0, total_tokens=0,
            ),
            provider="mock",
            metadata={"responder": "smart_mock_responder"},
        )

    # Fallback: emit a structured "unknown" intent so the Brain
    # surfaces a clarification request rather than crashing the
    # interpreter with a malformed-JSON error.
    payload = {
        "kind": "unknown",
        "objective": text_clean.lower() if text_clean else "no input",
        "parameters": {"raw": text},
        "confidence": 0.1,
        "source_text": text,
    }
    return LLMResponse(
        content=json.dumps(payload),
        finish_reason=FinishReason.STOP,
        model=request.model or "mock-smart",
        usage=LLMUsage(
            prompt_tokens=0, completion_tokens=0, total_tokens=0,
        ),
        provider="mock",
        metadata={"responder": "smart_mock_responder"},
    )


def _split_compound(text_clean: str) -> list:
    """Split a clean user utterance on compound-request coordinators.

    Returns the raw clause list (empty / single-element when the
    utterance is not compound).  Filtering of degenerate clauses is
    the caller's responsibility — we only split, not validate.
    """
    if not text_clean:
        return []
    if not _COMPOUND_SPLITTER.search(text_clean):
        return [text_clean]
    parts = _COMPOUND_SPLITTER.split(text_clean)
    # Strip trailing / leading whitespace from each piece.
    return [p.strip() for p in parts]


def _resolve_provider_name(config: Any) -> str:
    """Look up the provider name in the config object or environment.

    The existing V6 :class:`OmnixConfig` is a frozen dataclass; we do
    not add new fields for Phase 5A.  Resolution order:

        1. environment variable ``OMNIX_LLM_PROVIDER`` (for tests)
        2. ``config.extra["llm_provider"]`` if set
        3. default: ``"mock"``
    """
    env_name = os.environ.get(PROVIDER_ENV_VAR)
    if env_name:
        return env_name.strip().lower()
    try:
        extra = getattr(config, "extra", None) or {}
    except Exception:  # noqa: BLE001
        extra = {}
    cfg_name = extra.get("llm_provider")
    if cfg_name:
        return str(cfg_name).strip().lower()
    return "mock"


def _resolve_model_name(config: Any) -> Optional[str]:
    env_name = os.environ.get(MODEL_ENV_VAR)
    if env_name:
        return env_name.strip()
    try:
        extra = getattr(config, "extra", None) or {}
    except Exception:  # noqa: BLE001
        extra = {}
    cfg_name = extra.get("llm_model")
    if cfg_name:
        return str(cfg_name).strip()
    return None


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------

def get_provider(config: Any) -> LLMProvider:
    """Construct the configured :class:`LLMProvider`.

    Parameters
    ----------
    config:
        An :class:`OmnixConfig` (or any object with an ``.extra``
        dict).  The factory is intentionally tolerant so it can be
        used in tests with a stub.

    Returns
    -------
    LLMProvider
        The provider instance.  For Phase 5A this is always a
        :class:`MockProvider` unless the configuration explicitly
        selects another one.

    Raises
    ------
    ProviderConfigurationError
        If the configured provider name is unknown, or the provider
        was requested without a required key.
    """
    name = _resolve_provider_name(config)
    factory = _PROVIDER_REGISTRY.get(name)
    if factory is None:
        raise ProviderConfigurationError(
            f"Unknown LLM provider: {name!r}",
            context={
                "requested": name,
                "available": sorted(_PROVIDER_REGISTRY.keys()),
            },
        )
    model = _resolve_model_name(config)
    # Try the canonical (model=, config=) signature first; fall back to
    # the (config=) signature for older factories, and finally to a
    # zero-arg call so tests can register the simplest possible factory.
    for kwargs in (
        {"model": model, "config": config},
        {"config": config},
        {"model": model},
        {},
    ):
        try:
            return factory(**kwargs)
        except TypeError:
            continue
    # If no signature worked, surface the original TypeError to the caller.
    return factory(config=config)


def register_provider(name: str, factory: Any) -> None:
    """Register a new provider at runtime (Phase 5A: tests only).

    Future phases can call this from their own ``__init__`` to wire
    the real OpenAI-compatible / local providers without changing the
    Brain.
    """
    if not name or not isinstance(name, str):
        raise ProviderConfigurationError(
            "Provider name must be a non-empty string",
            context={"got": repr(name)},
        )
    if not callable(factory):
        raise ProviderConfigurationError(
            "Provider factory must be callable",
            context={"name": name, "type": type(factory).__name__},
        )
    _PROVIDER_REGISTRY[name.strip().lower()] = factory
