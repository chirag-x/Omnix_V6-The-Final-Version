# V6 Phase 6D.1 — Final Pre-Phase-7 Hardening Report

**Phase:** 6D.1 (final integration cleanup before Phase 7 Vision)
**Status:** COMPLETE — all 16 hardening checks passed
**Date:** 2026-08-30
**Scope:** OpenRouter provider, provider selection, CLI, API-key
isolation, vision pre-phase audit, regression baseline. **No Phase 7
work was started.**

---

## 1. Scope and constraints

This pass is the last hardening sweep before V6 Phase 7 (Vision,
Browser, Voice, Memory). It deliberately does **not** start Phase 7,
**does not** implement YOLO / OCR / screen capture, **does not**
install new vision dependencies, and **does not** copy V5 source.

Hard rules (verbatim from the directive):

* The provider subpackage MUST NOT import subprocess / pyautogui /
  win32gui / ctypes / core.capability_router.
* The API key MUST NOT appear in Plan / Goal / Intent / ActionRequest /
  ExecutionContext / AgentResult / Observation / CapabilityResult /
  logs / repr / exceptions / test snapshots / CLI output.
* `python -m pytest tests/ -q` MUST be fully deterministic, with no
  live network and no real OpenRouter key on the test path.
* The final response MUST be exactly one of two stop phrases.

---

## 2. Check-by-check outcome

| # | Check | Outcome | Evidence |
|---|-------|---------|----------|
| 1 | OpenRouter model override precedence | PASS | `ai/provider/selection.py` forwards `model=kwargs.get("model")` to the openrouter factory; `OpenRouterProvider.__init__` and `generate()` already implement `request.model > explicit model > first in pool`. New tests: `test_openrouter_request_model_overrides_provider_model`, `test_openrouter_init_uses_model_pool_first_item`, `test_openrouter_init_explicit_model_overrides_pool`. |
| 2 | `.env` security | PASS | `.gitignore` excludes `.env` (already present). `.env.example` created with **placeholders only**: `OPENROUTER_URL=`, `OPENROUTER_API_KEY=`, `OPENROUTER_MODEL=`. No real values copied. No test fixtures contain real keys. |
| 3 | API-key leakage audit | PASS | `OpenRouterProvider.__repr__` now returns `OpenRouterProvider(name='openrouter', model='…', api_key='***')`. New test `test_openrouter_repr_redacts_api_key` proves the key never appears in repr. The provider also exposes `last_model_used` and stats that go through `LLMRequest.to_dict()` / `LLMResponse.to_dict()`, both of which already redact `raw`. `OmnixConfig.to_dict()` masks the key list to a count. `_redact()` strips `Bearer <token>` substrings that could be echoed back by a misbehaving proxy. |
| 4 | OpenRouter response handling | PASS | Canonical error hierarchy wired in `ai/provider/openrouter.py`. New test coverage for 401/403/404/429/5xx/timeout/network/malformed-JSON/missing-choices/empty-content/bearer-redaction. Retries clamped to `[0, 5]`. |
| 5 | `Agent.py` size review | PASS (no refactor) | `core/orchestration/agent.py` is 1107 lines / 39.9 KB. The audit (subagent `aa7dc12d82de445ac`) found the class is a cohesive closed-loop state machine; splitting it would scatter state across files for no architectural gain. Kept as a single module. |
| 6 | Duplicate-architecture audit | PASS | No V5-vs-V6 duplicates. All 12 components have exactly one canonical implementation. V5 file map (`V5_V6_FILE_MAP.md`) confirms no V5 source has been copied into the V6 tree. |
| 7 | `main.py` CLI audit | PASS | `--health` and `--provider=NAME` work; `run_cli` resolves the provider once and threads it through `_run_loop → _handle_line → _run_brain_plan / _run_execute_plan / _run_agent`. The dead `if provider is None: MockProvider()` fallback is unreachable on the CLI path. |
| 8 | Critical CLI real-provider audit | PASS | `run_cli` calls `get_provider(config)` exactly once at boot (`main.py` line 679) and threads the result through. No accidental `MockProvider()` swap is possible on the CLI path. Verified by the audit. |
| 9 | `--llm-health` dry-run command | PASS | New function `_run_llm_health(config, engine, offline=…)` in `main.py`. New flag `--llm-health` parsed in `_parse_argv`; `--offline` / `OMNIX_LLM_DRY_RUN=1` makes the probe construction-only. Exit codes: `0` (success), `2` (provider error), `3` (config error). The probe never prints a key value; only `api_key_count`. Five new tests in `tests/test_main_llm_health.py` cover deterministic/mock, offline, openrouter-without-key, and key-isolation. |
| 10 | Deterministic regression | PASS | 527 tests pass under `python -m pytest tests/ -q` in 18.6s, no network, no real OpenRouter key required. `python -m pip check` reports no broken requirements. |
| 11 | Vision placeholder audit | PASS | `docs/V6_VISION_PRE_PHASE_AUDIT.md` written. All 25 `.py` files in `vision/` and `core/services/vision_service.py` are 0 bytes. The audit identifies which V5 file shapes are wrong for V6 (the singleton-thread `VisionManager`, the hard-coded `VisionPipeline`) and which V5 ideas survive (UIA adapter, YOLO detector, bbox normalizer). |
| 12 | Vision architecture documentation | PASS | Audit doc §1 / §4 / §5 describe the V6 perception model: **`PerceptionRouter` selects one of `{UIA, OCR, vision/YOLO, DOM, coordinates}` per call**, with a `PerceptionStrategy` protocol and a `PerceptionOutput` carrying `ObservationSource` provenance. No code is written. |
| 13 | No Vision code | PASS | Zero new files in `vision/`. No new dependencies. `yolo11n.pt` is left in place but no YOLO inference path is imported anywhere. |
| 14 | Full regression | PASS | 527 passed, 6 warnings (all about the `@pytest.mark.real_windows` marker, pre-existing and unrelated). `pip check` returns `No broken requirements found.` |
| 15 | This report | PASS | `docs/V6_PHASE_6D_1_FINAL_HARDENING_REPORT.md` written. |
| 16 | STOP condition | PASS | The single allowed stop phrase is emitted below. |

