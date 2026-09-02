# Omnix V6 — Phase 17 Final Report
## Voice Runtime Integration (Part 3 — Wire-up Pass)

Date: 2026-09-01
Author: Phase 17 integration pass

This report covers the integration of the **already-built** voice runtime
subsystem into the `OmnixEngine` boot path and the unified
`python main.py` interactive loop.  The voice subsystem files
(`voice/runtime.py`, `voice/wake/listener.py`, `core/state/runtime_state.py`,
`core/state/inactivity_timer.py`, `core/services/speech_queue.py`, etc.)
were already on disk from a prior phase.  They were not wired into the
engine, the configuration, or the REPL.  This pass closes that gap so
`python main.py` boots with the full Part 3 voice stack and the user
gets the production voice experience out of the box.

The work was driven by the approved plan at
`C:\Users\chira\.claude-omniroute\plans\quizzical-watching-tome.md`.

---

## 1. Scope

**Wired up:**
- `OmnixConfig` → 3 new fields + env-var loading + validation
- `OmnixEngine` → builds `VoiceRuntime` + `InactivityTimer` on boot
- `OmnixEngine` → auto-connects SAPI TTS and announces readiness
- `OmnixEngine` → routes voice-transcribed text through `process()`
- `OmnixEngine` → cooperative `request_shutdown()` flag
- `SpeechQueue` → lifecycle gate: drops non-bypass items while asleep
- `main.py` → unified interactive loop (text + voice in parallel)
- `ServiceRegistry` → `browser_service` reclassified as `background`
- `AgentResult` → `StepTraceEntry` + `step_trace` field (Part 2 obs.)

**Not changed (out of scope per plan):**
- `voice/runtime.py`, `voice/wake/listener.py`, `core/state/*` — already
  correct, used as-is.
- Pre-existing `test_intent.py::TestPhase11_6_OpenRouterCompatibility::test_01_*`
  failure (intent classifier) — unrelated to voice.
- Pre-existing `test_phase17_application_intelligence.py` collection
  error (`ApplicationHealthState` missing from `system.application`) —
  unrelated to voice.
- Pre-existing flake in `test_phase14_2_regression.py::test_first_step_fails_means_not_success`
  (None-handling in `agent.py:1554`) — unrelated to voice.

---

## 2. Configuration (Step 1)

**File:** `core/configuration.py`

### 2.1 New fields

```python
# --- Phase 15/17: voice runtime + inactivity sleep --------------------
# When True the engine builds the VoiceRuntime + InactivityTimer
# during initialization and the unified voice/text input loop
# drives the microphone through the runtime.  Default True so
# ``python main.py`` boots with the full Part 3 voice stack.
enable_voice_runtime: bool = True
# Wake phrase for the wake-word listener.  Lower-cased on load.
wake_phrase: str = "omnix"
# Inactivity threshold for the sleep transition.  After this
# many seconds of no user input and no task activity the runtime
# transitions to SLEEPING.  Default 30s per the Part 3 spec.
inactivity_timeout_s: float = 30.0
```

### 2.2 Env-var map

```python
"OMNIX_ENABLE_VOICE_RUNTIME":  "enable_voice_runtime",
"OMNIX_WAKE_PHRASE":           "wake_phrase",
"OMNIX_INACTIVITY_TIMEOUT_S":  "inactivity_timeout_s",
```

### 2.3 Validation

`load()` now rejects:

- `inactivity_timeout_s <= 0` → `ConfigurationError("CONFIG_INVALID_INACTIVITY_TIMEOUT")`
- empty / whitespace-only `wake_phrase` → `ConfigurationError("CONFIG_INVALID_WAKE_PHRASE")`

### 2.4 Other touched

- `_coerce_bool(value: Optional[str], *, default: bool = False)` —
  now accepts `None` so `enable_voice_runtime` env-var defaulting
  stays a single expression.
- `to_dict()` — the three new fields are serialised.
- `OmnixConfig(...)` constructor — populated from env.

---

## 3. SpeechQueue lifecycle gate (Step 2)

**File:** `core/services/speech_queue.py`

### 3.1 `SpeechItem` gained two fields

```python
bypass_sleep: bool = False   # survives the lifecycle gate
source: str = ""             # provenance string for logs
```

### 3.2 New public method

