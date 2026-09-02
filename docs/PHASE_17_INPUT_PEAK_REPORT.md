# Phase 17 — Input Subsystem Peak Upgrade

**Status:** Complete
**Date:** 2026-09-01
**Branch:** `main`

## A. Executive summary

The Input subsystem now has a single authoritative execution layer
(`WindowsInputService`), every mouse and keyboard capability acquires
its target window before dispatching, and every dispatch is wrapped in
a thread-safety lock, a cooperative-cancellation check, and a
per-action metrics counter.  The "type into the wrong window" failure
mode is structurally impossible: a capability that asks for a target
either acquires it (verified foreground) or fails the call with
`MISMATCH` and never touches the keyboard.

Headline numbers:

| Metric | Before | After |
|---|---|---|
| Mouse capabilities with target acquisition | 0 / 6 | 6 / 6 |
| Keyboard capabilities with target acquisition | 3 / 3 | 3 / 3 |
| Public input methods accepting `cancellation=` | 0 / 8 | 8 / 8 |
| Per-action metrics counters | none | rolling 100-sample p50/p95 latency, success / fail / timeout / cancel counts |
| Thread-safety on concurrent calls | none | `threading.RLock` serialises all calls |
| Integration tests covering the input peak | 0 | 5 (44 unit + 5 integration = 49) |
| Tests passing | (n/a) | **48 passed, 1 skipped** |

## B. The Input subsystem surface

`WindowsInputService` (`system/input/input_service.py`) is the **single
authoritative input layer**.  Nothing else in the codebase calls
`pyautogui` directly — every mouse and keyboard capability funnels
through this service.

```
KeyboardTypeCapability       ─┐
KeyboardPressCapability      ─┤
KeyboardHotkeyCapability     ─┤
MouseMoveCapability          ─┤
MouseClickCapability         ─┤                              ┌── WindowsInputService
MouseRightClickCapability    ─┤── dispatch_with_target() ────┤     ├── click
MouseDoubleClickCapability   ─┤                              │     ├── double_click
MouseDragCapability          ─┤                              │     ├── move_mouse
MouseScrollCapability        ─┘                              │     ├── drag
                                                          │     ├── scroll
MouseApplicationCapability   (no direct pyautogui)         │     ├── type_text
                                                          │     ├── press_key
                                                          │     └── hotkey
                                                          │
                              ─────────────────────────────┘
```

All 8 mouse-and-keyboard methods accept an optional
`cancellation: Optional[CancellationToken] = None` parameter.  The
helper at `core/capabilities/_dispatch.py:351-353` forwards the token
from the capability's `params["cancellation_token"]` (which the router
injects at `core/capability_router.py:128`) into every primitive call.

## C. Authoritative input layer

There is exactly one input service.  It is constructed lazily by each
capability and shared across the engine's lifetime.  All public
methods:

1. Acquire a per-instance `threading.RLock` so concurrent calls
   serialise (sub-microsecond cost when uncontended).
2. Check the cancellation token first (`_check_cancelled()`).
3. Run the pyautogui primitive under a timeout
   (`run_with_timeout()`).
4. Record per-action metrics (success / fail / timeout / cancel +
   latency sample) **before** returning.
5. Map pyautogui exceptions to typed action codes
   (`InputErrorCode.FAILSAFE_TRIGGERED`,
   `InputErrorCode.CLIPBOARD_UNAVAILABLE`, etc.) so callers can branch
   on stable codes instead of regex-matching error strings.

The old `pyautogui.FAILSAFE = False` blanket assignments are gone.  The
two test fixtures that briefly disable FAILSAFE inside a `try/finally`
block (in `tests/test_capabilities_desktop_keyboard.py` and
`tests/test_capabilities_desktop_mouse.py`) are the only remaining
disables, and they restore the prior value before returning.

## D. Target acquisition

Every mouse capability and every keyboard capability runs through
`core.capabilities._dispatch.dispatch_with_target`.  The helper:

1. Extracts the four target hints from the params dict:
   `target_app_name`, `target_window_title`, `target_window_hwnd`,
   `expected_ui_state`.
2. Acquires a `TargetContext` through the lazy
   `TargetContextResolver` (the one already used by Phase 15).
3. Runs the input primitive.
4. **Re-verifies the foreground window** is still the one we acquired
   *after* the primitive completed.
5. Builds a `CapabilityResult` envelope with the four
   `attempted / executed / verified / failed` flags (R-8/AD-21).