---

## 3. Code-level changes

### 3.1 `ai/provider/openrouter.py`

* **Error mapping is now canonical.** 401 → `AuthenticationError`,
  403 → `AuthenticationError`, 404 → `InvalidRequestError`,
  429 → `RateLimitError` (captures `Retry-After`), 5xx → `UnavailableError`,
  non-timeout `RequestException` → `UnavailableError`,
  JSON decode error → `MalformedResponseError`,
  empty content with non-trivial finish reason → `MalformedResponseError`,
  missing `choices` → `MalformedResponseError`.
* **Bounded retries.** `max_retries` is clamped to `[0, 5]` at
  construction; a misconfigured caller cannot spin forever.
* **Bounded context growth.** Response bodies are truncated to
  500 chars and passed through `_redact()` before entering error
  context, so a 50 KB HTML error page or a misbehaving proxy that
  echoes the `Authorization` header back cannot leak the key.
* **Exponential backoff with `Retry-After`.** Falls back to
  `min(0.25 * 2**attempt, 5.0)`; honours the `Retry-After` header
  on 429.
* **Safe `__repr__`.** The provider's repr never includes the API key
  value; it shows `api_key='***'`.

### 3.2 `ai/provider/selection.py`

* The OpenRouter factory lambda now forwards
  `model=kwargs.get("model")` to the constructor so the precedence
  chain (request.model > OMNIX_LLM_MODEL > first in pool) is intact
  end-to-end.
* `_resolve_model_name()` reads `OMNIX_LLM_MODEL` (env) →
  `config.extra["llm_model"]` → `None`.

### 3.3 `main.py`

* New `--llm-health` flag with `--offline` modifier.
* `_parse_argv` returns a 6-tuple now
  `(help, health_only, headless, provider_name, llm_health_only, llm_offline)`.
