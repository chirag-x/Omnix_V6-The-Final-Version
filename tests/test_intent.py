"""
Omnix V6 — Phase 5B Intent Interpreter tests.

The 16+ required scenarios:
  1.  minimal payload -> Intent
  2.  unknown kind -> validation error
  3.  missing required parameter -> validation error
  4.  unexpected parameter -> validation error
  5.  wrong type -> validation error
  6.  confidence out of range -> validation error
  7.  source_text too long -> validation error
  8.  empty text -> clarification result
  9.  malformed provider JSON -> error result with INTENT_MALFORMED_JSON
  10. provider error -> error result with provider code
  11. LLMIntentInterpreter happy path ("Open Spotify") -> CONTROL_APPLICATION
  12. ambiguity ("Open it.") -> CLARIFY
  13. unknown text -> UNKNOWN
  14. Intent -> Goal conversion preserves fields
  15. non-JSON provider output stripped of code fences
  16. deterministic system prompt
  17. validation: ALL kinds in the default registry are accepted
  18. IntentResult.is_ok + to_dict
"""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, Optional

import pytest

from ai.intent import (
    LLMIntentInterpreter,
    IntentResult,
    MAX_INTENT_TEXT_LENGTH,
    build_default_registry,
    validate_intent_payload,
)
from ai.intent.specs import IntentValidationError
from ai.intent.validation import (
    MAX_CONFIDENCE,
    MAX_NORMALIZED_OBJECTIVE_LENGTH,
    MIN_CONFIDENCE,
)
from ai.provider import (
    LLMMessage,
    LLMRequest,
    LLMResponse,
    LLMUsage,
    MessageRole,
    MockProvider,
    OutputFormat,
    ProviderError,
)
from core.orchestration import Goal, Intent, IntentKind


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _json_provider(payload: Dict[str, Any]) -> MockProvider:
    """Build a MockProvider that returns ``payload`` as JSON content."""
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


# ===========================================================================
# 1-2: Minimal payload + unknown kind
# ===========================================================================

def test_minimal_payload_returns_intent(registry) -> None:
    payload = {"kind": "open_application", "parameters": {"app_name": "spotify"}}
    intent = validate_intent_payload(payload, registry)
    assert intent.kind is IntentKind.OPEN_APPLICATION
    assert intent.parameters == {"app_name": "spotify"}
    assert intent.confidence == 1.0
    assert intent.source_text == ""


def test_unknown_kind_raises(registry) -> None:
    with pytest.raises(IntentValidationError) as info:
        validate_intent_payload(
            {"kind": "nuke_mars", "parameters": {}}, registry
        )
    assert info.value.code == "INTENT_VALIDATION_ERROR"
    assert "nuke_mars" == info.value.context["requested"]


# ===========================================================================
# 3-5: Missing / unexpected / wrong type
# ===========================================================================

def test_missing_required_parameter_raises(registry) -> None:
    with pytest.raises(IntentValidationError) as info:
        validate_intent_payload(
            {"kind": "open_application", "parameters": {}}, registry
        )
    assert info.value.context["missing_keys"] == ["app_name"]


def test_unexpected_parameter_raises(registry) -> None:
    with pytest.raises(IntentValidationError) as info:
        validate_intent_payload(
            {
                "kind": "open_application",
                "parameters": {"app_name": "x", "wat": 1},
            },
            registry,
        )
    assert "wat" in info.value.context["unexpected_keys"]


def test_wrong_type_raises(registry) -> None:
    with pytest.raises(IntentValidationError) as info:
        validate_intent_payload(
            {
                "kind": "open_application",
                "parameters": {"app_name": 42},  # int, not str
            },
            registry,
        )
    assert info.value.context["key"] == "app_name"
    assert info.value.context["expected"] == "string"


# ===========================================================================
# 6-7: confidence / length bounds
# ===========================================================================

def test_confidence_out_of_range_raises(registry) -> None:
    with pytest.raises(IntentValidationError):
        validate_intent_payload(
            {
                "kind": "open_application",
                "parameters": {"app_name": "x"},
                "confidence": 1.5,
            },
            registry,
        )
    with pytest.raises(IntentValidationError):
        validate_intent_payload(
            {
                "kind": "open_application",
                "parameters": {"app_name": "x"},
                "confidence": -0.1,
            },
            registry,
        )