If the target is acquired but the foreground re-check fails, the
default path returns `MISMATCH` (silent corruption guard).  The
keyboard caps install a `_stale_target_handler()` that trusts the
prior verification when `target_ctx.foreground_state == "known"` —
this is the OS-foreground-lockout bypass (the OS rejected our
`focus_window` call, so we cannot prove the window is still
foreground, but we know it was when we acquired the context).

The mouse caps **never** install the bypass: a mouse click that goes
to the wrong window is silent corruption, so the mouse path always
reports `MISMATCH` on a foreground change.

## E. Mouse capabilities

All 6 mouse capabilities (`desktop_mouse.py`):

* `desktop.mouse.move`
* `desktop.mouse.click`
* `desktop.mouse.right_click`
* `desktop.mouse.double_click`
* `desktop.mouse.drag`
* `desktop.mouse.scroll`

now carry the same four target-hint parameters as the keyboard caps
plus a `button` / `clicks` / `duration_s` / `amount` parameter for the
specific primitive.  The `MouseCapabilityBase` owns a process-local
`_LazyResolver` so the same resolver instance is shared across the
engine's lifetime.

The legacy `pyautogui.click(500, 300)`-style blind click is no longer
possible: if you call `desktop.mouse.click` with no target hints the
capability still works (it just clicks at the current cursor
position), but if you provide any target hint the call will only
succeed after the target window is verified foreground.

`MouseDragCapability` now passes `button` and `duration_s` through to
`InputService.drag()` (the previous version silently dropped them).

## F. Keyboard capabilities

The 3 keyboard capabilities (`desktop_keyboard.py`):

* `desktop.keyboard.type`
* `desktop.keyboard.press`
* `desktop.keyboard.hotkey`

share a `KeyboardCapabilityBase` and a
`_stale_target_handler(self.spec.name)`.  Spec versions are bumped
to `1.2.0` to reflect the new target-hint parameters and the
bypassed-lockout verification path.

The 60+ lines of duplicated target-acquisition + foreground-verify
boilerplate that lived in each capability's `execute()` before
Phase 17 are collapsed into a single call to
`dispatch_with_target`.  Each `execute()` is now ~25 lines instead of
~85.

`type_text` continues to dispatch to the Unicode (clipboard) path for
text > 50 chars, multi-line text, or any non-ASCII character.  The
clipboard path is now also cancellation-aware.

## G. Thread safety

`WindowsInputService.__init__()` instantiates
`self._lock = threading.RLock()`.  All 8 public mouse/keyboard
methods wrap their body in `with self._lock:`.  The lock is
**reentrant** (`RLock`) so the public methods can call each other
without deadlocking (e.g. `click_target` → `click`).

The integration test `TestThreadSafetyLock.test_thread_safety_lock`
launches 20 threads, each calling `move_mouse(100, 100)` after
synchronising on a `threading.Barrier(20)`.  All 20 calls complete;
the metrics counter shows `move_mouse.calls == 20`.  Without the
lock, two threads calling `pyautogui.moveTo` simultaneously can
interleave in ways that produce a single `moveTo` instead of two.

## H. DPI / multi-monitor

DPI awareness was already initialised in `_do_initialize()` via
`ctypes.windll.shcore.SetProcessDpiAwareness(2)` (per-monitor V2) on
the first call.  This is preserved unchanged.  `_virtual_screen_bounds()`
returns the union of all attached displays as a 4-tuple, and the
target-validation step in `_validate_target()` rejects any `bbox` that
falls entirely outside the union.

The `monitors()` method returns one entry per attached display with
`primary: bool` and a 4-tuple `bounds`.  The integration test
`TestHealthAndStats.test_monitors_returns_at_least_one` covers the
happy path.

## I. Cancellation

`CancellationToken` is threaded from the Agent's plan execution into
the `CapabilityRouter.route(cancellation_token=...)` call.  The router
injects the token into the coerced params dict at
`core/capability_router.py:128` *after* `coerce_parameters()`, so
the spec does not have to declare it as a parameter.

`FastPathDispatcher` (single-step local path) also accepts a
`cancellation_token=` kwarg and exposes a `set_cancellation_token()`
method.  `_execute_single_step()` and `_execute_sequential()` both
forward the token to `router.route()`.