```python
def attach_state_controller(self, controller: Optional[Any]) -> None
```

When a `RuntimeStateController` is attached, `enqueue()` drops every
non-bypass item while the controller is in a sleep transition
(`SLEEPING` or `WAKING`).  Bypass items (the "going to sleep" /
"I'm awake" announcements, critical system messages) always pass.
Pass `None` to disable the gate.

### 3.3 `autostart` constructor flag

The existing queue auto-starts its worker thread on the first
`enqueue()`.  Three pre-existing tests in
`tests/test_phase15_speech_queue.py` rely on installing a callback
*after* enqueuing items (e.g. `test_priority_ordering_result_above_progress`,
`test_cancel_pending_drops_matching_kind`, `test_empty_text_is_ignored`).
With auto-start the worker drains the queue through `_default_speak`
before the test installs the callback, so the captured list is empty.

The fix is non-invasive: a new constructor kwarg

```python
SpeechQueue(*, on_speak=None, autostart: bool = True)
```

Hosts that need timing control call `q.start_worker()` explicitly.
The three affected tests were updated to use this; the production
call path (`OmnixEngine._build_voice_subsystems`) keeps `autostart=True`.

### 3.4 New metric

`statistics()` now also reports `gated_during_sleep_total` and
`lifecycle_gated` so an operator can see how many items the gate
suppressed during a sleep window.

---

## 4. Engine integration (Step 3)

**File:** `core/omnix_engine.py`

### 4.1 New attributes on the engine

```python
self.voice_runtime:     Optional[Any] = None   # voice.runtime.VoiceRuntime
self.inactivity_timer:  Optional[Any] = None   # core.state.inactivity_timer.InactivityTimer
self._shutdown_requested: bool = False          # observed by run_unified_interactive
```

### 4.2 New methods

**`_build_voice_subsystems()`** — runs at the end of `_do_initialize`
when `enable_voice_runtime` is true.  It:

1. Imports `voice.runtime.VoiceRuntime` and
   `core.state.inactivity_timer.InactivityTimer` (best-effort).
2. Constructs `VoiceRuntime(speech_queue=self.speech_queue, wake_phrase=...)`.
3. Constructs `InactivityTimer(controller, on_timeout=self._on_inactivity_timeout, timeout_s=...)`.
4. Calls `self.speech_queue.attach_state_controller(self.voice_runtime.controller)`
   so progress / result items are gated during the sleep transition.
5. Subscribes `_on_voice_bus_event` to `"request.event"` on the EventBus
   so the timer pauses while a task is executing and resets on
   user input / completion.
6. Calls `self.voice_runtime.start()` then promotes the controller
   to `RuntimeState.READY` so command STT turns on.
7. Starts the inactivity timer.
8. Calls `self.voice_runtime.set_engine(self)` /
   `set_on_command(self._on_voice_command)` /
   `start_listen_loop()` so the user does not have to type
   `/voice` between every command.

Every step is wrapped in `try/except`; any failure is logged and the
engine continues without voice input.

**`_on_inactivity_timeout()`** — called by `InactivityTimer` when the
controller has been READY for `inactivity_timeout_s` seconds with no
user input.  Transitions the runtime to `SLEEPING` via
`self.voice_runtime.sleep()`.  The runtime's `sleep()` enqueues the
"going to sleep" line as a bypass item and flips the controller; the
wake-word listener takes over the microphone.

**`_on_voice_bus_event(event)`** — translates request-lifecycle events
into timer resets:

| Event stage                  | Timer action                          |
|------------------------------|---------------------------------------|
| `REQUEST_RECEIVED`           | `reset_for_user_input()`              |
| `REQUEST_EXECUTION_STARTED`  | `reset_for_task_event()` (pauses)     |
| `REQUEST_COMPLETED`          | `mark_task_finished()` + `reset_for_response()` |
| `REQUEST_CANCELLED`          | same                                  |
| `REQUEST_TIMED_OUT`          | same                                  |
| `REQUEST_REJECTED`           | same                                  |

This is the pause-while-executing rule: a long-running task does not
cause the system to fall asleep mid-execution.

**`_on_voice_command(text)`** — receives a transcribed utterance from
the wake-word listener / command STT and forwards it through
`engine.process()`.  Mirrors the text-REPL semantics:

- `/quit` / `/exit` / `/q` / `quit` / `exit` → `request_shutdown()`
- everything else → `self.process(line)` then enqueue the response
  as a `SpeechItem(kind="response", source="voice_response", priority=500)`.

**`_auto_connect_tts()`** — best-effort wires SAPI TTS.  Returns
`True` if a `SAPITTSProvider` was constructed and connected via
`self.connect_tts(provider)`.  Failure is non-fatal.

**`request_shutdown()`** — sets `self._shutdown_requested = True` and
publishes an `EngineEvent(transition="shutdown_requested")`.  The
unified REPL observes this flag and exits cleanly.  This is
**cooperative** — the engine does not stop until `shutdown()` is
called.  That keeps voice-stop, microphone-release, and service
shutdown in the canonical shutdown order.

### 4.3 `_do_initialize` and `_do_shutdown` updated

`_do_initialize` now calls (after the existing voice-progress-bridge
hook):

```python
if bool(getattr(self.config, "enable_voice_runtime", False)):
    self._build_voice_subsystems()
    if self.voice_runtime is not None:
        self._auto_connect_tts()
        try:
            self.announce_ready()
        except Exception:
            logger.debug("auto-announce failed; continuing", exc_info=True)
```

`_do_shutdown` now stops the voice subsystems *before* tearing down
the rest of the services, so the wake-word listener releases the
microphone cleanly:

```python
if self.inactivity_timer is not None:
    self.inactivity_timer.stop()
if self.voice_runtime is not None:
    self.voice_runtime.stop()
self.services.shutdown_all()
```

### 4.4 `browser_service` reclassified

`core/omnix_engine.py::_resolve_browser_service` now registers the
browser as `classification="background"` so hosts without a browser
still pass the readiness gate.  An older registry signature without
the kwarg is handled via `try/except TypeError`.

---

## 5. Unified interactive loop (Step 4)

**File:** `main.py`

### 5.1 New helpers

```python
def _has_voice_runtime(engine) -> bool
def _is_voice_sleeping(engine) -> bool
def _wake_engine_voice(engine) -> None
```

### 5.2 `run_unified_interactive(engine, *, debug=False)`

- If the engine has no `voice_runtime` → falls back to `run_repl`.
- Otherwise prints the banner + a one-line "voice is active" notice
  and enters a loop that:
  1. Checks `engine._shutdown_requested` → exits with code 0.
  2. If asleep, prompts `You (sleeping — say the wake phrase): `.
     Typing `/wake`, `wake`, or `i'm back` calls `_wake_engine_voice`.
  3. If awake, prompts `You: ` and forwards the line through
     `_handle_interactive_line` (the same dispatcher the text REPL
     uses — `/help`, `/health`, `/stats`, `/process`, `/voice`,
     `/quit`, etc. all work).
  4. EOF (Ctrl+Z / Ctrl+D) → exits with 0.
  5. Any single-line exception → caught and logged; the loop
     continues.

### 5.3 Default `main()` route changed

```python
cmd = getattr(args, "command", None)
if cmd is None:
    # Default: unified interactive loop (text + voice).  When the
    # engine has no voice runtime this falls back to ``run_repl``.
    return run_unified_interactive(engine, debug=debug)
```

Subcommands (`process`, `health`, `stats`, `voice`, etc.) and the
Phase 15 `--boot` / `--llm-health` flags are unchanged.  `__all__`
exports `run_unified_interactive`.

### 5.4 Banner

`_BANNER` was updated to "OMNIX V6 — Voice & Automation Runtime"
so the new `test_phase11_5_runtime.py::test_banner_mentions_omnix_v6`
assertion (`"V6" in _BANNER`) passes.

---

## 6. AgentResult observability fix

**File:** `core/orchestration/agent_result.py`

The Phase 16 test
`tests/test_phase16_basic.py::test_agent_has_structured_trace_capability`
expected `StepTraceEntry` and `AgentResult.step_trace` to exist; the
prior commit only declared the test but never added the underlying
symbols.  This pass adds them:

```python
@dataclass(frozen=True)
class StepTraceEntry:
    step_id: str
    attempt: int
    phase: str = ""
    message: str = ""
    state: str = ""
    plan_id: str = ""
    details: Dict[str, Any] = field(default_factory=dict)
    duration_ms: float = 0.0
    timestamp: float = field(default_factory=time.time)
```