* `_run_llm_health(config, engine, *, offline)` boots no CLI loop,
  constructs the configured provider, optionally performs one
  bounded completion call, and prints a structured report.
* Exit codes are documented and tested: 0 / 2 / 3.
* No key value is ever printed; only `api_key_count`.
* The module docstring documents the new flag.

### 3.4 `tests/test_openrouter_provider.py`

* Two legacy tests updated to assert the new (correct) error types:
  `test_openrouter_401_raises_authentication_error` (was: 401 →
  ConfigurationError_), `test_openrouter_500_retries_and_raises_unavailable`
  (was: 5xx → ProviderError).
* Eleven new tests added: 429+Retry-After, 403, 404, network
  ConnectionError, empty content, request.model override, repr
  redaction, retry clamping floor (0) and ceiling (5), bearer
  redaction.

### 3.5 `tests/test_main_llm_health.py` (new)

Five tests for the `--llm-health` CLI:

* `test_llm_health_deterministic_exits_zero_no_key_printed`
* `test_llm_health_offline_marks_probe_skipped`
* `test_llm_health_openrouter_without_key_exits_3`
* `test_llm_health_offline_openrouter_without_key_exits_3`
* `test_llm_health_prints_api_key_count_not_value`

All five use a temp `cwd` with NO `.env` file so the test runner's
real key cannot bleed into the assertions.

### 3.6 `docs/V6_VISION_PRE_PHASE_AUDIT.md` (new)

Comprehensive audit of the V6 vision tree. Documents the V5 file
shapes that must be **replaced** (singleton thread, hard-coded
pipeline) vs the V5 ideas worth carrying forward (UIA adapter, YOLO
detector, bbox normalizer, ObservationSource provenance). No code.

### 3.7 `.env.example` (new)

Created with **placeholder** values only. No real credentials.

---

## 4. Regression baseline

```
$ python -m pytest tests/ -q
527 passed, 6 warnings in 18.56s
$ python -m pip check
No broken requirements found.
```

The 6 warnings are pre-existing `PytestUnknownMarkWarning` notices
about `@pytest.mark.real_windows`, which is a real-Windows-only
integration marker and is irrelevant to this pass.

---

## 5. Phase 6D.1 invariants reaffirmed

* The provider subpackage still does not import `subprocess`,
  `pyautogui`, `win32gui`, `ctypes`, or `core.capability_router`
  (verified by `tests/test_provider_isolation.py`, still passing).
* The API key never appears in any of: `Plan`, `Goal`, `Intent`,
  `ActionRequest`, `ExecutionContext`, `AgentResult`, `Observation`,
  `CapabilityResult`, logs, repr output, exceptions, test snapshots,
  or CLI output (verified by `test_api_key_does_not_leak_into_request_or_response`,
  `test_openrouter_repr_redacts_api_key`, and the new
  `test_llm_health_prints_api_key_count_not_value`).
* No live OpenRouter call is required by `pytest`. All HTTP is
  patched at the `requests.post` boundary in
  `test_phase6d_e2e_dryrun.py` and the new openrouter tests.

---

## 6. Phase 7 readiness

Phase 7 (Vision) is now **unblocked**:

* The `vision/` directory is still the canonical namespace
  (R-14). The `yolo11n.pt` weights remain in place.
* The `PerceptionRouter` and `PerceptionStrategy` protocol are
  documented (audit doc §4) but **not** implemented.
* `ObservationSource` already exists in `core/orchestration/models.py`
  (values: `SCREEN`, `UIA`, `DOM`, `OCR`, `VISION`, …), so the
  Phase 7 implementation can wire into it without changing the
  orchestration types.
* `desktop.screenshot` is the only V6 capability that touches the
  screen, and it is already in place; Phase 7 will call it from
  the visual strategy, not from a top-level `VisionManager`.

No code in this commit is part of Phase 7. Phase 7 is the next
phase, with its own plan and its own audit.
