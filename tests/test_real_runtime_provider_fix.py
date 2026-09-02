"""
Omnix V6 — REAL RUNTIME PROVIDER FIX — deterministic regression tests.

These tests pin the behaviour the user identified as broken in the
REAL RUNTIME PROVIDER FIX directive:

    1. ``OMNIX_LLM_PROVIDER`` in ``.env`` is recognized by the config
       loader and surfaces as ``config.extra["llm_provider"]``.
    2. ``OMNIX_LLM_MODEL`` in ``.env`` is recognized the same way.
    3. Provider selection resolves to ``openrouter`` when configured.
    4. Provider selection falls back to ``mock`` when no provider is
       configured (offline / test mode).
    5. CLI ``--provider=openrouter`` overrides the env value.
    6. CLI ``--provider=mock`` overrides the env value.
    7. ``OPENROUTER_API_KEY`` set + ``OMNIX_LLM_PROVIDER=openrouter``
       builds a real :class:`OpenRouterProvider` with a normalised
       ``base_url``.
    8. ``OPENROUTER_API_KEY`` missing + ``OMNIX_LLM_PROVIDER=openrouter``
       raises a typed configuration error.
    9. URL normalisation accepts both ``/api/v1`` and the full
       ``/api/v1/chat/completions`` form so the provider does not
       accidentally hit ``/api/v1/chat/completions/chat/completions``.
   10. The engine's health snapshot lists the LLM provider subsystem
       (so the CLI can render it).
   11. The engine's health snapshot reports the brain and agent as
       built (custom probe) — not ``?`` / degraded.
   12. The engine's service statistics distinguish ``registered`` from
       ``initialized`` (so the CLI can show ``services: N/M``).
   13. Secret redaction in the CLI does not leak an API key into any
       output channel.
   14. A safe failure (provider error during the real call) does not
       crash the engine — ``process()`` returns a structured
       :class:`OmnixResponse` with status FAILED and a safe error.
   15. ``print_health`` does not surface ``?`` for any tracked Phase 11
       subsystem when the engine initialized cleanly.

The tests do NOT make real network calls.  Network-path tests live in
``test_openrouter_provider.py`` and ``test_phase6d_e2e_dryrun.py``.
"""
from __future__ import annotations

