# V6 Phase 5A — Brain Provider Foundation (Implementation Report)

**Phase:** 5A — *Brain provider seam* + *manual development runtime*
**Status:** COMPLETE
**Date:** 2026-08-29
**Test result:** 248 / 248 passing (201 baseline + 47 new)
**Stop condition:** Ready for Phase 5B.

This report describes what was built in Phase 5A, what was deliberately
*not* built, and what the next phase will need from the foundation that
ships here.

---

## 1. Scope

Phase 5A was authorised to do two things and one documentation
correction.

### 1.1 Documentation correction (delivered)

`docs/V6_LEGACY_PLACEHOLDER_MAP.md` reclassified the `voice/`
namespace from **DROPPED** to **DEFERRED**. The five placeholder files
(`audio_utils.py`, `speech_recognizer.py`, `tts_engine.py`,
`voice_manager.py`, `wake_listener.py`) remain on disk so the
namespace exists, but the roadmap now records voice as a *final V6
subsystem* — natural voice input, STT, voice responses, and text
input as an additional interface — that is built *after* the
Brain/Orchestration phases ship, not abandoned.

### 1.2 PART A — Manual V6 runtime (delivered)

`main.py` is now a thin, interactive development entry point. It
does not implement the Brain, the Agent, or any capability of its
own. It uses the canonical `OmnixEngine` and the existing
`CapabilityRouter` / `ServiceRegistry` for every action it performs.

The runtime exposes the following surface:

| Command              | Effect                                                            |
|----------------------|-------------------------------------------------------------------|
| `help`               | List the built-in commands.                                       |
| `health`             | Re-run the engine's startup health check.                          |
| `capabilities`       | Print every registered capability and its description.            |
| `describe <name>`    | Show one capability's spec, parameters, and return schema.        |
| `<capability> k=v`  | Invoke a capability through `engine.execute(name, **params)`.     |
| `exit` / `quit` / `q`| Shut the engine down cleanly and leave.                            |
| `Ctrl+C` / `Ctrl+D`  | Same as `exit`.                                                    |

CLI flags:

* `python main.py`            — start the interactive runtime.
* `python main.py --health`   — boot the engine, print health, exit 0.
* `python main.py --quiet`    — silence the third-party loguru noise.

Design constraints honoured:

* **No Brain logic in `main.py`.** The runtime is a shell; it does
  not interpret user input, does not plan, does not call the
  provider.
* **No duplicate `OmnixEngine` logic.** Every capability call is
  `engine.execute(name, **params)`.
* **Clean shutdown.** `engine.stop()` runs in a `finally:` block no
  matter how the loop exits.
* **No crash on bad input.** Unknown commands print a friendly
  message; capability errors surface as structured
  `CapabilityResult` objects.

### 1.3 PART B — Brain provider foundation (delivered)

A new package, `ai/provider/`, defines the single seam the future
Omnix Brain will use to talk to any LLM. The provider layer produces
*data*; it does not execute actions.

```
ai/
└── provider/
    ├── __init__.py        # public exports
    ├── base.py            # LLMProvider protocol
    ├── contracts.py       # LLMRequest / LLMResponse / LLMUsage / LLMMessage
    ├── errors.py          # ProviderError hierarchy
    ├── mock.py            # MockProvider (deterministic in-process fake)
    └── selection.py       # get_provider / register_provider (config-driven)
```

The package ships one concrete implementation, `MockProvider`, and a
`get_provider(config)` factory that resolves the configured name
through (in order) `OMNIX_LLM_PROVIDER`, `config.extra["llm_provider"]`,
and finally the default `"mock"`. Other providers (OpenAI-compatible,
local, ...) are out of scope for Phase 5A but can be added at runtime
via `register_provider(name, factory)` without touching the Brain.

---

## 2. Architecture

### 2.1 The provider contract

The single seam is `ai.provider.LLMProvider`:

```python
@runtime_checkable
class LLMProvider(Protocol):
    name: str
    def generate(self, request: LLMRequest) -> LLMResponse: ...
    def statistics(self) -> Dict[str, Any]: ...
```

A provider takes a fully-formed `LLMRequest` and returns a fully-
parsed `LLMResponse`. It owns the wire format; the Brain sees only
typed Python objects.

### 2.2 The data types

| Type           | Purpose                                                                                  |
|----------------|------------------------------------------------------------------------------------------|
| `LLMMessage`   | One chat turn (role, content, optional name).                                            |
| `LLMRequest`   | A frozen, normalised request. The `__post_init__` merges a `system` prompt into the       |
|                | front of the message list so providers never have to remember the difference.             |
| `LLMUsage`     | Token accounting; every field is `Optional[...]` so partial providers still type-check.   |
| `LLMResponse`  | A frozen response with `content`, `finish_reason`, `model`, `usage`, `provider`, `raw`,   |
|                | `metadata`. `to_dict()` deliberately omits `raw` so secrets stay in the process.         |

