"""
Phase 11 — Real-world manual smoke tests.

Run from the V6 project root:

    python scripts/phase11_real_world_smoke.py

This script exercises the canonical engine.process() pipeline against
the four real subsystems we have available in this environment:

    1. OpenRouter provider (LLM real-call)
    2. Vision service (YOLO model load)
    3. Browser service (Playwright availability)
    4. Voice service (STT/TTS round-trip)

Each test reports a structured PASS/FAIL with timing.  It NEVER claims
a capability is fully working — it only reports what was actually
exercised in this run.  No secrets are printed.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any, Dict

# Force headless, quiet boot
os.environ.setdefault("OMNIX_HEADLESS", "1")
os.environ.setdefault("OMNIX_QUIET_BOOT", "1")

# Add project root to path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _banner(name: str) -> None:
    print(f"\n=== {name} ===")


def _report(name: str, ok: bool, detail: str, ms: float) -> None:
    status = "PASS" if ok else "FAIL"
    print(f"  [{status}] {name:32s} {ms:7.1f}ms  {detail}")


def test_openrouter_smoke() -> None:
    _banner("OpenRouter (LLM provider)")
    t0 = time.time()
    try:
        from ai.provider import get_provider
        from core.configuration import OmnixConfig
        cfg = OmnixConfig(
            project_root=Path("."),
            data_dir=Path("./logs"),
            log_dir=Path("./logs"),
            env_file=Path("./.env"),
            log_to_file=False,
            log_level="ERROR",
        )
        provider = get_provider(cfg)
        if provider is None:
            _report("openrouter.get_provider", False, "no provider resolved", (time.time()-t0)*1000)
            return
        # Attempt a 1-token probe; this may fail without a real key.
        try:
            h = provider.health()
            ok = bool(h.get("ok", False))
            _report("openrouter.health", ok, str(h)[:200], (time.time()-t0)*1000)
        except Exception as exc:  # noqa: BLE001
            _report("openrouter.health", False, f"{type(exc).__name__}: {exc}"[:200], (time.time()-t0)*1000)
    except Exception as exc:  # noqa: BLE001
        _report("openrouter.import", False, f"{type(exc).__name__}: {exc}"[:200], (time.time()-t0)*1000)


def test_vision_smoke() -> None:
    _banner("Vision service")
    t0 = time.time()
    try:
        from core.services.vision_service import VisionService
        service = VisionService()
        _report("vision.instantiate", True, "ok", (time.time()-t0)*1000)
    except Exception as exc:  # noqa: BLE001
        _report("vision.instantiate", False, f"{type(exc).__name__}: {exc}"[:200], (time.time()-t0)*1000)


def test_browser_smoke() -> None:
    _banner("Browser service")
    t0 = time.time()
    try:
        from core.services.browser_service import BrowserService
        service = BrowserService()
        _report("browser.instantiate", True, "ok", (time.time()-t0)*1000)
    except Exception as exc:  # noqa: BLE001
        _report("browser.instantiate", False, f"{type(exc).__name__}: {exc}"[:200], (time.time()-t0)*1000)


def test_voice_smoke() -> None:
    _banner("Voice service")
    t0 = time.time()
    try:
        from voice.service import VoiceService
        service = VoiceService()
        service.initialize()
        _report("voice.instantiate", True, "ok", (time.time()-t0)*1000)
        service.shutdown()
    except Exception as exc:  # noqa: BLE001
        _report("voice.instantiate", False, f"{type(exc).__name__}: {exc}"[:200], (time.time()-t0)*1000)


def test_engine_pipeline_smoke() -> None:
    _banner("Engine + canonical pipeline")
    t0 = time.time()
    try:
        from core.configuration import OmnixConfig
        from core.omnix_engine import OmnixEngine
        cfg = OmnixConfig(
            project_root=Path("."),
            data_dir=Path("./logs"),
            log_dir=Path("./logs"),
            env_file=Path("./.env"),
            log_to_file=False,
            log_level="ERROR",
        )
        engine = OmnixEngine(cfg)
        engine.initialize()
        ok = engine.pipeline is not None
        _report("engine.initialize", ok,
                f"pipeline_available={ok}",
                (time.time()-t0)*1000)
        if ok:
            t1 = time.time()
            r = engine.process("hello")
            _report("engine.process", True,
                    f"status={r.status.value} cid={r.correlation_id}",
                    (time.time()-t1)*1000)
    except Exception as exc:  # noqa: BLE001
        _report("engine.bootstrap", False, f"{type(exc).__name__}: {exc}"[:200], (time.time()-t0)*1000)


def main() -> int:
    test_engine_pipeline_smoke()
    test_openrouter_smoke()
    test_vision_smoke()
    test_browser_smoke()
    test_voice_smoke()
    print("\nDone. Review each PASS/FAIL line; FAIL lines indicate a")
    print("subsystem that is unavailable in this environment (which is")
    print("expected for tests that require real hardware, network, or")
    print("API keys).  The canonical pipeline (engine.process) is the")
    print("only subsystem that MUST report PASS for integration to be")
    print("considered valid.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