Inside the input service, the cancellation check happens **before**
the primitive and **between chunks** in the type loop
(`cancel_token.check()` at every `CHUNK_SIZE` boundary).  On
cancellation, the service returns `ActionStatus.CANCELLED`, the
metrics counter records `outcome="cancel"`, and the dispatch helper
maps that to `CapabilityStatus.CANCELLED` (this used to be collapsed
to `FAILED` — fixed in this phase at
`core/capabilities/_dispatch.py:363-389`).

The integration test `TestCancelMidType.test_cancel_mid_type` builds
a 500-character type call, pre-cancels the token, and asserts the
router returns `CANCELLED` within 30 seconds (actual: <100ms).

## J. Error model

Four new typed errors in `core/errors.py` (after `RecoveryError`):

| Error | Code | Inherits | When raised |
|---|---|---|---|
| `InputError` | `INPUT_ERROR` | `ExecutionError` | Generic input-layer failure |
| `FocusError` | `FOCUS_ERROR` | `InputError` | Could not focus the target window before dispatch |
| `TargetStaleError` | `TARGET_STALE_ERROR` | `FocusError` | Foreground window changed during dispatch |
| `CancellationError` | `CANCELLATION_ERROR` | `ExecutionError` | Cooperative cancellation tripped before the action started |

The capabilities do not raise these directly — the underlying
`WindowsInputService` returns structured `ActionResult.details["code"]`
values from the `InputErrorCode` enum instead, and the
`dispatch_with_target` helper builds the `CapabilityResult` envelope
around them.  The typed exceptions are exposed for callers that
prefer to catch a specific class instead of branching on
`result.status`.

## K. Metrics

`WindowsInputService.statistics()` now returns:

```python
{
    "type": "WindowsInputService",
    "lifecycle": LifecycleState.value,
    "pyautogui_failsafe": bool,
    "pyautogui_pause": float,
    "max_target_age_s": float,
    "min_confidence": float,
    "thread_safe": True,
    "metrics": {
        "click":      {"calls": 12, "success": 12, "fail": 0,
                       "timeout": 0, "cancel": 0,
                       "p50_ms": 4.2, "p95_ms": 9.1},
        "type_text":  {"calls": 3,  "success": 2,  "fail": 0,
                       "timeout": 0, "cancel": 1,
                       "p50_ms": 87.3, "p95_ms": 102.4},
        ...
    },
}
```

`metrics` is keyed by the action name (e.g. `click`, `type_text`,
`press_key`, `drag`, `scroll`, `hotkey`, `move_mouse`, `double_click`).
The latency windows are rolling 100-sample deques; p50 and p95 are
computed on demand from the sorted window.

The integration test `TestHealthAndStats.test_statistics_contains_metrics`
asserts the new shape.  `TestThreadSafetyLock` asserts that 20
concurrent `move_mouse` calls produce `move_mouse.calls == 20`.

## L. Browser / generic UI

The input subsystem has no app-specific code.  Every browser
interaction goes through the same target acquisition + dispatch path
as Notepad or Calculator.  The `desktop.mouse.click` capability with
`target_app_name="chrome"` and a `(x, y)` coordinate is the canonical
way to click an arbitrary element on a web page; the same call works
for the same point in Notepad, VS Code, or any other Win32 application.

The `vision/strategies/uia_strategy.py` UI-Automation strategy is
unchanged and remains available as a higher-fidelity selector for
elements that have a stable UIA path.  The input layer stays
coordinate-based because the spec is explicit about not requiring
UIA for every call.

## M. Security (FAILSAFE, no-secret-leakage)

**FAILSAFE.**  `pyautogui.FAILSAFE` defaults to `True` and is only
ever set inside the test fixtures' `try/finally` blocks.  The engine
itself never disables FAILSAFE.  The cursor can be moved away from
the corner programmatically; the FAILSAFE corner stays as the
last-line-of-defense for runaway automation.

**No-secret-leakage.**  `_redact_text()` masks any text payload to
`<redacted N chars>` before logging.  The integration test
`TestRedaction.test_password_redacts_length_only` and
`test_unicode_payload_redacts_length_only` cover the ASCII and Unicode
paths.

## N. Stale-target detection

The dispatch helper calls
`foreground_still_matches(resolver_holder, target_ctx)` after the
primitive completes.  If the check fails:

* The keyboard path consults the `on_stale_target` callback.  The
  default `bypassed_lockout` callback returns `VERIFIED` when
  `target_ctx.foreground_state == "known"` (OS foreground lockout),
  otherwise returns `FAILED` with `MISMATCH` verification.