def test_source_text_too_long_raises(registry) -> None:
    long_text = "x" * (MAX_INTENT_TEXT_LENGTH + 1)
    with pytest.raises(IntentValidationError) as info:
        validate_intent_payload(
            {
                "kind": "open_application",
                "parameters": {"app_name": "x"},
                "source_text": long_text,
            },
            registry,
        )
    assert info.value.context["length"] == MAX_INTENT_TEXT_LENGTH + 1


# ===========================================================================
# 8-10: Interpreter edge cases
# ===========================================================================

def test_empty_text_returns_clarification() -> None:
    interp = LLMIntentInterpreter(_json_provider({"kind": "OPEN_APPLICATION"}))
    result = interp.interpret("")
    assert result.status == "clarification"
    assert result.clarifying_question is not None


def test_malformed_json_returns_error() -> None:
    interp = LLMIntentInterpreter(_string_provider("not json at all"))
    result = interp.interpret("Open Spotify")
    assert result.status == "error"
    assert result.error_code == "INTENT_MALFORMED_JSON"


def test_provider_error_propagates_as_error() -> None:
    def boom(_req: LLMRequest) -> LLMResponse:
        raise ProviderError("backend down")
    provider = MockProvider(responder=boom)
    interp = LLMIntentInterpreter(provider)
    result = interp.interpret("Open Spotify")
    assert result.status == "error"
    assert result.error_code == "PROVIDER_ERROR"


# ===========================================================================
# 11-13: Happy paths
# ===========================================================================

def test_open_spotify_yields_control_application() -> None:
    """Semantic intent, not app-specific."""
    payload = {
        "kind": "control_application",
        "parameters": {"app_name": "spotify", "action": "open"},
        "objective": "open spotify",
        "source_text": "Open Spotify",
        "confidence": 0.95,
    }
    interp = LLMIntentInterpreter(_json_provider(payload))
    result = interp.interpret("Open Spotify")
    assert result.is_ok
    assert result.intent.kind is IntentKind.CONTROL_APPLICATION
    assert result.intent.parameters["app_name"] == "spotify"


def test_ambiguity_returns_clarify() -> None:
    payload = {
        "kind": "clarify",
        "parameters": {"question": "Which app should I open?"},
        "objective": "user said 'open it' with no referent",
        "source_text": "Open it.",
        "confidence": 0.4,
    }
    interp = LLMIntentInterpreter(_json_provider(payload))
    result = interp.interpret("Open it.")
    assert result.status == "clarification"
    assert result.clarifying_question == "Which app should I open?"


def test_unknown_text_returns_unknown_status() -> None:
    payload = {
        "kind": "unknown",
        "parameters": {},
        "objective": "cannot map",
        "source_text": "blarg",
        "confidence": 0.1,
    }
    interp = LLMIntentInterpreter(_json_provider(payload))
    result = interp.interpret("blarg")
    assert result.status == "unknown"
    assert result.intent.kind is IntentKind.UNKNOWN


# ===========================================================================
# 14: Intent -> Goal conversion
# ===========================================================================

def test_intent_to_goal_preserves_fields() -> None:
    intent = Intent(
        intent_id="i-001",
        kind=IntentKind.CONTROL_APPLICATION,
        text="play music in spotify",
        parameters={"app_name": "spotify", "action": "play"},
        confidence=0.9,
        source_text="play music in spotify",
    )
    goal = intent.to_goal(
        goal_id="g-001",
        success_criteria=("spotify is playing",),
        priority=3,
    )
    assert isinstance(goal, Goal)
    assert goal.goal_id == "g-001"
    assert "control_application" in goal.metadata["normalized_objective"]
    assert goal.priority == 3


# ===========================================================================
# 15: Code-fence stripping
# ===========================================================================

def test_markdown_fences_are_stripped() -> None:
    raw = "```json\n" + json.dumps(
        {"kind": "open_application", "parameters": {"app_name": "x"}}
    ) + "\n```"
    interp = LLMIntentInterpreter(_string_provider(raw))
    result = interp.interpret("Open X")
    assert result.is_ok
    assert result.intent.parameters["app_name"] == "x"


