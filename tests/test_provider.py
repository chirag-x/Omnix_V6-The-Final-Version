"""
Omnix V6 — Phase 5A LLM provider functional tests.

These tests cover the 10 deterministic scenarios required by the
Phase 5A directive.  They do not need an external API, an OpenAI key,
or a network connection.  Every test is fully in-process.

The categories map to the directive as follows:

    (1)  Provider request construction        — test_request_construction_*
    (2)  Provider response parsing            — test_response_parsing_*
    (3)  Provider error handling              — test_error_handling_*
    (4)  Timeout / cancellation behavior      — test_timeout_and_cancellation
    (5)  Mock provider                        — test_mock_provider_*
    (6)  Provider selection                   — test_provider_selection_*
    (7)  Invalid configuration                — test_invalid_configuration
    (8)  Malformed provider response          — test_malformed_response_*
    (9)  Provider cannot access automation    — see test_provider_isolation.py
    (10) Provider cannot directly execute     — see test_provider_isolation.py
              capabilities
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from core.configuration import OmnixConfig

from ai.provider import (
    LLMMessage,
    LLMRequest,
    LLMResponse,
    LLMUsage,
    MessageRole,
    FinishReason,
    OutputFormat,
    ProviderError,
    AuthenticationError,
    TimeoutError_,
    RateLimitError,
    UnavailableError,
    InvalidRequestError,
    MalformedResponseError,
    CancelledError,
    ConfigurationError_,
    MockProvider,
    get_provider,
    register_provider,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolate_provider_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip the LLM env vars so tests are not order-dependent.

    Each test is allowed to set its own value; we just guarantee the
    ambient shell state is clean.
    """
    monkeypatch.delenv("OMNIX_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("OMNIX_LLM_MODEL", raising=False)


@pytest.fixture
def base_config() -> OmnixConfig:
    """A minimal :class:`OmnixConfig` for unit tests.

    No real paths are needed because the provider layer does not touch
    the filesystem.
    """
    return OmnixConfig(
        project_root=Path("."),
        data_dir=Path(".data"),
        log_dir=Path(".log"),
        env_file=Path(".env"),
    )


# ===========================================================================
# (1) Provider request construction
# ===========================================================================

class TestRequestConstruction:
    def test_minimal_request_uses_defaults(self) -> None:
        req = LLMRequest(messages=[LLMMessage(role=MessageRole.USER, content="hi")])
        assert req.output_format is OutputFormat.TEXT
        assert req.temperature is None
        assert req.max_tokens is None
        assert req.timeout_s is None
        assert req.model is None
        assert req.system is None
        assert req.options == {}
        assert req.caller is None

    def test_user_messages_property_returns_only_user_turns(self) -> None:
        req = LLMRequest(
            messages=[
                LLMMessage(role=MessageRole.SYSTEM, content="you are helpful"),
                LLMMessage(role=MessageRole.USER, content="hello"),
                LLMMessage(role=MessageRole.ASSISTANT, content="hi back"),
                LLMMessage(role=MessageRole.USER, content="how are you?"),
            ],
        )
        assert len(req.user_messages) == 2
        assert [m.content for m in req.user_messages] == [
            "hello", "how are you?",
        ]

    def test_with_message_appends(self) -> None:
        req = LLMRequest(messages=[LLMMessage(role=MessageRole.USER, content="a")])
        req2 = req.with_message(LLMMessage(role=MessageRole.USER, content="b"))
        assert len(req.messages) == 1 and req.messages[0].content == "a"
        assert [m.content for m in req2.messages] == ["a", "b"]
        # original is untouched (frozen)
        assert len(req.messages) == 1

    def test_to_dict_round_trip(self) -> None:
        req = LLMRequest(
            system="be terse",
            messages=[LLMMessage(role=MessageRole.USER, content="ping")],
            output_format=OutputFormat.JSON,
            model="unit-test-model",
            temperature=0.25,
            max_tokens=64,
            timeout_s=5.0,
            options={"top_p": 0.9},
            caller="tests",
        )
        d = req.to_dict()
        assert d["system"] == "be terse"
        assert d["model"] == "unit-test-model"
        assert d["output_format"] == "json"
        assert d["temperature"] == 0.25
        assert d["max_tokens"] == 64
        assert d["timeout_s"] == 5.0
        assert d["caller"] == "tests"
        # When ``system`` is supplied, it is prepended as a SYSTEM
        # message by ``LLMRequest.__post_init__`` so the provider
        # never has to remember the difference.
        assert d["messages"] == [
            {"role": "system", "content": "be terse"},
            {"role": "user", "content": "ping"},
        ]


# ===========================================================================
# (2) Provider response parsing
# ===========================================================================

class TestResponseParsing:
    def test_response_carries_full_payload(self) -> None:
        usage = LLMUsage(prompt_tokens=3, completion_tokens=5, total_tokens=8)
        resp = LLMResponse(
            content="hello",
            finish_reason=FinishReason.STOP,
            model="m",
            usage=usage,
            provider="mock",
            raw={"raw_field": "ignored-but-present"},
            metadata={"trace": "abc"},
        )
        d = resp.to_dict()
        assert d["content"] == "hello"
        assert d["finish_reason"] == "stop"
        assert d["model"] == "m"
        assert d["provider"] == "mock"
        assert d["usage"]["prompt_tokens"] == 3
        assert d["usage"]["completion_tokens"] == 5
        assert d["usage"]["total_tokens"] == 8
        assert d["metadata"] == {"trace": "abc"}

    def test_usage_is_complete(self) -> None:
        good = LLMUsage(prompt_tokens=1, completion_tokens=2, total_tokens=3)
        assert good.is_complete()
        bad = LLMUsage(prompt_tokens=1, completion_tokens=2, total_tokens=None)
        assert not bad.is_complete()

    def test_response_never_carries_secret(self) -> None:
        """A response to_dict must not surface secret-looking fields by accident.

        Provider implementations that pass ``raw`` through to the
        response carry their own responsibility; the typed surface
        itself has no place for keys.
        """
        resp = LLMResponse(
            content="x",
            finish_reason=FinishReason.STOP,
            model="m",
            usage=LLMUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            provider="mock",
            raw={"api_key": "sk-shhh"},
        )
        d = resp.to_dict()
        assert "api_key" not in d
        assert "raw" not in d  # raw is not in the public dict


# ===========================================================================
# (3) Provider error handling
# ===========================================================================

class TestErrorHandling:
    @pytest.mark.parametrize("exc_cls, expected_code", [
        (AuthenticationError, "PROVIDER_AUTH_FAILED"),
        (TimeoutError_, "PROVIDER_TIMEOUT"),
        (RateLimitError, "PROVIDER_RATE_LIMITED"),
        (UnavailableError, "PROVIDER_UNAVAILABLE"),
        (InvalidRequestError, "PROVIDER_INVALID_REQUEST"),
        (MalformedResponseError, "PROVIDER_MALFORMED_RESPONSE"),
        (CancelledError, "PROVIDER_CANCELLED"),
    ])
    def test_structured_errors_have_distinct_codes(
        self, exc_cls: type, expected_code: str,
    ) -> None:
        exc = exc_cls("oops", context={"k": "v"})
        assert exc.code == expected_code
        assert exc.message == "oops"
        d = exc.to_dict()
        assert d["code"] == expected_code
        assert d["message"] == "oops"
        assert d["context"] == {"k": "v"}
        assert exc.__cause__ is None

    def test_provider_error_subclasses_share_root(self) -> None:
        """All provider errors must be ProviderError subclasses for the
        Brain to branch on a single base type."""
        for cls in (
            AuthenticationError, TimeoutError_, RateLimitError,
            UnavailableError, InvalidRequestError, MalformedResponseError,
            CancelledError, ConfigurationError_,
        ):
            assert issubclass(cls, ProviderError), cls

    def test_provider_error_preserves_cause(self) -> None:
        try:
            try:
                raise ValueError("boom")
            except ValueError as inner:
                raise MalformedResponseError("bad payload", cause=inner) from inner
        except MalformedResponseError as exc:
            assert exc.code == "PROVIDER_MALFORMED_RESPONSE"
            assert isinstance(exc.__cause__, ValueError)

    def test_repr_is_diagnostic(self) -> None:
        exc = RateLimitError("slow down", context={"retry_after_s": 30})
        r = repr(exc)
        assert "RateLimitError" in r
        assert "PROVIDER_RATE_LIMITED" in r
        assert "slow down" in r


# ===========================================================================
# (4) Timeout / cancellation behavior
# ===========================================================================

class TestTimeoutAndCancellation:
    def test_cancellation_blocks_subsequent_calls(self) -> None:
        provider = MockProvider()
        req = LLMRequest(messages=[LLMMessage(role=MessageRole.USER, content="x")])
        provider.cancel()
        with pytest.raises(CancelledError):
            provider.generate(req)
        # uncancel and confirm it works again
        provider.uncancel()
        resp = provider.generate(req)
        assert resp.content == "<mock>x</mock>"

    def test_timeout_simulated_via_latency(self) -> None:
        """If the mock's latency exceeds the request timeout, raise
        TimeoutError_ (not a generic exception)."""
        provider = MockProvider(default_latency_s=2.0)
        req = LLMRequest(
            messages=[LLMMessage(role=MessageRole.USER, content="x")],
            timeout_s=0.5,
        )
        with pytest.raises(TimeoutError_) as info:
            provider.generate(req)
        assert info.value.code == "PROVIDER_TIMEOUT"

    def test_uncancel_after_cancel_resumes(self) -> None:
        provider = MockProvider()
        provider.cancel()
        provider.uncancel()
        stats = provider.statistics()
        assert stats["cancelled"] is False


# ===========================================================================
# (5) Mock provider
# ===========================================================================

class TestMockProvider:
    def test_default_responder_echoes_last_user_message(self) -> None:
        provider = MockProvider()
        req = LLMRequest(
            messages=[
                LLMMessage(role=MessageRole.SYSTEM, content="sys"),
                LLMMessage(role=MessageRole.USER, content="hello world"),
            ],
        )
        resp = provider.generate(req)
        assert resp.content == "<mock>hello world</mock>"
        assert resp.finish_reason == FinishReason.STOP
        assert resp.provider == "mock"
        assert resp.usage.total_tokens == resp.usage.prompt_tokens + resp.usage.completion_tokens

    def test_custom_responder_overrides_default(self) -> None:
        def responder(req: LLMRequest) -> LLMResponse:
            return LLMResponse(
                content="custom!",
                finish_reason=FinishReason.STOP,
                model="m",
                usage=LLMUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
                provider="mock",
            )
        provider = MockProvider(responder=responder)
        req = LLMRequest(messages=[LLMMessage(role=MessageRole.USER, content="ignored")])
        resp = provider.generate(req)
        assert resp.content == "custom!"

    def test_statistics_track_calls_and_errors(self) -> None:
        def boom(_req: LLMRequest) -> LLMResponse:
            raise ProviderError("nope")
        provider = MockProvider(responder=boom)
        req = LLMRequest(messages=[LLMMessage(role=MessageRole.USER, content="x")])
        with pytest.raises(ProviderError):
            provider.generate(req)
        stats = provider.statistics()
        assert stats["call_count"] == 1
        assert stats["error_count"] == 1

    def test_thread_safety(self) -> None:
        """100 concurrent calls must all complete deterministically."""
        provider = MockProvider()
        req = LLMRequest(messages=[LLMMessage(role=MessageRole.USER, content="x")])
        results: List[LLMResponse] = []
        errors: List[BaseException] = []

        def worker() -> None:
            try:
                results.append(provider.generate(req))
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors
        assert len(results) == 50
        stats = provider.statistics()
        assert stats["call_count"] == 50
        assert stats["error_count"] == 0

    def test_last_request_capture(self) -> None:
        provider = MockProvider()
        req = LLMRequest(
            messages=[LLMMessage(role=MessageRole.USER, content="hello")],
            model="my-model",
        )
        provider.generate(req)
        last = provider.last_request()
        assert last is not None
        assert last.model == "my-model"
        assert last.messages[0].content == "hello"


# ===========================================================================
# (6) Provider selection
# ===========================================================================

class TestProviderSelection:
    def test_default_selection_is_mock(self, base_config: OmnixConfig) -> None:
        provider = get_provider(base_config)
        assert isinstance(provider, MockProvider)
        assert provider.name == "mock"

    def test_env_var_overrides_config(
        self, base_config: OmnixConfig, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Register a sentinel provider so we can detect the env-var path.
        class SentinelProvider:
            name = "sentinel"

            def generate(self, request: LLMRequest) -> LLMResponse:
                return LLMResponse(
                    content="sentinel",
                    finish_reason=FinishReason.STOP,
                    model="m",
                    usage=LLMUsage(prompt_tokens=0, completion_tokens=0, total_tokens=0),
                    provider="sentinel",
                )

            def statistics(self) -> Dict[str, Any]:
                return {"name": self.name}

        register_provider("sentinel", lambda *, model: SentinelProvider())
        monkeypatch.setenv("OMNIX_LLM_PROVIDER", "sentinel")
        provider = get_provider(base_config)
        assert isinstance(provider, SentinelProvider)

    def test_config_extra_overrides_default(self, base_config: OmnixConfig) -> None:
        class FakeProvider:
            name = "fake"

            def generate(self, request: LLMRequest) -> LLMResponse:
                return LLMResponse(
                    content="fake",
                    finish_reason=FinishReason.STOP,
                    model="m",
                    usage=LLMUsage(prompt_tokens=0, completion_tokens=0, total_tokens=0),
                    provider="fake",
                )

            def statistics(self) -> Dict[str, Any]:
                return {"name": self.name}

        register_provider("fake", lambda *, model: FakeProvider())
        cfg = base_config.with_overrides(extra={"llm_provider": "fake"})
        provider = get_provider(cfg)
        assert isinstance(provider, FakeProvider)

    def test_register_provider_rejects_empty_name(self) -> None:
        with pytest.raises(ConfigurationError_):
            register_provider("", lambda *, model: MockProvider())

    def test_register_provider_rejects_non_callable(self) -> None:
        with pytest.raises(ConfigurationError_):
            register_provider("bogus", "not a factory")  # type: ignore[arg-type]


# ===========================================================================
# (7) Invalid configuration
# ===========================================================================

class TestInvalidConfiguration:
    def test_unknown_provider_name_raises_configuration_error(
        self, base_config: OmnixConfig,
    ) -> None:
        cfg = base_config.with_overrides(extra={"llm_provider": "no-such-thing"})
        with pytest.raises(ConfigurationError_) as info:
            get_provider(cfg)
        assert info.value.code == "PROVIDER_CONFIG_INVALID"
        # structured context should list the requested name and the
        # known providers so the Brain can branch.
        ctx = info.value.context
        assert ctx["requested"] == "no-such-thing"
        assert "mock" in ctx["available"]

    def test_blank_provider_name_in_config_falls_back_to_default(
        self, base_config: OmnixConfig,
    ) -> None:
        # empty string is not a valid name; should fall back to mock
        cfg = base_config.with_overrides(extra={"llm_provider": ""})
        provider = get_provider(cfg)
        assert isinstance(provider, MockProvider)


# ===========================================================================
# (8) Malformed provider response
# ===========================================================================

class TestMalformedResponse:
    def test_responder_returning_wrong_type_raises_malformed(
        self,
    ) -> None:
        def bad(req: LLMRequest) -> str:  # type: ignore[return-value]
            return "not an LLMResponse"

        provider = MockProvider(responder=bad)
        req = LLMRequest(messages=[LLMMessage(role=MessageRole.USER, content="x")])
        with pytest.raises(MalformedResponseError) as info:
            provider.generate(req)
        assert info.value.code == "PROVIDER_MALFORMED_RESPONSE"

    def test_responder_returning_empty_content_raises_malformed(
        self,
    ) -> None:
        def empty(req: LLMRequest) -> LLMResponse:
            return LLMResponse(
                content="",
                finish_reason=FinishReason.STOP,
                model="m",
                usage=LLMUsage(prompt_tokens=0, completion_tokens=0, total_tokens=0),
                provider="mock",
            )

        provider = MockProvider(responder=empty)
        req = LLMRequest(messages=[LLMMessage(role=MessageRole.USER, content="x")])
        with pytest.raises(MalformedResponseError):
            provider.generate(req)

    def test_responder_raising_unexpected_exception_is_wrapped(
        self,
    ) -> None:
        def explode(_req: LLMRequest) -> LLMResponse:
            raise RuntimeError("kaboom")

        provider = MockProvider(responder=explode)
        req = LLMRequest(messages=[LLMMessage(role=MessageRole.USER, content="x")])
        with pytest.raises(MalformedResponseError) as info:
            provider.generate(req)
        # The structured error exposes the original exception via its
        # own ``cause`` attribute (set by the provider boundary).
        assert isinstance(info.value.cause, RuntimeError)
        assert "kaboom" in str(info.value.cause)

    def test_responder_raising_provider_error_passes_through(
        self,
    ) -> None:
        def auth_fail(_req: LLMRequest) -> LLMResponse:
            raise AuthenticationError("bad key")

        provider = MockProvider(responder=auth_fail)
        req = LLMRequest(messages=[LLMMessage(role=MessageRole.USER, content="x")])
        with pytest.raises(AuthenticationError) as info:
            provider.generate(req)
        assert info.value.code == "PROVIDER_AUTH_FAILED"


# ===========================================================================
# (9) Provider cannot access Windows automation
#     — see tests/test_provider_isolation.py
# (10) Provider cannot directly execute capabilities
#     — see tests/test_provider_isolation.py
# ===========================================================================

# This file intentionally has no "(9)" and "(10)" classes.  Those
# guarantees are enforced by tests/test_provider_isolation.py, which
# uses static analysis to keep the provider layer free of automation
# imports and the capability execution path.