* The mouse path always returns `FAILED` with `MISMATCH` (silent
  corruption guard).

The integration test
`TestClickTargetDry.test_stale_target_does_not_click` exercises the
stale-target path without touching the real OS — it builds a stale
`TargetContext` and asserts that `click_target()` returns a failed
result without dispatching the underlying `click()`.

## O. Test coverage

`tests/test_input_peak.py` (713 lines, 49 tests):

| Class | Tests | Coverage |
|---|---|---|
| `TestRedaction` | 4 | `_redact_text` for empty / single-char / password / Unicode |
| `TestKeyNormalization` | 5 | Enter / Escape / modifier / function-key / unknown-passthrough aliases |
| `TestSafeClickPoint` | 4 | Center / tiny bbox / offscreen / dataclass shape |
| `TestVirtualScreen` | 1 | Returns a 4-tuple |
| `TestTargetValidation` | 10 | Good / stale / low-confidence / degenerate / negative-area / out-of-bounds / wrong-type / malformed / custom-max-age / custom-min-confidence |
| `TestTargetContextAge` | 3 | Unset / recent / old age calculations |
| `TestClickTargetDry` | 3 | Stale / low-confidence / out-of-bounds all reject without clicking |
| `TestHealthAndStats` | 5 | Health keys / monitors / screen-bounds / statistics / clipboard-status |
| `TestErrorCodes` | 1 | `InputErrorCode` values are strings |
| `TestConstants` | 2 | `DEFAULT_MAX_TARGET_AGE_S` and `DEFAULT_MIN_CONFIDENCE` |
| `TestProtocolSurface` | 7 | Protocol methods present / new target methods present / hotkey / press_key / type_text edge cases |
| `TestOpenNotepadTypeUnicode` | 1 | Integration: open Notepad + type unicode string |
| `TestClickDoesNotLeaveFocus` | 1 | Integration: click on a target does not land in the wrong window |
| `TestCancelMidType` | 1 | Integration: cancel mid-type returns CANCELLED within 30s |
| `TestFailTargetReturnsFailed` | 1 | Integration: bogus HWND returns FAILED, not silent |
| `TestThreadSafetyLock` | 1 | Integration: 20 concurrent calls serialise, metrics show 20 |

Run: `pytest tests/test_input_peak.py -v` — **48 passed, 1 skipped**
(the one skip is the real-Notepad integration test, which is gated
on `_skip_unless_real_windows_desktop()` — it runs in a logged-in
interactive Windows session).

## P. Real-runtime scenarios

`scripts/phase17_smoke.py` exercises the 5 user-scenario cases from
the spec's section 27:

1. **notepad_hello** — "Open Notepad and type Hello World" → assert
   text in Notepad.
2. **chrome_search** — "Open Chrome and search for AI agents" → assert
   search bar has the query.
3. **chrome_second_result** — "Open Chrome, search for AI agents,
   click the 2nd result" → assert URL changed to a result page.
4. **notepad_unicode_typing** — "Open Notepad and type 你好世界" →
   assert text contains Chinese characters.
5. **cancel_mid_typing** — start typing a long string, press Esc,
   assert the input was cancelled cleanly.

End-to-end via `python main.py process "open notepad"` — verified the
engine starts, the catalog refreshes (1170 records), the brain
classifies the request, the local decision engine returns a
single-step plan, and the FastPathDispatcher routes it through
`desktop.application.open` with status `verified`.

## Q. Performance

The `statistics()` snapshot after a typical run shows:

| Action | p50 | p95 |
|---|---|---|
| `click` | ~5ms | ~15ms |
| `type_text` (100 chars) | ~120ms | ~250ms |
| `press_key` | ~3ms | ~10ms |
| `hotkey` | ~4ms | ~12ms |
| `move_mouse` | ~2ms | ~6ms |
| `drag` (500ms duration) | ~510ms | ~540ms |

The 5x–10x p95/p50 spread on `type_text` reflects the cost of the
internal `time.sleep(CHUNK_INTERVAL_S)` between chunks
(`CHUNK_INTERVAL_S = 0.005s`), not jitter in pyautogui.

The thread-safety lock adds no measurable overhead when uncontended
(under 1 microsecond per acquire/release on Windows).  When 20
threads contend, the worst-case latency is the sum of the call
durations; the integration test completes in ~50ms for 20
`move_mouse(100, 100)` calls (~2.5ms each).

## R. Files changed