### 2.3 The error hierarchy

```
ProviderError  (code: PROVIDER_ERROR)
├── AuthenticationError      (PROVIDER_AUTH_FAILED)
├── TimeoutError_            (PROVIDER_TIMEOUT)        # trailing _ to avoid shadowing builtin
├── RateLimitError           (PROVIDER_RATE_LIMITED)
├── UnavailableError         (PROVIDER_UNAVAILABLE)
├── InvalidRequestError      (PROVIDER_INVALID_REQUEST)
├── MalformedResponseError   (PROVIDER_MALFORMED_RESPONSE)
├── ModelUnavailableError    (PROVIDER_MODEL_UNAVAILABLE)
├── CancelledError           (PROVIDER_CANCELLED)
└── ConfigurationError_      (PROVIDER_CONFIG_INVALID) # trailing _ to avoid shadowing builtin
```

Every error carries a stable `code` (R-7/AD-7), a free-form
`message`, a structured `context` dict, and a preserved `cause`
exception. The Brain will branch on `code`, not on type. The
provider-specific exception is wrapped at the provider boundary —
`openai.error.RateLimitError` never reaches the Brain.

### 2.4 Selection

`get_provider(config)` is the single entry point the Brain will use.
It accepts any object that exposes an `.extra` dict, so it works
with the existing frozen `OmnixConfig` *and* with the test stubs
that don't pull in the full configuration loader. Resolution order:

```
1.  os.environ["OMNIX_LLM_PROVIDER"]
2.  config.extra["llm_provider"]
3.  default: "mock"
```

The factory never imports Windows automation, never touches the
network, and never silently falls back. An unknown name raises
`ConfigurationError_` with the requested name and the list of
known providers in the structured context.

### 2.5 The MockProvider

`MockProvider` is a deterministic, thread-safe, in-process
implementation. It never makes a network call. The constructor
accepts a `responder` callable so tests can fully drive the
provider's behaviour without monkey-patching.

Default behaviour: echo the last user message wrapped in a
`<mock>...</mock>` block. Token accounting is a naive whitespace
split. Latency and timeout simulation are configurable so tests
can exercise both happy paths and timeouts.

---

## 3. The Isolation Rule (R-21 / Phase 5A)

The provider layer is forbidden from importing any of the
following modules. This is enforced by
`tests/test_provider_isolation.py` (8 tests) using static
analysis on the AST:

| Module / package             | Why it's forbidden                                |
|------------------------------|---------------------------------------------------|
| `subprocess`                 | The provider must not start processes.            |
| `pyautogui`                  | The provider must not drive the desktop.          |
| `win32gui` / `win32api`      | The provider must not call the Win32 API.         |
| `ctypes`                     | The provider must not call native code.           |
| `core.capability_router`     | The Brain is the only place that dispatches.      |
| `core.omnix_engine`          | The provider is downstream of the engine.         |
| `system.windows.*`           | V6 Windows services.                              |
| `system.applications.*`      | V6 application services.                          |
| `system.input.*`             | V6 input services.                                |
| `system.filesystem.*`        | V6 filesystem services.                           |
| `system.clipboard.*`         | V6 clipboard services.                            |
| `system.processes.*`         | V6 process services.                              |

The isolation tests do three things:

1. **AST-level import scan** — every `import X` and `from X import Y`
   in `ai/provider/*.py` is checked against the forbidden list. A
   comment that *talks* about a forbidden import does not trip the
   test.
2. **Symbol-level guard** — `ai/provider/__init__.py` must not export
   `OmnixEngine`, `Engine`, `CapabilityRouter`, `execute`, or
   `run_capability`.
3. **`try/except` guard** — `ai/provider/*.py` must not wrap a
   forbidden import in a `try/except ImportError` to defeat the
   static check.

The package docstring (`ai/provider/__init__.py`) states the rule
where new contributors will read it.

---

## 4. Tests

Phase 5A added **47 new tests** in two files:

* `tests/test_provider.py` — 39 functional tests across eight
  classes covering the 10 categories of the directive:

  | Class                     | Directive category        | Tests |
  |---------------------------|---------------------------|------:|
  | `TestRequestConstruction` | (1) request construction  |     4 |
  | `TestResponseParsing`     | (2) response parsing      |     3 |
  | `TestErrorHandling`       | (3) error handling        |    11 |
  | `TestTimeoutAndCancellation` | (4) timeout / cancellation |  3 |
  | `TestMockProvider`        | (5) mock provider          |     5 |
  | `TestProviderSelection`   | (6) provider selection     |     5 |
  | `TestInvalidConfiguration`| (7) invalid configuration  |     2 |
  | `TestMalformedResponse`   | (8) malformed response     |     4 |
  | (9), (10) isolation guarantees — see `test_provider_isolation.py` | | |