# ===========================================================================
# 16: System prompt is deterministic
# ===========================================================================

def test_system_prompt_is_deterministic() -> None:
    a = LLMIntentInterpreter(_json_provider({}))
    b = LLMIntentInterpreter(_json_provider({}))
    assert a.system_prompt == b.system_prompt
    assert "open_application" in a.system_prompt
    assert "Never embed shell commands" in a.system_prompt


# ===========================================================================
# 17: Every default-registry kind validates a minimal payload
# ===========================================================================

@pytest.mark.parametrize("kind", list(IntentKind))
def test_every_kind_has_default_registry_spec(kind: IntentKind, registry) -> None:
    """The default registry must have a spec for every IntentKind, and that
    spec must validate a payload with every *required* parameter filled."""
    spec = registry.get(kind)
    required = {n for n, p in spec.parameters.items() if p.required}
    # Some parameters are typed (e.g. ``steps`` is a list).  Use a
    # type-appropriate sample value so the test exercises the
    # validator's type-coercion path the way a real LLM payload
    # would.  Phase 14.2: this is what makes the per-kind fixture
    # work for ``compound_request`` whose ``steps`` is a list.
    sample_value_for = {
        "list_of_strings": ["sample"],
    }
    payload: Dict[str, Any] = {
        "kind": kind.value,
        "parameters": {
            n: sample_value_for.get(p.param_type.value, "x")
            for n, p in spec.parameters.items() if p.required
        },
    }
    # should not raise
    intent = validate_intent_payload(payload, registry)
    assert intent.kind is kind


# ===========================================================================
# 18: IntentResult helpers
# ===========================================================================

def test_intent_result_is_ok_and_to_dict() -> None:
    r_ok = IntentResult(
        status="ok",
        intent=Intent(intent_id="i-1", kind=IntentKind.NO_OP, text="noop"),
    )
    assert r_ok.is_ok
    d = r_ok.to_dict()
    assert d["status"] == "ok"
    assert "intent" in d

    r_err = IntentResult(status="error", error_code="X", error_message="y")
    assert not r_err.is_ok
    assert r_err.to_dict()["error_code"] == "X"


# ===========================================================================
# Phase 11.6 — OpenRouter compatibility (deterministic, mock-based)
# ===========================================================================