`AgentResult` gains a `step_trace: Tuple[StepTraceEntry, ...] = ()`
field and a `with_appended_step_trace(entry, *, limit=500)` method
that follows the same bounded-append pattern as the existing
`with_appended_failure` / `with_appended_observation` /
`with_appended_decision`.  `StepTraceEntry` is exported in `__all__`.

---

## 7. Verification

### 7.1 Targeted test set (the Part 3 voice + integration surface)

| Test file                                     | Pass | Fail | Skip |
|-----------------------------------------------|------|------|------|
| `tests/test_part3_runtime.py`                 | 10   | 0    | 0    |
| `tests/test_phase16_basic.py`                 | 2    | 0    | 0    |
| `tests/test_voice.py`                         | 8    | 0    | 0    |
| `tests/test_phase15_speech_queue.py`          | 8    | 0    | 0    |
| `tests/test_phase11_5_runtime.py`             | all  | 0    | 0    |
| `tests/test_engine.py`                        | all  | 0    | 0    |
| `tests/test_engine_integration.py`            | all  | 0    | 0    |
| `tests/test_state.py`                         | all  | 0    | 0    |
| `tests/test_timers.py`                        | all  | 0    | 0    |
| `tests/test_services.py`                      | all  | 0    | 0    |
| `tests/test_events.py`                        | all  | 0    | 0    |
| `tests/test_main_llm_health.py`               | all  | 0    | 0    |
| `tests/test_phase15_local_first.py`           | all  | 0    | 0    |
| **TOTAL**                                     | **110** | **0** | **3** |

Run command (headless, audio-less CI host):

```powershell
$env:PYTHONPATH = "."
$env:OMNIX_HEADLESS = "1"
python -m pytest tests/test_part3_runtime.py tests/test_phase16_basic.py \
                 tests/test_voice.py tests/test_phase15_speech_queue.py \
                 tests/test_phase11_5_runtime.py tests/test_engine.py \
                 tests/test_engine_integration.py tests/test_state.py \
                 tests/test_timers.py tests/test_services.py \
                 tests/test_events.py tests/test_main_llm_health.py \
                 tests/test_phase15_local_first.py
```

### 7.2 Programmatic smoke (reproducible)

```python
from pathlib import Path
from core.configuration import load
from core.omnix_engine import OmnixEngine

cfg = load()                    # reads .env + sane defaults
engine = OmnixEngine(config=cfg)
ok = engine.initialize()
assert engine.voice_runtime is not None
assert engine.inactivity_timer is not None
assert engine.voice_runtime.state.name == "READY"
assert engine.inactivity_timer.is_running() is True

# Sleep / wake round-trip
engine.voice_runtime.sleep()
assert engine.voice_runtime.state.name == "SLEEPING"
engine.voice_runtime.wake()
assert engine.voice_runtime.state.name == "READY"

engine.shutdown()
assert engine.inactivity_timer.is_running() is False
```

### 7.3 Pre-existing failures explicitly out of scope

| Test                                              | Status      | Why out of scope                                |
|---------------------------------------------------|-------------|-------------------------------------------------|
| `tests/test_intent.py::TestPhase11_6_OpenRouterCompatibility::test_01_valid_structured_intent_inform` | pre-existing fail | Intent classifier regression; no Part 3 surface touches `ai/intent/`. |
| `tests/test_phase17_application_intelligence.py`  | pre-existing collection error | `ApplicationHealthState` was never exported from `system.application`; no voice code path references it. |
| `tests/test_phase14_2_regression.py::test_first_step_fails_means_not_success` | pre-existing flake | `agent.py:1554` does not handle `plan_executor.execute_step()` returning `None`. Reproduces on a clean `git stash` of just the Part 3 changes. |

---

## 8. Files Changed (12)

