# V6 Phase 11.5 — User Runtime (Thin Front Door)

**Phase:** 11.5 — User-facing runtime
**Date:** 2026-08-30
**Status:** ✅ Complete. Ready for Phase 12.
**Supersedes:** the dev-mode CLI in `main.py` that pre-dated Phase 11.5.
**Stop condition honoured:** Phase 12 is **NOT** started in this report.

---

## Goal

Turn the integrated V6 system into a usable manual runtime so the
developer can actually interact with Omnix through text and voice, and
observe what the integrated system can currently do — without building
a *second* automation pipeline on top of the canonical one.

The canonical V6 entry point (`OmnixEngine.process(text)`) is the only
path the user has to the system.  `main.py` is a **thin front door**:
it handles startup, argument parsing, engine initialization,
interactive input, command dispatch, response display, and graceful
shutdown, and it delegates **all real work** to V6 services.

## What changed

### 1. `main.py` was rewritten as a thin front door

The pre-Phase-11.5 `main.py` was a developer/dev-mode CLI with its
own command-routing layer.  The new `main.py` is structured around
five responsibilities only:

| Responsibility | Mechanism |
|---|---|
| Startup | `build_engine()` boots `OmnixEngine` once per process. |
| Argument parsing | `argparse` with five top-level flags and four subcommands. |
| Engine initialization | The single `OmnixEngine` instance is reused by every subcommand. |
| Interactive input | An REPL loop driven by `_handle_interactive_line()`. |
| Command dispatch | Slash-prefixed lines (`/help`, `/health`, ...) bypass the engine; everything else goes to `engine.process()`. |
| Response display | `format_response()` shows only the user-facing `text`; debug mode adds metadata. |
| Graceful shutdown | `engine.stop()` runs in a `finally` block; env vars are restored. |

The CLI is **never** the place that decides *what* the user means by
"open chrome", "what time is it", "search for cats", or any other
natural-language input.  All natural-language lines are passed
verbatim to `engine.process(line)`.  This is enforced by the
`test_interactive_line_does_not_hardcode_routes` parametrized test
(see `tests/test_phase11_5_runtime.py`).

### 2. New `argparse` interface

```
python main.py                       # interactive REPL (default)
python main.py process "text"        # one-shot request
python main.py health                # engine health
python main.py stats                 # engine statistics
python main.py voice                 # one voice turn
python main.py voice --turns 3       # three voice turns
python main.py --debug process hi    # run with debug info
python main.py --llm-health          # LLM provider probe (Phase 6D.1)
python main.py --llm-health --offline  # construct-only (no HTTP)
python main.py --provider openrouter   # override the LLM provider
python main.py --headless              # force OMNIX_HEADLESS=1
```

Each flag is the only knob that needs to exist for the developer to
exercise the system.  The flag set is closed — adding a new flag is an
architectural change, not a development convenience.

### 3. Interactive slash commands

In the REPL (the default with no subcommand), a single leading slash
denotes a meta command.  Anything else is a natural-language request
forwarded to `engine.process()`.

| Slash command | Effect |
|---|---|
| `/help` | Print the REPL help. |
| `/health` | Print a compact engine + subsystem health snapshot. |
| `/stats` | Print engine statistics. |
| `/process T` | Run `engine.process(T)` once (debug aid). |
| `/voice` | One voice turn: listen → process → speak. |
| `/clear` | Clear the screen (ANSI). |
| `/quit` (or `/exit`, `/q`) | Leave the REPL.  `Ctrl+C` does the same. |

`/voice` and the `voice` subcommand both degrade gracefully when the
voice subsystem is not importable in the current environment
(voice is **optional** — see Phase 10).

### 4. Canonical provider `health()` surface (Phase 11.5 contract)

`LLMProvider` now declares a `health()` method on the Protocol, and
both `MockProvider` and `OpenRouterProvider` implement it with the
canonical shape:

```python
{
    "name":   str,    # provider name (matches LLMProvider.name)
    "ok":     bool,   # False if the provider is misconfigured / down
    "reason": str,    # short human sentence; "" when ok
    "stats":  dict,   # whatever statistics() returned
}
```

The `OpenRouterProvider` is considered `ok` iff it has an API key
configured.  It does **not** issue a live HTTP probe from `health()`;
that path is owned by `python main.py --llm-health` (Phase 6D.1).
`MockProvider` is always `ok: True` because it is offline by design.

### 5. Canonical vision construction helper

A new `make_screenshot_provider(engine, *, headless=None)` factory
exists in `vision/router/screenshot_provider.py`.  It is the **only**
place in V6 that decides which `ScreenshotProvider` a host should use
to back `VisionService`.

Resolution order:

1. If `headless` is explicitly `True` OR the environment sets
   `OMNIX_HEADLESS=1` OR the engine's
   `OmnixConfig.enable_vision` is `False`, return
   `NullScreenshotProvider()` (no screen).
2. Otherwise, return `CapabilityScreenshotProvider(engine)` so the
   closed capability set remains the only path to the screen (R-21).

The helper is intentionally narrow: it does **not** import
`OmnixEngine` itself, and it does **not** touch `pyautogui` / `mss`
directly.  Screenshots always go through the canonical
`desktop.screenshot` capability when running for real.

### 6. Secret redaction (defence in depth)

