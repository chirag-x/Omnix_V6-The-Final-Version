"""
Tests for the OpenRouter provider implementation.
"""
import pytest
import requests
from unittest.mock import patch, MagicMock

from ai.provider.openrouter import OpenRouterProvider
from ai.provider.contracts import LLMRequest, LLMMessage, FinishReason, OutputFormat
from ai.provider.errors import (
    AuthenticationError,
    ConfigurationError_,
    InvalidRequestError,
    MalformedResponseError,
    RateLimitError,
    TimeoutError_,
    UnavailableError,
)

def test_openrouter_init_requires_api_key():
    with pytest.raises(ConfigurationError_, match="OpenRouter API key is required"):
        OpenRouterProvider(api_key="", model="test-model")

def test_openrouter_init_requires_model():
    with pytest.raises(ConfigurationError_, match="No model specified"):
        OpenRouterProvider(api_key="sk-1234")

def test_openrouter_init_uses_model_pool_first_item():
    provider = OpenRouterProvider(api_key="sk-1234", model_pool=("model-a", "model-b"))
    assert provider._model == "model-a"

def test_openrouter_init_explicit_model_overrides_pool():
    provider = OpenRouterProvider(
        api_key="sk-1234", model="explicit-model", model_pool=("model-a", "model-b")
    )
    assert provider._model == "explicit-model"

@patch("ai.provider.openrouter.requests.post")
def test_openrouter_generate_success(mock_post):
    # Setup mock response
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "choices": [{"message": {"content": "Hello world!"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    }
    mock_post.return_value = mock_resp

    provider = OpenRouterProvider(api_key="sk-test", model="test-model")
    req = LLMRequest(messages=[LLMMessage(role="user", content="Hi")])
    
    resp = provider.generate(req)
    
    assert resp.content == "Hello world!"
    assert resp.finish_reason == FinishReason.STOP
    assert resp.usage.prompt_tokens == 10
    
    # Verify request payload
    mock_post.assert_called_once()
    args, kwargs = mock_post.call_args
    assert kwargs["headers"]["Authorization"] == "Bearer sk-test"
    payload = kwargs["json"]
    assert payload["model"] == "test-model"
    assert len(payload["messages"]) == 1
    assert payload["messages"][0]["content"] == "Hi"

@patch("ai.provider.openrouter.requests.post")
def test_openrouter_generate_with_system_prompt_and_format(mock_post):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]
    }
    mock_post.return_value = mock_resp

    provider = OpenRouterProvider(api_key="sk-test", model="test-model")
    req = LLMRequest(
        system="You are an AI",
        messages=[LLMMessage(role="user", content="Hi")],
        output_format=OutputFormat.JSON,
        temperature=0.7,
        max_tokens=100
    )
    
    provider.generate(req)
    
    args, kwargs = mock_post.call_args
    payload = kwargs["json"]
    # System prompt should be prepended
    assert len(payload["messages"]) == 2
    assert payload["messages"][0]["role"] == "system"
    assert payload["response_format"] == {"type": "json_object"}
    assert payload["temperature"] == 0.7
    assert payload["max_tokens"] == 100

@patch("ai.provider.openrouter.time.sleep")
@patch("ai.provider.openrouter.requests.post")
def test_openrouter_retries_on_timeout(mock_post, mock_sleep):
    mock_post.side_effect = requests.exceptions.Timeout("Timed out")
    
    provider = OpenRouterProvider(api_key="sk-test", model="test-model", max_retries=1)
    req = LLMRequest(messages=[LLMMessage(role="user", content="Hi")])
    
    with pytest.raises(TimeoutError_, match="timed out"):
        provider.generate(req)
        
    assert mock_post.call_count == 2
    mock_sleep.assert_called_once()

@patch("ai.provider.openrouter.requests.post")
def test_openrouter_401_raises_authentication_error(mock_post):
    mock_resp = MagicMock()
    mock_resp.status_code = 401
    mock_resp.text = "unauthorized"
    mock_post.return_value = mock_resp

    provider = OpenRouterProvider(api_key="sk-bad", model="test-model")
    req = LLMRequest(messages=[LLMMessage(role="user", content="Hi")])

    with pytest.raises(AuthenticationError, match="authentication failed"):
        provider.generate(req)

    # 401 should NOT be retried
    assert mock_post.call_count == 1


@patch("ai.provider.openrouter.time.sleep")
@patch("ai.provider.openrouter.requests.post")
def test_openrouter_500_retries_and_raises_unavailable(mock_post, mock_sleep):
    mock_resp = MagicMock()
    mock_resp.status_code = 500
    mock_resp.text = "internal error"
    mock_post.return_value = mock_resp

    provider = OpenRouterProvider(api_key="sk-test", model="test-model", max_retries=1)
    req = LLMRequest(messages=[LLMMessage(role="user", content="Hi")])

    with pytest.raises(UnavailableError, match="server error: 500"):
        provider.generate(req)

    assert mock_post.call_count == 2
    mock_sleep.assert_called_once()

@patch("ai.provider.openrouter.requests.post")
def test_openrouter_bad_json_response(mock_post):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.side_effect = ValueError("Bad JSON")
    mock_resp.headers = {}
    mock_post.return_value = mock_resp

    provider = OpenRouterProvider(api_key="sk-test", model="test-model")
    req = LLMRequest(messages=[LLMMessage(role="user", content="Hi")])

    # Non-retryable parsing error for JSON decode failure
    with pytest.raises(MalformedResponseError):
        provider.generate(req)


