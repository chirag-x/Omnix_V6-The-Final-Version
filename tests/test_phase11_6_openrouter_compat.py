"""
Omnix V6 — Phase 11.6 OpenRouter / Intent compatibility test contract.

This file consolidates the Phase 11.6 contract tests so the new
behaviour can be re-run as a focused unit.  All tests use
``MockProvider``; no real OpenRouter call is made in pytest.

The contract:

    1. Valid structured Intent responses (with optional
       ``dialogue_kind``) are accepted.
    2. A non-canonical ``dialogue_kind`` (e.g. ``"greeting"``) is
       tolerated and falls back to auto-derivation; the action
       ``kind`` is still strictly validated.
    3. Free-form prose (no JSON object) is rejected with
       ``INTENT_MALFORMED_JSON``.
    4. JSON wrapped in a small amount of additional prose is
       located and accepted.
    5. Truly malformed JSON is rejected with
       ``INTENT_MALFORMED_JSON``.
    6. ``LLMRequest(output_format=OutputFormat.JSON)`` carries
       through the provider contract.
    7. The system prompt never contains an API key fragment.
    8. CLARIFY outcomes are surfaced as ``status="clarification"``
       with the question in ``parameters``.
    9. ``inform`` (dialogue) and ``open_application`` (action)
       intents flow through the interpreter end-to-end.
"""

from __future__ import annotations

import json
from typing import Any, Dict

import pytest

from ai.intent import (
    LLMIntentInterpreter,
    build_default_registry,
    validate_intent_payload,
)
from ai.intent.specs import IntentValidationError
from ai.provider import (
    LLMMessage,
    LLMRequest,
    LLMResponse,
    LLMUsage,
    MessageRole,
    MockProvider,
    OutputFormat,
)
from core.orchestration import IntentKind


def _json_provider(payload: Dict[str, Any]) -> MockProvider:
    def responder(_req: LLMRequest) -> LLMResponse:
        return LLMResponse(
            content=json.dumps(payload),
            finish_reason="stop",
            model="mock",
            usage=LLMUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            provider="mock",
        )
    return MockProvider(responder=responder)


def _string_provider(text: str) -> MockProvider:
    def responder(_req: LLMRequest) -> LLMResponse:
        return LLMResponse(
            content=text,
            finish_reason="stop",
            model="mock",
            usage=LLMUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            provider="mock",
        )
    return MockProvider(responder=responder)


@pytest.fixture
def registry():
    return build_default_registry()


def test_valid_structured_intent_inform(registry) -> None:
    payload = {
        "kind": "inform",
        "dialogue_kind": "greeting",  # non-canonical, must fall back
        "objective": "greet the assistant",
        "parameters": {"information": "Hello Omnix"},
        "confidence": 0.9,
        "source_text": "Hello Omnix",
        "referenced_entities": ["Omnix"],
        "referenced_goal_id": None,
        "constraints": [],
        "metadata": {},
    }
    intent = validate_intent_payload(payload, registry)
    assert intent.kind is IntentKind.INFORM
    assert intent.dialogue_kind is IntentKind.INFORM
    assert intent.parameters == {"information": "Hello Omnix"}


def test_free_form_rejected() -> None:
    interp = LLMIntentInterpreter(_string_provider("Sure, I'll open Notepad."))
    result = interp.interpret("Open Notepad")
    assert not result.is_ok
    assert result.error_code == "INTENT_MALFORMED_JSON"


def test_json_extraction_wrapped() -> None:
    interp = LLMIntentInterpreter(
        _string_provider(
            'Sure! Here you go: {"kind":"open_application",'
            '"parameters":{"app_name":"notepad"}}'
        )
    )
    result = interp.interpret("Open Notepad")
    assert result.is_ok
    assert result.intent is not None
    assert result.intent.kind is IntentKind.OPEN_APPLICATION


def test_malformed_json_rejected() -> None:
    interp = LLMIntentInterpreter(_string_provider('{"kind":"open_application"'))
    result = interp.interpret("Open Notepad")
    assert not result.is_ok
    assert result.error_code == "INTENT_MALFORMED_JSON"


def test_output_format_json_carry_through() -> None:
    req = LLMRequest(
        system="test",
        messages=[LLMMessage(role=MessageRole.USER, content="x")],
        output_format=OutputFormat.JSON,
    )
    assert req.output_format is OutputFormat.JSON


def test_dialogue_kind_non_canonical_fallback(registry) -> None:
    payload = {
        "kind": "inform",
        "dialogue_kind": "greeting",
        "objective": "hello",
        "parameters": {"information": "hello"},
    }
    intent = validate_intent_payload(payload, registry)
    assert intent.dialogue_kind is IntentKind.INFORM


def test_hello_omnix_full_pipeline(registry) -> None:
    payload = {
        "kind": "inform",
        "dialogue_kind": "greeting",
        "objective": "greet the assistant",
        "parameters": {"information": "Hello Omnix"},
        "confidence": 0.9,
        "source_text": "Hello Omnix",
        "referenced_entities": ["Omnix"],
        "referenced_goal_id": None,
        "constraints": [],
        "metadata": {},
    }
    interp = LLMIntentInterpreter(_json_provider(payload))
    result = interp.interpret("Hello Omnix")
    assert result.is_ok
    assert result.intent is not None
    assert result.intent.kind is IntentKind.INFORM
    assert result.intent.parameters.get("information") == "Hello Omnix"


def test_open_notepad_full_pipeline() -> None:
    payload = {
        "kind": "open_application",
        "parameters": {"app_name": "notepad"},
        "objective": "open notepad",
        "confidence": 0.9,
        "source_text": "Open Notepad",
    }
    interp = LLMIntentInterpreter(_json_provider(payload))
    result = interp.interpret("Open Notepad")
    assert result.is_ok
    assert result.intent.kind is IntentKind.OPEN_APPLICATION
    assert result.intent.parameters == {"app_name": "notepad"}


def test_clarification_intent() -> None:
    interp = LLMIntentInterpreter(
        _json_provider(
            {
                "kind": "clarify",
                "parameters": {"question": "Which button?"},
                "objective": "ask for clarification",
            }
        )
    )
    result = interp.interpret("Click the button.")
    assert result.status == "clarification"
    assert result.intent is not None
    assert result.intent.kind is IntentKind.CLARIFY


def test_no_secret_in_system_prompt() -> None:
    interp = LLMIntentInterpreter(MockProvider())
    prompt = interp.system_prompt
    # No "sk-" prefix (any provider key format) and no "Bearer " header.
    assert "sk-" not in prompt
    assert "Bearer " not in prompt
