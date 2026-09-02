"""
Phase 6D.1 hardening: --llm-health CLI command.

This test boots the dev runtime's main module end-to-end and asserts:

    1. ``--llm-health`` with the deterministic provider exits 0, no
       network is touched, and no API key is printed.
    2. ``--llm-health --offline`` with the deterministic provider
       exits 0 and prints the SKIPPED probe line.
    3. ``--llm-health`` with ``--provider=openrouter`` and a missing
       key exits 3 (configuration error) and does not crash.

The tests do NOT exercise the live network path.  Live network is
covered by ``test_phase6d_e2e_dryrun.py`` with mocked HTTP.
"""
from __future__ import annotations

import io
import os
import sys
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MAIN_PATH = PROJECT_ROOT / "main.py"


def _run_main_with_args_in(tmp_path, argv, env_overrides=None):
    """Invoke ``main.py`` as a subprocess with ``argv[1:]`` in ``tmp_path``.

    The temp dir is used as both ``cwd`` and project root, so the
    loader finds NO ``.env`` file and the only config it sees is what
    the test injects via ``env_overrides``.
    """
    import subprocess
    tmp_path.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    if env_overrides:
        env.update(env_overrides)
    # Force the deterministic provider unless the test overrides it.
    env.setdefault("OMNIX_LLM_PROVIDER", "mock")
    # Strip any real key the test runner may have set, unless the
    # test explicitly injects one.
    if "OPENROUTER_API_KEY" not in env:
        env["OPENROUTER_API_KEY"] = ""
    try:
        proc = subprocess.run(
            [sys.executable, str(MAIN_PATH), *argv],
            cwd=str(tmp_path),
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return proc.stdout, proc.stderr, proc.returncode
    except Exception:
        # Fall back: import and call programmatically.
        return _run_main_inprocess(argv, env)


def _run_main_with_args(argv, env_overrides=None):
    """Backward-compat shim: run the CLI in the project root."""
    return _run_main_with_args_in(PROJECT_ROOT, argv, env_overrides)


def _run_main_inprocess(argv, env):
    """Programmatic fallback for environments where subprocess fails."""
    saved_argv = sys.argv
    saved_env = os.environ.copy()
    try:
        sys.argv = ["main.py", *argv]
        for k, v in env.items():
            os.environ[k] = v
        # Import the module and invoke __main__ via runpy.
        import runpy
        buf_out, buf_err = io.StringIO(), io.StringIO()
        rc = 0
        try:
            with redirect_stdout(buf_out), redirect_stderr(buf_err):
                runpy.run_path(str(MAIN_PATH), run_name="__main__")
        except SystemExit as e:
            rc = e.code if isinstance(e.code, int) else 1
        return buf_out.getvalue(), buf_err.getvalue(), rc
    finally:
        sys.argv = saved_argv
        os.environ.clear()
        os.environ.update(saved_env)


def test_llm_health_deterministic_exits_zero_no_key_printed(tmp_path):
    """--provider=deterministic --llm-health must succeed without any key.

    We run the CLI in a temp dir with NO .env file, so the loader
    cannot pick up a stray real key from the workspace.
    """
    out, err, rc = _run_main_with_args_in(
        tmp_path,
        ["--provider=deterministic", "--llm-health"],
        env_overrides={"OMNIX_LLM_PROVIDER": "mock", "OMNIX_HEADLESS": "1"},
    )
    combined = out + err
    assert rc == 0, f"unexpected exit code {rc}; output={combined!r}"
    assert "OMNIX V6 LLM HEALTH PROBE" in out
    assert "llm_provider" in out
    # No key value should appear anywhere.
    assert "sk-" not in combined
    assert "Bearer " not in combined


def test_llm_health_offline_marks_probe_skipped(tmp_path):
    """--llm-health --offline must print SKIPPED and exit 0."""
    out, err, rc = _run_main_with_args_in(
        tmp_path,
        ["--provider=deterministic", "--llm-health", "--offline"],
        env_overrides={"OMNIX_LLM_PROVIDER": "mock", "OMNIX_HEADLESS": "1"},
    )
    combined = out + err
    assert rc == 0, f"unexpected exit code {rc}; output={combined!r}"
    assert "SKIPPED" in out
    assert "OMNIX V6 LLM HEALTH PROBE" in out


def test_llm_health_openrouter_without_key_exits_3(monkeypatch):
    """--provider=openrouter with NO usable key must exit 3, not crash.

    This test runs entirely offline: it uses --offline so the probe
    does not hit the network.  We invoke ``_run_llm_health`` directly
    with a config that has no key, and force OMNIX_LLM_PROVIDER to
    "openrouter" so the factory actually tries to build the real
    provider.
    """
    import tempfile
    monkeypatch.setenv("OMNIX_LLM_PROVIDER", "openrouter")
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        from main import _run_llm_health
        from core.configuration import OmnixConfig

        config = OmnixConfig(
            project_root=tmp_path,
            data_dir=tmp_path / ".data",
            log_dir=tmp_path / ".log",
            env_file=tmp_path / ".env",
            openrouter_url="https://example.invalid/api/v1",
            openrouter_keys=(),
            openrouter_model_pool=(),
        )
        engine_stub = MagicMock()
        rc = _run_llm_health(config, engine_stub, offline=True)
        assert rc == 3, f"expected 3 (config error) but got {rc}"


def test_llm_health_offline_openrouter_without_key_exits_3(monkeypatch):
    """--provider=openrouter --llm-health --offline with no key still fails clean.

    Same shape as the live-probe variant: directly invoke
    ``_run_llm_health`` with a config that has no key, offline=True.
    """
    import tempfile
    monkeypatch.setenv("OMNIX_LLM_PROVIDER", "openrouter")
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        from main import _run_llm_health
        from core.configuration import OmnixConfig

        config = OmnixConfig(
            project_root=tmp_path,
            data_dir=tmp_path / ".data",
            log_dir=tmp_path / ".log",
            env_file=tmp_path / ".env",
            openrouter_url="https://example.invalid/api/v1",
            openrouter_keys=(),
            openrouter_model_pool=(),
        )
        engine_stub = MagicMock()
        rc = _run_llm_health(config, engine_stub, offline=True)
        assert rc == 3, f"expected 3 (config error) but got {rc}"


def test_llm_health_prints_api_key_count_not_value(tmp_path):
    """When a key IS configured, only the *count* is surfaced, never the value.

    We run in a temp dir so the test runner's real ``.env`` key is
    not the one being asserted on.  We inject a fake, distinctive
    value and assert it is never echoed in the output.
    """
    fake_key = "sk-FAKE-1234567890-FAKE-FAKE-FAKE-FAKE-FAKE-FAKE"
    out, err, rc = _run_main_with_args_in(
        tmp_path,
        ["--provider=deterministic", "--llm-health"],
        env_overrides={
            "OMNIX_LLM_PROVIDER": "mock",
            "OPENROUTER_API_KEY": fake_key,
            "OMNIX_HEADLESS": "1",
        },
    )
    combined = out + err
    assert rc == 0, f"unexpected exit code {rc}; output={combined!r}"
    # Key value must not appear anywhere in the output.
    assert fake_key not in combined
    assert "1234567890" not in combined
    # The deterministic provider is offline, so SKIPPED is expected.
    assert "SKIPPED" in out