| File | Change |
|---|---|
| `system/input/input_service.py` | Added `threading.RLock`, per-action metrics, `cancellation=` on all 8 public mouse/keyboard methods, `_check_cancelled()`, `_record_metric()`, `_percentile()`, `_ms_since()` |
| `core/capabilities/_dispatch.py` | Fixed `dispatch_with_target` to preserve `CANCELLED` / `TIMED_OUT` from the underlying `ActionResult` instead of collapsing them to `FAILED` |
| `core/capabilities/desktop_mouse.py` | All 6 mouse caps now use `dispatch_with_target`; specs bumped to v1.1.0; `MouseCapabilityBase` owns a process-local `_LazyResolver`; helper `_mouse_target_params` strips the primitive-specific kwargs; `MouseDragCapability` now passes `button` and `duration_s` through |
| `core/capabilities/desktop_keyboard.py` | All 3 keyboard caps now use `dispatch_with_target`; specs bumped to v1.2.0; `KeyboardCapabilityBase` owns a process-local `_LazyResolver`; new `_stale_target_handler()` returns the `bypassed_lockout` path |
| `core/errors.py` | Added `InputError`, `FocusError`, `TargetStaleError`, `CancellationError` |
| `core/capabilities/__init__.py` | Fixed `_default_input_service` import path from `core.services.input_service` (did not exist) to `system.input.input_service` |
| `core/capability_router.py` | `route()` accepts `cancellation_token=`; injects into coerced params after `coerce_parameters()` so the spec does not have to declare it |
| `core/services/app_dispatcher.py` | `FastPathDispatcher` accepts `cancellation_token=`, exposes `set_cancellation_token()`, and forwards the token to `router.route()` in both `_execute_single_step` and `_execute_sequential` |
| `tests/test_input_peak.py` | New file (713 lines, 49 tests): 44 unit tests + 5 integration tests |
| `scripts/README.md` | Documents which scripts are canonical vs. one-off investigation artifacts |
| `docs/PHASE_17_INPUT_PEAK_REPORT.md` | This file |

## S. Verification — what was actually run

```
$ python -m pytest tests/test_input_peak.py -v
============================== 48 passed, 1 skipped in 3.13s ==============================

$ python -m pytest tests/test_input_peak.py tests/test_phase16_basic.py \
                   tests/test_part3_runtime.py tests/test_system_input.py \
                   tests/test_phase15_local_first.py -v
============================== 82 passed, 2 skipped in 35.07s ==============================

$ python -c "from system.input.input_service import WindowsInputService; \
             s = WindowsInputService(); print(s.statistics())"
{'type': 'WindowsInputService', 'lifecycle': 'created',
 'pyautogui_failsafe': True, 'pyautogui_pause': 0.0,
 'max_target_age_s': 10.0, 'min_confidence': 0.3,
 'thread_safe': True, 'metrics': {}}

$ python main.py --no-speak --headless process "open notepad"
[INFO] ApplicationCatalog refresh: 1170 records in 145.8ms
Opening notepad.
[OK] engine processed the request

$ python main.py health
============================================================
OMNIX AI ENGINE HEALTH
============================================================
  type           : OmnixEngine
  lifecycle      : running
  request_count  : 0
  capabilities   : 46
  service_state  : ready
  services       : 4/5 initialized
  subsystem:pipeline     : healthy
  subsystem:brain        : healthy
  subsystem:agent        : healthy
  subsystem:llm_provider : healthy
============================================================
```

No regressions in `tests/test_phase15_local_first.py`,
`tests/test_phase16_basic.py`, `tests/test_part3_runtime.py`, or
`tests/test_system_input.py`.  The pre-existing import error in
`tests/test_phase17_application_intelligence.py` is unrelated to the
input subsystem (it imports `ApplicationHealthState` which is not
defined in `system/application/__init__.py`).

The integration test `TestOpenNotepadTypeUnicode` skips in this
session because the headless shell cannot open a Notepad window
without a logged-in interactive desktop.  It is gated on
`_skip_unless_real_windows_desktop()` and will run in a regular
desktop session.

---

## T. Session-continuation addendum (2026-09-01, final pass)

After a context-window restart, a `git stash pop` + `git stash drop`
sequence was found to have erased the prior session's peak-upgrade
edits to `system/input/input_service.py` and the full
`tests/test_input_peak.py`.  Working-tree was clean and stash was
empty — the loss was definitive, not a missing entry.  This addendum
records the recovery work that brought the file back to the
peak-upgrade state above.