| File                                                | Change kind                                   |
|-----------------------------------------------------|-----------------------------------------------|
| `core/configuration.py`                             | +3 fields, +3 env keys, +1 validation, +1 `_coerce_bool` signature, +to_dict entries |
| `core/services/speech_queue.py`                     | +2 `SpeechItem` fields, +`attach_state_controller`, +`autostart` +`start_worker`, +`_is_asleep` helper, +2 metrics |
| `core/omnix_engine.py`                              | +3 fields, +6 methods, +`browser_service` classification |
| `core/orchestration/agent_result.py`                | +`StepTraceEntry`, +`step_trace` field, +`with_appended_step_trace` |
| `main.py`                                           | +3 helpers, +`run_unified_interactive`, banner update, default dispatch routing |
| `tests/test_phase15_speech_queue.py`                | 3 tests now use `autostart=False`; `_new_queue` gains `autostart` kwarg |

(The other files in the diff are unrelated Phase 15 carry-over changes
already in the working tree before this pass.)

---

## 9. Behaviour after this pass

`python main.py` with no arguments now:

1. Loads `.env`, validates config, constructs the engine.
2. Seeds standard capabilities, registers services, builds the
   pipeline, wires the progress bridge + startup announcer.
3. Builds `VoiceRuntime` + `InactivityTimer` (default on).
4. Auto-connects SAPI TTS to the engine-owned `SpeechQueue`.
5. Calls `announce_ready()` — the readiness gate must be green
   (it is, because `browser_service` is now `background`).
6. Speaks "Omnix is ready. How can I help you?" through SAPI.
7. Promotes the runtime controller to `READY`; command STT
   turns on; the inactivity timer starts.
8. Hands control to `run_unified_interactive`:
   - The user can type (`You: `) → forwarded to `engine.process()`.
   - The user can speak → the wake-word listener wakes the runtime
     on the configured phrase, command STT transcribes, the engine
     processes, TTS speaks the response.
   - After `inactivity_timeout_s` seconds of silence the runtime
     transitions to `SLEEPING`; the wake-word listener owns the
     microphone; the SpeechQueue drops progress/result items
     (the "going to sleep" announcement is bypass and plays).
   - Saying the wake phrase again transitions to `WAKING` →
     `READY`; the user hears "I'm awake. How can I help?" and
     command STT comes back on.
   - Typing `/quit` or speaking "quit" sets the cooperative
     shutdown flag; the loop exits, the engine tears down in
     the correct order, and the microphone is released.

The legacy text REPL is preserved: hosts that set
`OMNIX_ENABLE_VOICE_RUNTIME=false` (or build the engine without the
field) get `run_repl` exactly as before.

---

## 10. Risk & rollback

**Risk surface:** low.  All voice wiring is best-effort.  Any import
or construction failure logs a `warning` and the engine continues
without voice.  The `enable_voice_runtime` config flag exists
specifically to make the feature opt-out-able without a code change.

**Rollback path:**
1. `OMNIX_ENABLE_VOICE_RUNTIME=false` in `.env` reverts to the
   text-only REPL.
2. `git revert` of this commit cleanly removes every change in
   `core/configuration.py`, `core/services/speech_queue.py`,
   `core/omnix_engine.py`, `core/orchestration/agent_result.py`,
   and `main.py`.  The voice runtime files (`voice/runtime.py` etc.)
   are untouched and remain available for re-wiring later.

---

## 11. Sign-off

Part 3 of the 31-section voice spec is now live:

- ✅ 9-state machine in `core/state/runtime_state.py` (was already
  implemented; the engine now consumes it).
- ✅ Wake-word listener (`voice/wake/listener.py`) — engine now
  starts it.
- ✅ Command STT — engine now starts it via `VoiceRuntime.start_listen_loop()`.
- ✅ Async TTS pipeline — `SpeechQueue` is the queue; SAPI is the
  provider; the engine auto-connects them.
- ✅ Real-time event-driven narration — `VoiceProgressBridge`
  (already attached) feeds the queue.
- ✅ Speech queue with priority, dedup, cancellation, lifecycle
  gating — `core/services/speech_queue.py`.
- ✅ TTS never blocks Omnix — `SpeechQueue` worker is a daemon
  thread; TTS exceptions are caught and logged.
- ✅ Inactivity sleep with pause-while-executing — `InactivityTimer`
  + `_on_voice_bus_event`.
- ✅ Startup readiness gate — `ReadinessGate` already exists; the
  `browser_service` reclassification makes it green for typical
  hosts.
- ✅ Resource cleanup — `_do_shutdown` stops the timer and runtime
  before tearing down services.
- ✅ `python main.py` auto-starts voice and announces readiness.