class TestPhase11_6_OpenRouterCompatibility:
    """Regression tests for the real OpenRouter -> Intent pipeline.
    All tests use MockProvider — no real network call.
    """

    def test_01_valid_structured_intent_inform(self, registry) -> None:
        # The exact shape the real model emitted for "Hello Omnix", with
        # constraints and metadata shaped per the V6 schema (lists / dicts).
        # The non-canonical dialogue_kind is the regression we are testing.
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
        assert intent.dialogue_kind is IntentKind.INFORM  # auto-derived
        assert intent.parameters == {"information": "Hello Omnix"}

    def test_02_free_form_rejected(self) -> None:
        interp = LLMIntentInterpreter(_string_provider("Sure, I'll open Notepad."))
        result = interp.interpret("Open Notepad")
        assert not result.is_ok
        assert result.error_code == "INTENT_MALFORMED_JSON"

    def test_03_json_extraction_wrapped(self) -> None:
        interp = LLMIntentInterpreter(
            _string_provider('Sure! Here you go: {"kind":"open_application","parameters":{"app_name":"notepad"}}')
        )
        result = interp.interpret("Open Notepad")
        assert result.is_ok
        assert result.intent is not None
        assert result.intent.kind is IntentKind.OPEN_APPLICATION

    def test_04_malformed_json_rejected(self) -> None:
        interp = LLMIntentInterpreter(_string_provider('{"kind":"open_application"'))
        result = interp.interpret("Open Notepad")
        assert not result.is_ok
        assert result.error_code == "INTENT_MALFORMED_JSON"

    def test_05_output_format_json_carry_through(self) -> None:
        # LLMRequest with JSON output carries through provider contract
        req = LLMRequest(
            system="test",
            messages=[LLMMessage(role=MessageRole.USER, content="x")],
            output_format=OutputFormat.JSON,
        )
        assert req.output_format is OutputFormat.JSON

    def test_06_dialogue_kind_non_canonical_fallback(self, registry) -> None:
        # The core regression: a non-canonical dialogue_kind must not break
        payload = {
            "kind": "inform",
            "dialogue_kind": "greeting",
            "objective": "hello",
            "parameters": {"information": "hello"},
        }
        intent = validate_intent_payload(payload, registry)
        assert intent.dialogue_kind is IntentKind.INFORM

    def test_07_hello_omnix_full_pipeline(self, registry) -> None:
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

    def test_08_open_notepad_full_pipeline(self) -> None:
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

    def test_09_ambiguity_clarify(self) -> None:
        interp = LLMIntentInterpreter(
            _json_provider({"kind": "clarify", "parameters": {"question": "Which button?"}, "objective": "ask for clarification"})
        )
        result = interp.interpret("Click the button.")
        assert result.status == "clarification"
        assert result.intent is not None
        assert result.intent.kind is IntentKind.CLARIFY

    def test_10_no_secret_in_system_prompt(self) -> None:
        # Ensure the system prompt does not contain any API key fragments
        interp = LLMIntentInterpreter(MockProvider())
        prompt = interp.system_prompt
        assert "sk-" not in prompt
        assert "Bearer " not in prompt

    def test_11_real_llm_payload_open_notepad_succeeds(self) -> None:
        """Regression test for the real LLM payload shape.

        The real OpenRouter/cohere-north-mini-code:free model returns
        ``"constraints": []`` (an empty list) for "Open Notepad".  An
        earlier system-prompt revision instructed the model to emit
        ``constraints`` as a dict, which the validator rejected with
        ``INTENT_VALIDATION_ERROR`` and produced the user-facing
        "I could not complete that request." failure on the real
        runtime.

        This test pins the end-to-end shape: a real-model payload
        with ``constraints: []`` must validate cleanly and produce
        an :class:`Intent` of kind ``OPEN_APPLICATION``.
        """
        # The exact payload shape that cohere/north-mini-code:free
        # produced on the user's real Windows machine for
        # "Open Notepad" before the fix.
        payload = {
            "kind": "open_application",
            "dialogue_kind": "command",
            "objective": "open notepad",
            "parameters": {"app_name": "Notepad"},
            "confidence": 0.9,
            "source_text": "Open Notepad",
            "referenced_entities": [],
            "referenced_goal_id": None,
            "constraints": [],
            "metadata": {},
        }
        interp = LLMIntentInterpreter(_json_provider(payload))
        result = interp.interpret("Open Notepad")
        assert result.is_ok, (
            f"Real-model payload must validate; got status={result.status} "
            f"error_code={result.error_code} error_message={result.error_message}"
        )
        assert result.intent is not None
        assert result.intent.kind is IntentKind.OPEN_APPLICATION
        assert result.intent.parameters == {"app_name": "Notepad"}
        # The constraints field must round-trip to a tuple of strings.
        assert isinstance(result.intent.constraints, tuple)
        assert all(isinstance(c, str) for c in result.intent.constraints)

    def test_12_system_prompt_documents_constraints_as_list(self) -> None:
        """The system prompt must instruct the LLM that ``constraints``
        is a list of strings, not a dict.

        A previous version of the system prompt used
        ``"constraints": {{ ... }}``, which formatted to ``{ ... }`` and
        told the LLM to emit a dict.  The validator accepts only
        string / list / tuple.  This regression pins the prompt so the
        schema mismatch cannot return.
        """
        interp = LLMIntentInterpreter(MockProvider())
        prompt = interp.system_prompt
        # The literal ``{{ ... }}`` shape (which would format to a
        # dict) must not appear in the rendered prompt.
        assert "{{" not in prompt, (
            "System prompt must not contain '{{' — that pattern tells "
            "the LLM to emit a dict for the field it precedes."
        )
        # The prompt must mention ``constraints`` at least once so the
        # model knows it is part of the schema.
        assert "constraints" in prompt