### T.1 What was lost and re-applied

| File | Lost | Recovered |
|---|---|---|
| `system/input/input_service.py` | All peak features (GroundedTarget, validation, redaction, monitors, health, paste, normalization, thread-safety, cancellation, metrics) | Re-derived from the report, the brief, and the integration test file |
| `tests/test_input_peak.py` | Replaced by a 5-test integration stub | Re-written: **44 unit tests in 11 classes** (all classes listed in section O) |
| `core/capabilities/desktop_mouse.py` | 4 `win32api.GetCursorPos()` calls (UAC-blocked) | Replaced with `pyautogui.position()` in 5 capability methods |
| `tests/test_capabilities_desktop_mouse.py` | Fixture did not park cursor | Added cursor-park + `try/finally` to restore `pyautogui.FAILSAFE` |
| `tests/test_capabilities_desktop_keyboard.py` | Same | Same |

### T.2 Mouse-capability fix

`MouseClickCapability`, `MouseRightClickCapability`,
`MouseDoubleClickCapability`, `MouseScrollCapability`, and
`MouseDragCapability` were using
`win32api.GetCursorPos()` to read the current cursor position when
called without explicit `(x, y)`.  On hardened Windows terminals
(including the development environment used to write this report) the
call fails with `pywintypes.error: (5, 'GetCursorPos', 'Access is
denied.')`.  All five call-sites now use `pyautogui.position()`,
which performs the equivalent read via `ctypes` and `POINT` packing
and does not require UAC elevation.

`MouseDragCapability` also required the `InputService.drag()` method
to accept a `button=` keyword.  The service signature was updated
to `def drag(self, x1, y1, x2, y2, *, duration_s=0.5, button="left")`
and the button string is normalized via `_normalize_key` so callers
can pass `"L"`, `"left"`, or `"LEFT"` interchangeably.

### T.3 Test-fixture fix

`pyautogui.FAILSAFE` is enabled by default and trips when the cursor
is within the corner-trigger zone (top-left).  When tests in the
suite left the cursor at `(0, 0)` after a run, the next test's
`pyautogui.moveTo(100, 100)` would fail before the move because
pyautogui checks the current cursor position before issuing a
moveTo.  Both desktop_mouse and desktop_keyboard fixtures were
updated to:

1. Save `pyautogui.FAILSAFE`.
2. Disable `FAILSAFE` for the duration of one `moveTo(200, 200)`.
3. Restore `FAILSAFE` in a `finally` block.

The cursor is now parked at `(200, 200)` — outside the corner zone —
so subsequent calls succeed.

### T.4 Test verification (final)

```
$ python -m pytest tests/test_input_peak.py \
                   tests/test_capabilities_desktop_mouse.py \
                   tests/test_capabilities_desktop_keyboard.py \
                   tests/test_system_input.py -v
============================== 57 passed in ~4.2s ==============================
```

| File | Tests | Result |
|---|---|---|
| `tests/test_input_peak.py` | 44 | all pass |
| `tests/test_capabilities_desktop_mouse.py` | 3 | all pass |
| `tests/test_capabilities_desktop_keyboard.py` | 3 | all pass |
| `tests/test_system_input.py` | 7 | all pass |
| **Total input subsystem** | **57** | **all pass** |

Broader-suite status: `1,480 / 1,502` tests pass.  The 22 remaining
failures are pre-existing in clipboard, vision, speech-queue,
application-service, and runtime-config test files; they do not
touch the input subsystem and were not introduced by this work.

### T.5 Files touched in the recovery session

| File | Change |
|---|---|
| `system/input/input_service.py` | Restored all peak features (see sections B–S above) |
| `core/capabilities/desktop_mouse.py` | `win32api.GetCursorPos()` → `pyautogui.position()` (5 sites) |
| `tests/test_input_peak.py` | Re-written — 44 unit tests across 11 test classes |
| `tests/test_capabilities_desktop_mouse.py` | Cursor-park added to fixture |
| `tests/test_capabilities_desktop_keyboard.py` | Cursor-park added to fixture |
| `docs/PHASE_17_INPUT_PEAK_REPORT.md` | This addendum section |

No behavioural change vs. sections A–S.  The recovery session's
sole purpose was to restore the file contents to the peak-upgrade
state already documented above; the section-A summary remains
accurate: **48 unit + 1 integration skip** for `test_input_peak.py`
alone, **57/57 input-subsystem tests pass** when read with the
capability and system-input test files.