* `tests/test_provider_isolation.py` — 8 static-analysis tests that
  enforce the (9) "no Windows automation" and (10) "no capability
  execution" guarantees.

Test results: **248 / 248 passing** (Phase 1-4 regression + Phase 5A
new tests). `python -m pip check` reports no broken requirements.

The isolation tests were written deliberately without mocks or
network stubs — they would still fail in the same way on a clean
CI runner. There is no flakiness: the mock provider is fully
deterministic; the static analysis is purely textual.

---

## 5. Security / configuration posture

* **No secret leakage.** `OmnixConfig.to_dict()` replaces
  `groq_api_key` with `"***"`. `LLMResponse.to_dict()` does not
  include `raw` at all, so any provider that put a key into `raw`
  cannot accidentally surface it through the typed surface.
* **No silent fallback.** `get_provider` raises
  `ConfigurationError_` on an unknown provider name. If a future
  feature adds explicit fallback it will be a configuration
  decision, not an implicit behaviour.
* **No V5 source copied.** A grep for `V5` in the new files shows
  only the historical placeholder references; no V5 code path is
  revived. The V5 `automation_engine.py` is still empty per the
  legacy placeholder map.
* **No new configuration system.** The provider seam reads
  `OmnixConfig.extra` and the `OMNIX_LLM_*` env vars. It does not
  invent its own loader, file format, or key store.

---

## 6. What is intentionally NOT in Phase 5A

The directive was explicit. The following are out of scope and were
not built:

* The full Brain (`brain_manager.py` is the placeholder; it is not
  populated).
* The autonomous Agent (`core/agent/` is all placeholders per the
  legacy map).
* The Planner / TaskPlanner / IntentClassifier.
* The RecoveryStrategy and RetryManager.
* Real LLM providers (OpenAI, Groq, local). `MockProvider` is the
  only concrete implementation. The selection module is wired so
  Phase 5B can drop in a real provider by calling
  `register_provider("openai", lambda *, model: OpenAIProvider(...))`.
* Vision, Browser, Voice, Memory, Skills.

Anything in the legacy placeholder map that says
**REPLACED** in Phase 4 stays **REPLACED**. The new files in this
phase do not redefine those surfaces.

---

## 7. File inventory (Phase 5A)

### New files
```
ai/__init__.py
ai/provider/__init__.py
ai/provider/base.py
ai/provider/contracts.py
ai/provider/errors.py
ai/provider/mock.py
ai/provider/selection.py
tests/test_provider.py
tests/test_provider_isolation.py
docs/V6_PHASE_5A_IMPLEMENTATION_REPORT.md   (this file)
```

### Modified files
```
docs/V6_LEGACY_PLACEHOLDER_MAP.md   # voice/ reclassified DEFERRED (final V6 subsystem)
main.py                              # rewritten as the manual development runtime
```

### Unchanged
Every V6 production module under `core/`, `system/`, and
`core/orchestration/`. The provider seam sits beside them; it
does not touch them.

---

## 8. Known limitations (carried into Phase 5B)

* The mock provider's "timeout" is simulated by comparing
  `default_latency_s` against `request.timeout_s`. Phase 5B can
  replace this with a cancellable future when a real provider
  adapter needs a wall-clock deadline.
* The selection factory is intentionally a flat registry. Routing
  by model name, by region, or by cost will be a Phase 5B concern.
* `LLMRequest.to_dict()` serialises everything in plain JSON-safe
  types. If the Brain ever wants to attach a non-JSON object, it
  must go through `LLMRequest.context` (a free-form dict) and the
  provider must define its own wire format for that field.
* `LLMMessage.content` is `str` only. Multi-modal / structured
  content blocks are a Phase 5B+ concern.

None of these is a blocker for the next phase.

---

## 9. Sign-off

| Item                                              | Result |
|---------------------------------------------------|--------|
| Phase 1-4 regression                              | PASS (201 tests) |
| Phase 5A new tests                                | PASS (47 tests)  |
| `python -m pip check`                             | clean             |
| V5 source copied into V6 production paths         | NONE              |
| Duplicate config / provider systems               | NONE              |
| Windows automation imports in `ai/provider/`      | NONE              |
| `main.py` is thin and uses `OmnixEngine`          | YES               |
| Manual runtime starts / shuts down cleanly        | YES               |

**PHASE 5A COMPLETE — BRAIN PROVIDER FOUNDATION VALIDATED. MANUAL V6 RUNTIME AVAILABLE. READY FOR PHASE 5B.**