`main.py` exports a `redact_secrets()` function.  It is called on
every line of CLI output and replaces any line that smells like a
secret (`sk-`, `Bearer`, `api_key=`, `password=`, `token=`, ...) with
the marker `[REDACTED]`.

This is the **last** line of defence — the engine itself never
returns secrets, and no key is ever constructed inside `main.py`.  But
because the CLI is the only place the user sees raw strings, it is
the right place to do final redaction.  This is intentionally
permissive: losing one line of harmless text is far better than
leaking a key.

## Architectural invariants honoured

The Phase 11.5 spec listed ten hard "do not"s.  All ten are honoured
in the rewrite:

| Invariant | How it is honoured |
|---|---|
| No second automation pipeline. | `main.py` only forwards text to `engine.process()`. |
| No hard-coded `if text == "open chrome":` routing. | Parametrized test `test_interactive_line_does_not_hardcode_routes` covers nine representative inputs. |
| No duplicate `.env` loader / OpenRouter client. | `main.py` only touches config via `core.configuration.load()` and the `LLMProvider` factory. |
| No new engine / brain / agent. | `main.py` imports `OmnixEngine` and only public interfaces. |
| No secrets printed. | `redact_secrets()` covers all CLI output.  `OpenRouterProvider.__repr__` redacts the key. |
| No API key printed even on `--llm-health`. | The probe uses the canonical provider's `statistics()` and a typed error on failure. |
| No `pyautogui` / `subprocess` / `win32gui` / `win32api` imports. | `test_main_module_does_not_import_forbidden_dependencies` scans the source. |
| No direct construction of `BrowserService` / `MemoryStore` / `OpenRouterProvider` / `VisionService`. | Same test as above. |
| No weakening of safety mechanisms. | The CLI does not add a shell escape, does not bypass safety, and does not register new capabilities. |
| No Phase 12 work. | This report is the deliverable; no further work was done. |

## Tests

### New tests

`tests/test_phase11_5_runtime.py` (51 tests, all deterministic and
offline):

- **CLI isolation (1 test):** `main.py` does not import forbidden
  dependencies or construct subsystem classes.
- **Argument parsing (5 tests):** top-level flags, subcommands,
  default behaviour, positional/keyword argument forms.
- **Banner / help surface (2 tests):** the banner mentions OMNIX V6;
  interactive help lists every slash command.
- **Secret redaction (5 tests):** all forbidden patterns are caught;
  non-secret text passes through; mixed lines are partially redacted;
  non-string input is safe.
- **Response formatter (4 tests):** normal mode hides metadata; debug
  mode shows it; `None` is handled; secret-bearing responses are still
  redacted.
- **No hard-coded automation (9 parametrized tests):** every
  representative input — "open chrome", "open notepad", "what time is
  it", "search for cats", "play some music", "tell me a joke",
  "launch the browser", "shutdown the computer", "click on the start
  menu" — is forwarded verbatim to `engine.process()`.
- **Interactive commands (9 tests):** `/quit`, `/exit`, `/q`, `/help`,
  `/health`, `/stats`, `/process T`, `/clear`, unknown slash commands.
- **Subcommand helpers (5 tests):** `run_process_cli` (empty input,
  success, engine exception), `run_health_cli`, `run_stats_cli`,
  `run_voice_cli` (zero turns, missing VoiceService).
- **Provider health (3 tests):** MockProvider health shape;
  OpenRouterProvider health with a key; OpenRouterProvider refuses
  construction without a key.
- **Vision construction (4 tests):** `NullScreenshotProvider` for
  headless / no engine / explicit headless; `CapabilityScreenshotProvider`
  when vision is enabled.
- **REPL robustness (1 test):** the REPL continues after a
  `RuntimeError` in `engine.process()`.
- **Top-level main (1 test):** `--help` exits 0.
- **build_engine headless (1 test):** `OMNIX_HEADLESS=1` is set
  before engine construction.
- **Voice stubs (2 tests):** stubbed VoiceService; missing
  VoiceService degrades gracefully.

### Existing tests

`tests/test_main_llm_health.py` (5 tests) was preserved with
backward-compatible shims (`boot_engine`, `run_cli`, `_build_engine`,
`_parse_argv`, `_run_llm_health`).

`tests/test_phase11_integration.py` and
`tests/test_phase11_scenarios_a_to_e.py` were hardened with
autouse fixtures that snapshot the `OMNIX_LLM_PROVIDER` /
`OMNIX_HEADLESS` / `OMNIX_QUIET_BOOT` env vars and restore them after
each test.  Without this, a stale `OMNIX_LLM_PROVIDER=mock` leaked
into the test suite and broke the Phase 6D end-to-end test that
relies on `get_provider(cfg)` honouring `cfg.extra["llm_provider"]`.

### Full regression

```
$ python -m pytest tests/ -q --timeout=60
1077 passed, 6 warnings in 21.75s
```

```
$ python -m pip check
No broken requirements found.
```

```
$ python -m compileall -q ai core vision browser voice system main.py
(no output)
```

## Stop condition

This report is the deliverable for Phase 11.5.

**Phase 12 is NOT started.**  No release / packaging work has been
done.  No new automation features have been added.  No V5 code has
been copied.  No second engine, brain, agent, or pipeline has been
created.

The next phase (12) is gated on the user explicitly starting it.

---

**PHASE 11.5 COMPLETE — USER RUNTIME VALIDATED. READY FOR PHASE 12.**