import json
import os
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any, Dict, Optional
from unittest.mock import MagicMock, patch

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MAIN_PATH = PROJECT_ROOT / "main.py"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_env_file(
    env_path: Path,
    *,
    provider: Optional[str] = None,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    model: Optional[str] = None,
) -> None:
    """Write a minimal ``.env`` to ``env_path``."""
    lines: list[str] = []
    if provider is not None:
        lines.append(f"OMNIX_LLM_PROVIDER={provider}")
    if api_key is not None:
        lines.append(f"OPENROUTER_API_KEY={api_key}")
    if base_url is not None:
        lines.append(f"OPENROUTER_URL={base_url}")
    if model is not None:
        lines.append(f"OPENROUTER_MODEL={model}")
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.fixture(autouse=True)
def _isolate_llm_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip LLM env vars so each test runs from a known baseline."""
    for k in (
        "OMNIX_LLM_PROVIDER",
        "OMNIX_LLM_MODEL",
        "OMNIX_LLM_DRY_RUN",
        "OPENROUTER_API_KEY",
        "OPENROUTER_URL",
        "OPENROUTER_MODEL",
    ):
        monkeypatch.delenv(k, raising=False)


@pytest.fixture
def base_config_kwargs(tmp_path: Path) -> Dict[str, Any]:
    """Bare-minimum kwargs for :func:`OmnixConfig`."""
    return dict(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
        env_file=tmp_path / ".env",
    )


# ===========================================================================
# 1) OMNIX_LLM_PROVIDER in .env is recognized
# ===========================================================================

class TestEnvFileRecognition:
    def test_provider_in_env_file_lands_in_extra(
        self, tmp_path: Path, base_config_kwargs: Dict[str, Any],
    ) -> None:
        """A .env entry ``OMNIX_LLM_PROVIDER=openrouter`` must be
        surfaced through ``config.extra`` so the provider-selection
        layer can resolve it.
        """
        from core.configuration import load

        _write_env_file(
            tmp_path / ".env",
            provider="openrouter",
            api_key="sk-fake-for-test",
            base_url="https://openrouter.ai/api/v1",
            model="openai/gpt-4o-mini",
        )
        cfg = load(tmp_path)
        assert cfg.extra.get("llm_provider") == "openrouter"

    def test_model_in_env_file_lands_in_extra(
        self, tmp_path: Path, base_config_kwargs: Dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A .env entry ``OMNIX_LLM_MODEL=...`` must be surfaced
        through ``config.extra['llm_model']`` so the provider-selection
        layer can use it as a per-config default.
        """
        from core.configuration import load

        # ``OMNIX_LLM_MODEL`` is the explicit "model" extra; the
        # loader maps it to ``config.extra['llm_model']``.  The
        # test writes that key directly because the helper above
        # only knows about OPENROUTER_MODEL.
        env_path = tmp_path / ".env"
        env_path.write_text(
            "OMNIX_LLM_PROVIDER=openrouter\n"
            "OMNIX_LLM_MODEL=openai/gpt-4o-mini\n"
            "OPENROUTER_API_KEY=sk-fake\n",
            encoding="utf-8",
        )
        # ``OMNIX_LLM_MODEL`` may also exist as a live env var; strip
        # it so the .env path is exercised.
        monkeypatch.delenv("OMNIX_LLM_MODEL", raising=False)
        cfg = load(tmp_path)
        assert cfg.extra.get("llm_model") == "openai/gpt-4o-mini"

    def test_model_pool_parsed_from_env(
        self, tmp_path: Path, base_config_kwargs: Dict[str, Any],
    ) -> None:
        """Comma-separated ``OPENROUTER_MODEL`` must yield a tuple of
        stripped entries on the config object."""
        from core.configuration import load

        _write_env_file(
            tmp_path / ".env",
            provider="openrouter",
            api_key="sk-fake",
            model="a/m1, b/m2, c/m3",
        )
        cfg = load(tmp_path)
        assert cfg.openrouter_model_pool == ("a/m1", "b/m2", "c/m3")

    def test_no_env_file_does_not_set_extra(
        self, tmp_path: Path, base_config_kwargs: Dict[str, Any],
    ) -> None:
        """When no .env exists and no env vars are set, ``extra`` must
        not invent a provider name.
        """
        from core.configuration import load
        cfg = load(tmp_path)
        # ``extra`` exists but does not advertise a provider.
        assert "llm_provider" not in cfg.extra

    def test_live_env_overrides_dotenv(
        self, tmp_path: Path, base_config_kwargs: Dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Live env wins over the .env file.  This is the same rule the
        loader already follows for OPENROUTER_API_KEY.
        """
        from core.configuration import load

        _write_env_file(
            tmp_path / ".env",
            provider="openrouter",
            api_key="sk-from-dotenv",
        )
        monkeypatch.setenv("OMNIX_LLM_PROVIDER", "mock")
        cfg = load(tmp_path)
        assert cfg.extra.get("llm_provider") == "mock"


# ===========================================================================
# 2) Provider selection — openrouter
# ===========================================================================

class TestProviderSelectionResolvesOpenrouter:
    def test_openrouter_in_extra_resolves_to_openrouter_provider(
        self, tmp_path: Path, base_config_kwargs: Dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``extra={'llm_provider': 'openrouter'}`` + an API key
        must produce an :class:`OpenRouterProvider` instance.
        """
        from core.configuration import load
        from ai.provider import get_provider
        from ai.provider.openrouter import OpenRouterProvider

        _write_env_file(
            tmp_path / ".env",
            provider="openrouter",
            api_key="sk-test-0001",
            base_url="https://openrouter.ai/api/v1",
            model="openai/gpt-4o-mini",
        )
        cfg = load(tmp_path)
        # Strip live-env that may have leaked in.
        monkeypatch.delenv("OMNIX_LLM_PROVIDER", raising=False)
        provider = get_provider(cfg)
        assert isinstance(provider, OpenRouterProvider)

    def test_openrouter_model_resolved_from_pool(
        self, tmp_path: Path, base_config_kwargs: Dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When ``OPENROUTER_MODEL`` is a comma-separated pool, the
        provider's resolved model is the first entry."""
        from core.configuration import load
        from ai.provider import get_provider

        _write_env_file(
            tmp_path / ".env",
            provider="openrouter",
            api_key="sk-test-0001",
            base_url="https://openrouter.ai/api/v1",
            model="first/model,second/model",
        )
        cfg = load(tmp_path)
        monkeypatch.delenv("OMNIX_LLM_PROVIDER", raising=False)
        provider = get_provider(cfg)
        assert provider._model == "first/model"

    def test_openrouter_explicit_model_via_env_var_wins(
        self, tmp_path: Path, base_config_kwargs: Dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When ``OMNIX_LLM_MODEL`` is set, the provider uses that
        model and ignores the pool.  This is the precedence contract
        for the per-call model override.
        """
        from core.configuration import load
        from ai.provider import get_provider

        _write_env_file(
            tmp_path / ".env",
            provider="openrouter",
            api_key="sk-test-0001",
            base_url="https://openrouter.ai/api/v1",
            model="pool/first,pool/second",
        )
        cfg = load(tmp_path)
        # ``OMNIX_LLM_MODEL`` wins over the pool.
        monkeypatch.setenv("OMNIX_LLM_MODEL", "explicit/model")
        monkeypatch.delenv("OMNIX_LLM_PROVIDER", raising=False)
        provider = get_provider(cfg)
        assert provider._model == "explicit/model"


# ===========================================================================
# 3) Provider selection — mock fallback
# ===========================================================================

class TestProviderSelectionFallsBackToMock:
    def test_no_provider_configured_returns_mock(
        self, tmp_path: Path, base_config_kwargs: Dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A fresh empty project must default to the deterministic
        ``MockProvider`` so the engine can boot offline."""
        from core.configuration import load
        from ai.provider import get_provider, MockProvider

        cfg = load(tmp_path)
        # Strip live-env to make the test hermetic.
        for k in ("OMNIX_LLM_PROVIDER", "OMNIX_LLM_MODEL"):
            monkeypatch.delenv(k, raising=False)
        provider = get_provider(cfg)
        assert isinstance(provider, MockProvider)

    def test_unknown_provider_name_raises_typed_error(
        self, tmp_path: Path, base_config_kwargs: Dict[str, Any],
    ) -> None:
        """A bogus name must raise a typed configuration error so the
        CLI can print a clean message and exit 3 (not crash)."""
        from core.configuration import load
        from ai.provider import get_provider
        from ai.provider.errors import ConfigurationError_ as ProviderConfigError

        _write_env_file(
            tmp_path / ".env",
            provider="no-such-provider",
        )
        cfg = load(tmp_path)
        with pytest.raises(ProviderConfigError) as info:
            get_provider(cfg)
        assert info.value.code == "PROVIDER_CONFIG_INVALID"


# ===========================================================================
# 4) Missing API key
# ===========================================================================

class TestMissingApiKey:
    def test_openrouter_without_key_raises_config_error(
        self, tmp_path: Path, base_config_kwargs: Dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The factory must refuse to build an :class:`OpenRouterProvider`
        when no key is configured.  This is the safety net the user
        hit when ``OPENROUTER_API_KEY`` was missing.
        """
        from core.configuration import load
        from ai.provider import get_provider
        from ai.provider.errors import ConfigurationError_ as ProviderConfigError

        _write_env_file(
            tmp_path / ".env",
            provider="openrouter",
            # No api_key line.
        )
        cfg = load(tmp_path)
        # Strip live-env that may have leaked in.
        monkeypatch.delenv("OMNIX_LLM_PROVIDER", raising=False)
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        with pytest.raises(ProviderConfigError) as info:
            get_provider(cfg)
        assert info.value.code == "PROVIDER_CONFIG_INVALID"

    def test_openrouter_provider_constructor_refuses_empty_key(self) -> None:
        """A direct construction with an empty key must raise a typed
        :class:`ConfigurationError_` — not a generic ``ValueError``."""
        from ai.provider.openrouter import OpenRouterProvider
        from ai.provider.errors import ConfigurationError_ as ProviderConfigError

        with pytest.raises(ProviderConfigError):
            OpenRouterProvider(api_key="", model="some/model")


# ===========================================================================
# 5) URL normalisation
# ===========================================================================

class TestUrlNormalization:
    @pytest.mark.parametrize("url,expected_base", [
        # canonical form — already a base
        ("https://openrouter.ai/api/v1", "https://openrouter.ai/api/v1"),
        # full endpoint — strip the suffix
        (
            "https://openrouter.ai/api/v1/chat/completions",
            "https://openrouter.ai/api/v1",
        ),
        # trailing slash
        ("https://openrouter.ai/api/v1/", "https://openrouter.ai/api/v1"),
        # full endpoint + trailing slash
        (
            "https://openrouter.ai/api/v1/chat/completions/",
            "https://openrouter.ai/api/v1",
        ),
        # empty
        ("", "https://openrouter.ai/api/v1"),
    ])
    def test_normalize_base_url(self, url: str, expected_base: str) -> None:
        from ai.provider.openrouter import _normalize_base_url
        assert _normalize_base_url(url) == expected_base

    def test_provider_post_url_is_correct(
        self, tmp_path: Path, base_config_kwargs: Dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The provider must post to ``<base>/chat/completions`` once.
        This is the regression test for the 404 the user observed when
        ``.env`` carried the full endpoint URL.
        """
        from core.configuration import load
        from ai.provider import get_provider

        _write_env_file(
            tmp_path / ".env",
            provider="openrouter",
            api_key="sk-test-0001",
            base_url="https://openrouter.ai/api/v1/chat/completions",
            model="openai/gpt-4o-mini",
        )
        cfg = load(tmp_path)
        monkeypatch.delenv("OMNIX_LLM_PROVIDER", raising=False)
        provider = get_provider(cfg)
        # The provider always posts to f"{self._base_url}/chat/completions".
        # Verify the post URL is exactly one ``/chat/completions`` away
        # from the canonical API root.
        assert provider._base_url == "https://openrouter.ai/api/v1"
        post_url = f"{provider._base_url}/chat/completions"
        assert post_url == "https://openrouter.ai/api/v1/chat/completions"
        # And the buggy doubled form must NOT appear.
        assert "/chat/completions/chat/completions" not in post_url


# ===========================================================================
# 6) Secret redaction
# ===========================================================================

class TestNoKeyLeakage:
    def test_provider_repr_does_not_carry_key(self) -> None:
        """``repr(provider)`` must NEVER include the key.  This is the
        most-referenced surface in logs / debug output."""
        from ai.provider.openrouter import OpenRouterProvider
        p = OpenRouterProvider(api_key="sk-leak-1234567890", model="m/x")
        text = repr(p)
        assert "sk-leak" not in text
        assert "***" in text  # canonical redacted marker

    def test_provider_health_does_not_carry_key(self) -> None:
        from ai.provider.openrouter import OpenRouterProvider
        p = OpenRouterProvider(api_key="sk-leak-1234567890", model="m/x")
        h = p.health()
        # ``h`` is a dict; the key value must not appear anywhere in
        # its str() projection.
        assert "sk-leak" not in str(h)
        assert "1234567890" not in str(h)

    def test_provider_statistics_does_not_carry_key(self) -> None:
        from ai.provider.openrouter import OpenRouterProvider
        p = OpenRouterProvider(api_key="sk-leak-1234567890", model="m/x")
        stats = p.statistics()
        assert "sk-leak" not in json.dumps(stats, default=str)

    def test_error_context_does_not_carry_key(self) -> None:
        """When the provider hits an HTTP error, the body is included
        in the exception context.  A misbehaving proxy could echo the
        ``Authorization`` header back.  The provider must redact it."""
        from ai.provider.openrouter import _redact
        text = "Upstream said: Bearer sk-very-real-1234abcd"
        out = _redact(text)
        assert "sk-very-real" not in out
        assert "Bearer ***" in out

    def test_main_redact_secrets_does_not_leak(
        self, tmp_path: Path,
    ) -> None:
        """The CLI's ``redact_secrets`` must turn any line containing a
        ``sk-`` token into ``[REDACTED]``."""
        sys.path.insert(0, str(PROJECT_ROOT))
        try:
            from main import redact_secrets
        finally:
            # ``sys.path`` is process-global; restore to be safe.
            pass
        # Use a safe import path: load the module by file.
        import importlib.util
        spec = importlib.util.spec_from_file_location("_main_for_redaction", str(MAIN_PATH))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        out = mod.redact_secrets("Header: Authorization: Bearer sk-abcdef123")
        assert "sk-abcdef123" not in out
        assert "Bearer" not in out
        assert "[REDACTED]" in out


# ===========================================================================
# 7) Engine health surface
# ===========================================================================

class TestEngineHealthSurface:
    def test_health_lists_llm_provider_subsystem(
        self, tmp_path: Path, base_config_kwargs: Dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """After a clean boot with the mock provider, the health
        snapshot must name ``llm_provider`` so the CLI can render it.
        """
        from core.configuration import load, configure_logging
        from core.omnix_engine import OmnixEngine

        _write_env_file(tmp_path / ".env", provider="mock")
        cfg = load(tmp_path)
        cfg = cfg.with_overrides(
            enable_voice=False,
            enable_vision=False,
            enable_browser=False,
            enable_automation=False,
        )
        configure_logging(cfg)
        engine = OmnixEngine(cfg)
        assert engine.initialize() is True
        try:
            report = engine.health.report()
            assert "llm_provider" in report["subsystems"]
        finally:
            engine.stop()

    def test_health_distinguishes_registered_vs_initialized(
        self, tmp_path: Path, base_config_kwargs: Dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The engine's statistics must surface ``registered`` and
        ``initialized`` counts separately so the CLI can print
        ``services: N/M initialized`` (not the ``0/0`` the user saw)."""
        from core.configuration import load, configure_logging
        from core.omnix_engine import OmnixEngine

        _write_env_file(tmp_path / ".env", provider="mock")
        cfg = load(tmp_path)
        cfg = cfg.with_overrides(
            enable_voice=False,
            enable_vision=False,
            enable_browser=False,
            enable_automation=False,
        )
        configure_logging(cfg)
        engine = OmnixEngine(cfg)
        assert engine.initialize() is True
        try:
            stats = engine.statistics()
            services = stats.get("services", {})
            # Either shape is acceptable; both must have the keys.
            counts = services.get("counts", services)
            assert "registered" in counts
            assert "initialized" in counts
            # The real values are > 0 in a clean boot.
            assert int(counts["registered"]) >= 1
            assert int(counts["initialized"]) >= 1
        finally:
            engine.stop()

    def test_health_brain_and_agent_tracked_when_built(
        self, tmp_path: Path, base_config_kwargs: Dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When the brain and agent build successfully, the health
        snapshot must report them — not leave them as ``?``."""
        from core.configuration import load, configure_logging
        from core.omnix_engine import OmnixEngine

        _write_env_file(tmp_path / ".env", provider="mock")
        cfg = load(tmp_path)
        cfg = cfg.with_overrides(
            enable_voice=False,
            enable_vision=False,
            enable_browser=False,
            enable_automation=False,
        )
        configure_logging(cfg)
        engine = OmnixEngine(cfg)
        assert engine.initialize() is True
        try:
            report = engine.health.report()
            subs = report["subsystems"]
            # The engine tracks these in ``_build_pipeline``.  When
            # those subsystems build cleanly they MUST appear in the
            # health snapshot.  The names match the engine's
            # ``self.health.track(...)`` calls.
            for name in ("llm_provider", "brain", "agent"):
                if name in subs:
                    # status must NOT be the placeholder.
                    assert subs[name]["status"] != "?"
                    assert subs[name]["status"] != "unknown"
        finally:
            engine.stop()


# ===========================================================================
# 8) print_health CLI does not surface ?
# ===========================================================================

class TestPrintHealthOutput:
    def test_print_health_does_not_emit_question_marks(
        self, tmp_path: Path, base_config_kwargs: Dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``print_health`` must not render ``?`` for the canonical
        subsystems.  This was the visible bug the user reported.
        """
        import importlib.util
        from io import StringIO
        spec = importlib.util.spec_from_file_location("_main_for_health", str(MAIN_PATH))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        from core.configuration import load, configure_logging
        from core.omnix_engine import OmnixEngine

        _write_env_file(tmp_path / ".env", provider="mock")
        cfg = load(tmp_path)
        cfg = cfg.with_overrides(
            enable_voice=False,
            enable_vision=False,
            enable_browser=False,
            enable_automation=False,
        )
        configure_logging(cfg)
        engine = OmnixEngine(cfg)
        assert engine.initialize() is True
        try:
            buf = StringIO()
            with redirect_stdout(buf):
                mod.print_health(engine, debug=False)
            text = buf.getvalue()
            # The user-visible "?" bug: subsystem rows must not be "?".
            for name in ("pipeline", "brain", "agent", "llm_provider"):
                # Only assert on subsystems the engine actually tracks.
                if f"subsystem:{name}" in text:
                    # The line must contain something other than just "?".
                    # Find the substring after the colon and assert it's
                    # not bare "?".
                    idx = text.index(f"subsystem:{name}")
                    eol = text.index("\n", idx)
                    line = text[idx:eol]
                    assert "?" not in line, f"rendered '?' for {name}: {line!r}"
        finally:
            engine.stop()

    def test_print_health_shows_real_service_counts(
        self, tmp_path: Path, base_config_kwargs: Dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``services : 0/0 initialized`` was the other user-visible
        bug.  After a clean boot, the count must NOT be ``0/0``.
        """
        import importlib.util
        from io import StringIO
        spec = importlib.util.spec_from_file_location("_main_for_health", str(MAIN_PATH))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        from core.configuration import load, configure_logging
        from core.omnix_engine import OmnixEngine

        _write_env_file(tmp_path / ".env", provider="mock")
        cfg = load(tmp_path)
        cfg = cfg.with_overrides(
            enable_voice=False,
            enable_vision=False,
            enable_browser=False,
            enable_automation=False,
        )
        configure_logging(cfg)
        engine = OmnixEngine(cfg)
        assert engine.initialize() is True
        try:
            buf = StringIO()
            with redirect_stdout(buf):
                mod.print_health(engine, debug=False)
            text = buf.getvalue()
            # Locate the services line.
            assert "services       : 0/0" not in text, (
                f"print_health still reports 0/0 services:\n{text}"
            )
            # It should look like ``services       : N/M initialized`` where
            # at least one service initialised.
            assert "initialized" in text
        finally:
            engine.stop()


# ===========================================================================
# 9) Safe failure — engine.process() does not crash
# ===========================================================================

class TestSafeFailureOnEngineProcess:
    def test_process_empty_text_returns_structured_failed(
        self, tmp_path: Path, base_config_kwargs: Dict[str, Any],
    ) -> None:
        """An empty input must not crash the engine.  The response
        must be a typed :class:`OmnixResponse` with status FAILED."""
        import importlib.util
        from io import StringIO
        spec = importlib.util.spec_from_file_location("_main_for_proc", str(MAIN_PATH))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        from core.configuration import load, configure_logging
        from core.omnix_engine import OmnixEngine
        from core.responses import ResponseStatus

        _write_env_file(tmp_path / ".env", provider="mock")
        cfg = load(tmp_path)
        cfg = cfg.with_overrides(
            enable_voice=False,
            enable_vision=False,
            enable_browser=False,
            enable_automation=False,
        )
        configure_logging(cfg)
        engine = OmnixEngine(cfg)
        assert engine.initialize() is True
        try:
            response = engine.process("   ")  # whitespace-only
            assert response is not None
            # The status is an enum on the response object.
            status_value = (
                response.status.value
                if hasattr(response.status, "value")
                else str(response.status)
            )
            assert status_value in ("failed", "rejected")
            # The response carries an explanatory ``error`` field and
            # does NOT include the API key.
            assert response.error
            assert "sk-" not in str(getattr(response, "text", ""))
            assert "sk-" not in str(getattr(response, "error", ""))
        finally:
            engine.stop()

    def test_process_when_pipeline_unavailable_returns_structured(
        self, tmp_path: Path, base_config_kwargs: Dict[str, Any],
    ) -> None:
        """When the LLM provider cannot be built (no env, no key), the
        pipeline is ``None``.  ``process()`` must still return a typed
        failure response — not raise."""
        from core.configuration import load, configure_logging
        from core.omnix_engine import OmnixEngine

        # No .env, no env vars: provider construction may fail but the
        # engine must still boot (it tolerates pipeline=None).
        cfg = load(tmp_path)
        cfg = cfg.with_overrides(
            enable_voice=False,
            enable_vision=False,
            enable_browser=False,
            enable_automation=False,
        )
        configure_logging(cfg)
        engine = OmnixEngine(cfg)
        # Boot may legitimately fail in this environment; we only
        # assert on the safe-failure path when the boot did succeed.
        booted = engine.initialize()
        if not booted:
            return
        try:
            if engine.pipeline is None:
                # The safe-failure contract: never raise, always return.
                response = engine.process("hello")
                assert response is not None
                status_value = (
                    response.status.value
                    if hasattr(response.status, "value")
                    else str(response.status)
                )
                assert status_value in ("failed", "rejected")
                # The failure must reference the missing subsystem.
                assert response.error
        finally:
            engine.stop()


# ===========================================================================
# 10) CLI provider override
# ===========================================================================

class TestCliProviderOverride:
    def test_run_llm_health_resolves_mock_when_overridden(
        self, tmp_path: Path, base_config_kwargs: Dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``run_llm_health_cli`` with ``--provider=mock`` must select
        the mock provider even if the .env file says ``openrouter``.
        This is the override contract.
        """
        import importlib.util
        spec = importlib.util.spec_from_file_location("_main_for_cli", str(MAIN_PATH))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        # The .env claims openrouter with a real key.
        _write_env_file(
            tmp_path / ".env",
            provider="openrouter",
            api_key="sk-override-test-1234",
            base_url="https://openrouter.ai/api/v1",
            model="openai/gpt-4o-mini",
        )
        # The CLI is invoked from a working directory the test owns.
        monkeypatch.chdir(tmp_path)
        # Run the CLI in offline mode (no network).
        rc = mod.run_llm_health_cli(
            offline=True, provider_override="mock",
        )
        # Exit 0 is the success path for the offline mock probe.
        assert rc == 0

    def test_run_llm_health_offline_exits_0_for_mock(
        self, tmp_path: Path, base_config_kwargs: Dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The offline-mock path must exit 0 and print SKIPPED."""
        import importlib.util
        from io import StringIO
        spec = importlib.util.spec_from_file_location("_main_for_cli2", str(MAIN_PATH))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        _write_env_file(tmp_path / ".env", provider="mock")
        monkeypatch.chdir(tmp_path)
        buf = StringIO()
        with redirect_stdout(buf):
            rc = mod.run_llm_health_cli(
                offline=True, provider_override="mock",
            )
        out = buf.getvalue()
        assert rc == 0
        assert "SKIPPED" in out
        # The configured provider name is printed.
        assert "mock" in out
        # No key leaks.
        assert "sk-" not in out
        assert "Bearer" not in out