# ---------------------------------------------------------------------------
# Phase 6D.1 hardening: error mapping is canonical, not a free-for-all.
# ---------------------------------------------------------------------------

@patch("ai.provider.openrouter.time.sleep")
@patch("ai.provider.openrouter.requests.post")
def test_openrouter_429_is_rate_limit_error_with_retry_after(mock_post, mock_sleep):
    """429 must raise RateLimitError; the Retry-After header is captured."""
    mock_resp = MagicMock()
    mock_resp.status_code = 429
    mock_resp.headers = {"Retry-After": "7"}
    mock_resp.text = "rate limited"
    mock_post.return_value = mock_resp

    provider = OpenRouterProvider(api_key="sk-test", model="test-model", max_retries=1)
    req = LLMRequest(messages=[LLMMessage(role="user", content="Hi")])

    with pytest.raises(RateLimitError) as excinfo:
        provider.generate(req)
    assert excinfo.value.context.get("retry_after") == "7"
    # 429 is retryable: max_retries=1 => 2 calls + 1 sleep
    assert mock_post.call_count == 2
    mock_sleep.assert_called_once()


@patch("ai.provider.openrouter.requests.post")
def test_openrouter_403_is_authentication_error(mock_post):
    """403 is an authorisation / entitlement failure, not an InvalidRequestError."""
    mock_resp = MagicMock()
    mock_resp.status_code = 403
    mock_resp.text = "forbidden"
    mock_post.return_value = mock_resp

    provider = OpenRouterProvider(api_key="sk-test", model="test-model")
    req = LLMRequest(messages=[LLMMessage(role="user", content="Hi")])

    with pytest.raises(AuthenticationError, match="forbidden"):
        provider.generate(req)
    # 403 is NOT retryable
    assert mock_post.call_count == 1


@patch("ai.provider.openrouter.requests.post")
def test_openrouter_404_is_invalid_request_error(mock_post):
    mock_resp = MagicMock()
    mock_resp.status_code = 404
    mock_resp.text = "model not found"
    mock_post.return_value = mock_resp

    provider = OpenRouterProvider(api_key="sk-test", model="test-model")
    req = LLMRequest(messages=[LLMMessage(role="user", content="Hi")])

    with pytest.raises(InvalidRequestError, match="not found"):
        provider.generate(req)
    assert mock_post.call_count == 1


@patch("ai.provider.openrouter.time.sleep")
@patch("ai.provider.openrouter.requests.post")
def test_openrouter_network_error_is_unavailable(mock_post, mock_sleep):
    """A non-timeout RequestException must map to UnavailableError."""
    mock_post.side_effect = requests.exceptions.ConnectionError("DNS fail")

    provider = OpenRouterProvider(api_key="sk-test", model="test-model", max_retries=1)
    req = LLMRequest(messages=[LLMMessage(role="user", content="Hi")])

    with pytest.raises(UnavailableError, match="OpenRouter request failed"):
        provider.generate(req)
    assert mock_post.call_count == 2
    mock_sleep.assert_called_once()


@patch("ai.provider.openrouter.requests.post")
def test_openrouter_empty_content_is_malformed(mock_post):
    """Empty content with no length/filter finish reason is malformed."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "choices": [{"message": {"content": ""}, "finish_reason": "stop"}],
    }
    mock_post.return_value = mock_resp

    provider = OpenRouterProvider(api_key="sk-test", model="test-model")
    req = LLMRequest(messages=[LLMMessage(role="user", content="Hi")])

    with pytest.raises(MalformedResponseError, match="empty content"):
        provider.generate(req)


@patch("ai.provider.openrouter.requests.post")
def test_openrouter_request_model_overrides_provider_model(mock_post):
    """Per-call request.model is precedence level 1 (highest)."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
    }
    mock_post.return_value = mock_resp

    provider = OpenRouterProvider(
        api_key="sk-test", model="provider-model", model_pool=("a", "b")
    )
    req = LLMRequest(
        messages=[LLMMessage(role="user", content="Hi")],
        model="request-model",
    )
    provider.generate(req)
    payload = mock_post.call_args.kwargs["json"]
    assert payload["model"] == "request-model"


def test_openrouter_repr_redacts_api_key():
    """R-12: __repr__ must never include the API key."""
    p = OpenRouterProvider(api_key="sk-SECRET-KEY-XYZ", model="m")
    text = repr(p)
    assert "sk-SECRET-KEY-XYZ" not in text
    assert "***" in text


def test_openrouter_max_retries_is_clamped_to_cap():
    """A caller-supplied max_retries > 5 must be clamped to 5."""
    p = OpenRouterProvider(api_key="sk", model="m", max_retries=9999)
    assert p._max_retries == 5


def test_openrouter_max_retries_floor_is_zero():
    p = OpenRouterProvider(api_key="sk", model="m", max_retries=-10)
    assert p._max_retries == 0


def test_openrouter_redacts_bearer_token_from_response_body():
    """If a misbehaving proxy echoes the auth header back, the redactor strips it."""
    from ai.provider.openrouter import _redact
    out = _redact("Error: Bearer sk-LEAKED-KEY-1234 in upstream body")
    assert "sk-LEAKED-KEY-1234" not in out
    assert "Bearer ***" in out
    # Non-bearer text passes through.
    assert _redact("hello world") == "hello world"
    assert _redact("") == ""


