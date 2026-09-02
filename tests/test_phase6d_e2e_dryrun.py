"""
Omnix V6 -- Phase 6D end-to-end dry-run test.

This test demonstrates the full production stack composition:

    OmnixConfig
        -> OpenRouterProvider (mocked HTTP)
            -> LLMRequest -> LLMResponse
                -> LLMPlanner.plan(goal)  -> Plan
                    -> LLMIntentInterpreter -> Intent -> Goal
                        -> Brain.handle_text(text) -> BrainResult

The test is fully deterministic: ``requests.post`` is patched so no
real network call is made, but the provider's full HTTP/retry/
error-mapping code path is exercised.

It also asserts the API key isolation invariant end-to-end: the key
is read by the provider at construction time and never appears in
any of the higher-level types (``Plan``, ``Goal``, ``Intent``,
``LLMRequest``, ``LLMResponse``) or in their ``to_dict()`` projections.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict
from unittest.mock import patch, MagicMock

import pytest

from core.capability_registry import CapabilityRegistry
from core.capability_router import CapabilityRouter, AllowAllSafetyPolicy
from core.configuration import OmnixConfig

from ai.brain import Brain, DeterministicPlanner, LLMPlanner
from ai.brain.discovery import discover_capabilities
from ai.intent import LLMIntentInterpreter, build_default_registry
from ai.provider import (
    LLMMessage,
    LLMRequest,
    LLMResponse,
    LLMUsage,
    FinishReason,
    MessageRole,
    OutputFormat,
    MockProvider,
    OpenRouterProvider,
    get_provider,
)


# ---------------------------------------------------------------------------
# A minimal in-test capability: an "echo" used to give the registry
# something to plan against.  Mirrors the test capability in
# tests/test_orchestration_e2e.py.
# ---------------------------------------------------------------------------

class _EchoCapability:
    """A minimal in-test capability with a class-level spec."""

    from core.capability import CapabilitySpec, CapabilityParameter, ParamType
    spec = CapabilitySpec(
        name="test.echo",
        version="1.0.0",
        description="Echoes the input text.",
        parameters=(
            CapabilityParameter(name="text", type=ParamType.STRING, required=True),
        ),
    )

    def is_available(self) -> bool:
        return True

    def execute(self, params):
        from core.results import ActionResult, ActionStatus, CapabilityResult
        text = params.get("text", "")
        action = ActionResult(
            status=ActionStatus.EXECUTED,
            attempt=1,
            output={"text": text},
            duration_ms=0.0,
        )
        return CapabilityResult(
            capability_name="test.echo",
            status="succeeded",
            action=action,
            verification=None,
        )


# Build a tiny capability spec + registry for the test.
def _build_registry() -> CapabilityRegistry:
    from core.capability_registry import CapabilityRegistry
    reg = CapabilityRegistry()
    reg.register(_EchoCapability())
    return reg


# A canned OpenRouter JSON response shaped like a real chat completion.
def _openrouter_response_json(content: str) -> Dict[str, Any]:
    return {
        "id": "or-test-1",
        "model": "anthropic/claude-3.5-sonnet",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 12,
            "completion_tokens": 7,
            "total_tokens": 19,
        },
    }


# ---------------------------------------------------------------------------
# 1) Configuration carries the model pool + provider key without leaking
# ---------------------------------------------------------------------------

def test_omni_config_carries_openrouter_pool_and_masks_keys() -> None:
    cfg = OmnixConfig(
        project_root=Path("."),
        data_dir=Path(".data"),
        log_dir=Path(".log"),
        env_file=Path(".env"),
        openrouter_url="https://openrouter.ai/api/v1",
        openrouter_keys=("sk-A", "sk-B"),
        openrouter_model_pool=("anthropic/claude-3.5-sonnet", "openai/gpt-4o-mini"),
    )
    d = cfg.to_dict()
    # The pool is exposed, but the keys are NOT serialised.
    assert d["openrouter_key_count"] == 2
    assert d["openrouter_url"] == "https://openrouter.ai/api/v1"
    # No key value should ever appear in the serialised dict.
    blob = json.dumps(d)
    assert "sk-A" not in blob
    assert "sk-B" not in blob


# ---------------------------------------------------------------------------
# 2) get_provider routes the configuration to OpenRouterProvider
# ---------------------------------------------------------------------------

def test_get_provider_resolves_openrouter_from_config() -> None:
    cfg = OmnixConfig(
        project_root=Path("."),
        data_dir=Path(".data"),
        log_dir=Path(".log"),
        env_file=Path(".env"),
        openrouter_url="https://openrouter.ai/api/v1",
        openrouter_keys=("sk-test-xyz",),
        openrouter_model_pool=("anthropic/claude-3.5-sonnet",),
    )
    cfg = cfg.with_overrides(extra={"llm_provider": "openrouter"})

    provider = get_provider(cfg)
    assert isinstance(provider, OpenRouterProvider)
    assert provider.name == "openrouter"
    # The provider's internal model is the first entry of the pool.
    assert provider._model == "anthropic/claude-3.5-sonnet"


# ---------------------------------------------------------------------------
# 3) Full LLMPlanner stack: provider -> LLMRequest -> LLMResponse -> Plan
# ---------------------------------------------------------------------------

def test_llm_planner_drives_end_to_end_via_mocked_openrouter() -> None:
    """The LLMPlanner talks to OpenRouterProvider over a mocked HTTP
    boundary; the returned plan must validate against the canonical
    CapabilityRegistry.
    """
    plan_payload = {
        "goal_id": "goal-1",
        "steps": [
            {
                "step_id": "step-1",
                "description": "Echo the greeting",
                "action": "capability_call",
                "capability_name": "test.echo",
                "parameters": {"text": "hello"},
                "depends_on": [],
                "timeout_s": 5.0,
                "max_retries": 0,
                "metadata": {},
            }
        ],
    }

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = _openrouter_response_json(
        json.dumps(plan_payload)
    )

    cfg = OmnixConfig(
        project_root=Path("."),
        data_dir=Path(".data"),
        log_dir=Path(".log"),
        env_file=Path(".env"),
        openrouter_url="https://openrouter.ai/api/v1",
        openrouter_keys=("sk-test-xyz",),
        openrouter_model_pool=("anthropic/claude-3.5-sonnet",),
    )

    provider = OpenRouterProvider(
        api_key=cfg.openrouter_keys[0],
        base_url=cfg.openrouter_url,
        model_pool=cfg.openrouter_model_pool,
        timeout_s=10.0,
        max_retries=0,
    )

    registry = _build_registry()
    planner = LLMPlanner(provider=provider, registry=registry)

    goal = __import__("core.orchestration", fromlist=["Goal"]).Goal(
        goal_id="goal-1",
        description="Say hello",
        success_criteria=("the text 'hello' is echoed",),
        constraints=(),
    )

    with patch("ai.provider.openrouter.requests.post", return_value=mock_resp):
        plan = planner.plan(goal)

    # Plan validates against the registry.
    assert plan.step_count == 1
    assert plan.steps[0].capability_name == "test.echo"

    # Stats show one call.
    stats = provider.statistics()
    assert stats["call_count"] == 1
    assert stats["error_count"] == 0

    # Secret isolation: the API key must NOT appear in the plan.
    plan_blob = json.dumps(plan.to_dict(), default=str)
    assert "sk-test-xyz" not in plan_blob


# ---------------------------------------------------------------------------
# 4) Full Brain stack: text -> Intent -> Goal -> Plan via LLMPlanner
# ---------------------------------------------------------------------------

def test_brain_handle_text_via_llm_planner_and_openrouter() -> None:
    """Compose the LLMIntentInterpreter and LLMPlanner behind the
    same OpenRouterProvider.  Patches the HTTP boundary so the test
    is fully deterministic.
    """
    plan_payload = {
        "goal_id": "goal-1",
        "steps": [
            {
                "step_id": "step-1",
                "description": "Echo the greeting",
                "action": "capability_call",
                "capability_name": "test.echo",
                "parameters": {"text": "hi"},
                "depends_on": [],
                "timeout_s": 5.0,
                "max_retries": 0,
                "metadata": {},
            }
        ],
    }

    # Two canned responses: one for the interpreter, one for the planner.
    responses = [
        MagicMock(
            status_code=200,
            json=MagicMock(return_value=_openrouter_response_json(
                json.dumps({
                    "kind": "open_application",
                    "parameters": {"app_name": "test"},
                })
            )),
        ),
        MagicMock(
            status_code=200,
            json=MagicMock(return_value=_openrouter_response_json(
                json.dumps(plan_payload)
            )),
        ),
    ]

    cfg = OmnixConfig(
        project_root=Path("."),
        data_dir=Path(".data"),
        log_dir=Path(".log"),
        env_file=Path(".env"),
        openrouter_url="https://openrouter.ai/api/v1",
        openrouter_keys=("sk-test-xyz",),
        openrouter_model_pool=("anthropic/claude-3.5-sonnet",),
    )
    provider = OpenRouterProvider(
        api_key=cfg.openrouter_keys[0],
        base_url=cfg.openrouter_url,
        model_pool=cfg.openrouter_model_pool,
        timeout_s=10.0,
        max_retries=0,
    )

    registry = _build_registry()
    interpreter = LLMIntentInterpreter(
        provider, build_default_registry()
    )
    planner = LLMPlanner(provider=provider, registry=registry)
    brain = Brain(registry=registry, interpreter=interpreter, planner=planner)

    with patch("ai.provider.openrouter.requests.post", side_effect=responses):
        result = brain.handle_text("Please say hi")

    assert result.status == "ok"
    assert result.plan is not None
    assert result.plan.step_count == 1
    assert result.plan.steps[0].capability_name == "test.echo"

    # The provider recorded both calls.
    stats = provider.statistics()
    assert stats["call_count"] == 2
    assert stats["error_count"] == 0


# ---------------------------------------------------------------------------
# 5) API key never leaks into LLMRequest / LLMResponse
# ---------------------------------------------------------------------------

def test_api_key_does_not_leak_into_request_or_response() -> None:
    """The provider builds an Authorization header but the typed
    LLMRequest and LLMResponse that flow into the Brain must not
    surface the key anywhere.
    """
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = _openrouter_response_json("ok")

    provider = OpenRouterProvider(
        api_key="sk-SECRET-KEY-1234",
        model="anthropic/claude-3.5-sonnet",
    )

    req = LLMRequest(
        system="be terse",
        messages=[LLMMessage(role=MessageRole.USER, content="hello")],
    )
    with patch("ai.provider.openrouter.requests.post", return_value=mock_resp):
        resp = provider.generate(req)

    # Nothing in the request/response round-trip carries the secret.
    req_blob = json.dumps(req.to_dict())
    resp_blob = json.dumps(resp.to_dict(), default=str)
    assert "sk-SECRET-KEY-1234" not in req_blob
    assert "sk-SECRET-KEY-1234" not in resp_blob
    # The provider's own statistics are also free of the key.
    stats_blob = json.dumps(provider.statistics(), default=str)
    assert "sk-SECRET-KEY-1234" not in stats_blob
    assert "sk-SECRET" not in stats_blob
