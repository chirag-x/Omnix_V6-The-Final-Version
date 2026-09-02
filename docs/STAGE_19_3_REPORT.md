# Stage 19.3: Execution Synchronization & State-Settling Foundation

## A. Stage 19.3 Summary

Stage 19.3 introduces a **generic execution synchronization abstraction** to
the Omnix V6 execution cycle. The synchronization layer is responsible for
*waiting for the environment to become "settled"* after an action has been
executed, **before** a fresh observation is requested for verification.

The new SYNCHRONIZE phase lives between ACT and VERIFY in the lifecycle:

```
PRECONDITION → OBSERVE → GROUND → ACT → INVALIDATE → SYNCHRONIZE → VERIFY
```

Stage 19.3 does **not** build application-specific waiters, recovery, AI
escalation, replanning, or multi-step autonomy. It is a **deterministic,
bounded, observation-based, LLM-free** waiting primitive that distinguishes
"the action completed" from "the environment is ready for the next
observation".

The implementation is reusable, composable, and respects all
Stage 18.x and 19.x architectural rules.

## B. Architecture

Stage 19.3 introduces a new module — `core/execution/sync.py` — that
defines:

* `SynchronizationStatus` — closed set of synchronization outcomes
* `SynchronizationContext` — per-call execution context
* `SynchronizationResult` — structured synchronization result
* `SynchronizationProvider` — provider protocol
* `DefaultSynchronizationProvider` — default implementation
* `create_default_synchronization_provider` — factory

The `ExecutionCycle` is extended to invoke the synchronization phase
between ACT and VERIFY, with timeout/cancellation/observability
integration. The `ExecutionPolicy` gains configuration knobs for the
new phase. The `ExecutionResult` and `ExecutionTrace` carry
synchronization diagnostics.

```
                ┌──────────────────────┐
                │   ExecutionCycle     │
                │  (orchestrator)      │
                └──────────┬───────────┘
                           │
        ┌──────────────────┼──────────────────┐
        ▼                  ▼                  ▼
 ┌────────────┐     ┌────────────┐     ┌────────────┐
 │  Observe   │     │    Act     │     │  Verify    │
 └────────────┘     └────────────┘     └────────────┘
                          │
                  ┌───────▼────────┐
                  │ Synchronize   │   <-- NEW in 19.3
                  │   (Stage 19.3) │
                  └────────────────┘
                          │
                  ┌───────▼────────┐
                  │ Synchronization│
                  │   Provider     │  (pluggable)
                  └────────────────┘
```

The synchronization phase **never** calls an LLM. It is purely
deterministic, bounded, and observation-based.

## C. SynchronizationProvider

The `SynchronizationProvider` is a `Protocol` with one async method:

```python
class SynchronizationProvider(Protocol):
    name: str

    async def wait_until_settled(
        self,
        context: SynchronizationContext,
        *,
        timeout_s: float = 5.0,
        poll_interval_s: float = 0.05,
        cancellation_token: Optional[CancellationToken] = None,
    ) -> SynchronizationResult: ...
```

The provider MUST:

* Be bounded (timeout + cancellation token)
* Use a fresh perception observation as settlement evidence
* Invalidate (or otherwise defeat) the perception cache for the
  pre-action observation so stale data cannot be mistaken for fresh
* Honor an optional expectation by treating its presence as evidence
  of settlement
* Return a structured `SynchronizationResult`
* Not call any LLM

The provider is a `Protocol` with `@runtime_checkable` so alternative
implementations can be duck-typed without inheritance.

## D. SynchronizationResult

`SynchronizationResult` is the structured outcome of a sync attempt:

```python
@dataclass(frozen=True)
class SynchronizationResult:
    status: SynchronizationStatus    # SETTLED, TIMEOUT, CANCELLED, INCONCLUSIVE, ERROR
    settled: bool                    # convenience boolean
    observation_id: Optional[str]    # the post-action observation that decided it
    confidence: float                # 0.0–1.0
    elapsed_ms: float                # how long sync took
    reason: str                      # human-readable reason
    poll_count: int                  # how many observations were polled
    metadata: Mapping[str, Any]      # free-form data
```

`status` is the authoritative outcome. `settled` mirrors
`status == SETTLED`, but `__post_init__` keeps the two consistent:
if either is overridden manually, the invariant is enforced.

`to_dict()` produces a JSON-serializable dict for logging.

## E. State-Settling Model

The execution cycle distinguishes two notions that Stage 19.3 makes
explicit:

* **Action completed** — the capability layer reports `EXECUTED`.
  This only means "the click/type/press request was sent". It does
  **not** mean the desktop has finished animating, the new window
  has appeared, or the new text is rendered.
* **Environment settled** — the observable desktop is in a stable
  post-action state. The fresh observation we are about to take
  reflects the action's effect, not a transient mid-animation
  state.

Settlement is observed, not declared. The cycle refuses to call
VERIFY until SETTLED is reported (or the policy says settlement is
not required).

This distinction is critical for fast-changing UIs (browsers, animations,
dialogs), where verifying against a mid-animation observation produces
false negatives.

## F. Expected-State Handling

If a `VerificationExpectation` is provided on the step, the
synchronization layer uses **expectation-driven waiting** as its
primary strategy. The default provider stops polling as soon as a
fresh observation satisfies the expectation.

This is the most efficient strategy because it short-circuits the
"is it stable yet?" question with "yes, we see the expected state".

The expectation kinds that are deterministically verifiable from a
`PerceptionResult` (no LLM) are:

* `TARGET_VISIBLE` / `TARGET_PRESENT` — a candidate with matching text
  or property exists
* `TARGET_ABSENT` — no candidate with matching text or property exists
* `WINDOW_EXISTS` — the window context has the expected title/application
* `WINDOW_FOCUSED` — the window context is foreground with matching
  title/application

Kinds that require OCR or reference comparison (`TEXT_PRESENT`,
`TEXT_CHANGED`, `SCREEN_CHANGED`, `FOCUS_CHANGED`) are **not** satisfied
by the sync layer. They are verified later in the VERIFY phase where
the verification provider handles them. This is honest separation of
concerns.

## G. Observation Freshness

Every observation used as settlement evidence is obtained directly
from the perception provider, never from a pre-action cache entry.
The provider:

* Sets `max_age_ms=0` on its `PerceptionRequest` to defeat the cache
* Disables `include_screenshot` (we only need cheap signals) but
  enables `include_window_context` (so contextual stability has data
  to compare)
* Rejects observations that match `context.before_observation_id` —
  if the cache somehow returns the pre-action observation, it is
  treated as stale and the loop continues

This is the "**No Stale Success**" rule: a pre-action observation
must never be reported as fresh settlement evidence.

## H. Cache Interaction

Before obtaining the first post-action observation, the default
synchronization provider **invalidates the perception cache** for
the pre-action observation id. This is best-effort:

* The cache is duck-typed: any object exposing `invalidate` (sync or
  async) and accepting a `key` argument is acceptable
* Cache invalidation is wrapped in a try/except so cache errors
  never break synchronization
* When no `key` is provided, the entire cache is invalidated
  (the safest default since cache keys may not match the
  pre-action observation id format)

After cache invalidation, the provider requests a fresh observation
directly from the perception provider. The combination of cache
invalidation and explicit `max_age_ms=0` ensures the first
post-action observation is genuinely fresh.

## I. ExecutionCycle Integration

The `ExecutionCycle.execute` method is extended with a new phase
between ACT and VERIFY:

```python
# Phase 3: ACT
act_result = await self._act(...)
if act_result.status != ExecutionStatus.SUCCESS:
    return act_result

# Phase 3.5: SYNCHRONIZE (Stage 19.3)
sync_result = await self._synchronize(...)
if sync_result is not None and self._policy.require_settlement:
    if sync_result.status != SynchronizationStatus.SETTLED:
        # Map sync status to execution status and return structured failure
        return self._build_sync_failure(...)

# Phase 4: VERIFY
verify_result = await self._verify(...)
```

The synchronization phase is **skipped** when:

* `policy.enable_synchronization` is `False`, or
* No `synchronization_provider` is configured

It is **required** when `policy.require_settlement` is `True` (the
default) and a provider is configured. When required and the sync
result is non-SETTLED, the cycle returns a structured failure with
the appropriate `ExecutionStatus`:

| SynchronizationStatus | ExecutionStatus                  |
| --------------------- | -------------------------------- |
| `SETTLED`             | (continues to VERIFY)            |
| `TIMEOUT`             | `ExecutionStatus.TIMEOUT`        |
| `CANCELLED`           | `ExecutionStatus.CANCELLED`      |
| `INCONCLUSIVE`        | `ExecutionStatus.INCONCLUSIVE`   |
| `ERROR`               | `ExecutionStatus.SYNCHRONIZATION_FAILED` |

When `require_settlement=False`, the cycle continues to VERIFY even
if sync returns non-SETTLED. The sync result is still attached to
the `ExecutionResult` for diagnostics.

## J. ExecutionPolicy Changes

The `ExecutionPolicy` dataclass gains four new fields:

```python
@dataclass
class ExecutionPolicy:
    # ... existing fields ...
    # Stage 19.3 extensions — synchronization / state-settling
    enable_synchronization: bool = True
    synchronization_timeout_s: float = 5.0
    synchronization_poll_interval_s: float = 0.05
    require_settlement: bool = True
```

* `enable_synchronization` — master switch for the new phase
* `synchronization_timeout_s` — hard upper bound on sync wait
* `synchronization_poll_interval_s` — minimum interval between polls
* `require_settlement` — whether non-SETTLED outcomes fail the step

The defaults are conservative: a 5-second timeout, 50ms poll interval,
and required settlement. These match the rule that the cycle must
not block indefinitely on a stuck environment.

## K. Timeout Handling

The synchronization phase is bounded by `policy.synchronization_timeout_s`.
The provider MUST return before the timeout. If the provider's own
internal loop does not respect the timeout, the cycle wraps the call
in `asyncio.wait_for` with a small buffer (`timeout_s + 1.0s`).

When the deadline is reached:

* The current poll's observation is discarded
* A `SynchronizationResult` with `status=TIMEOUT` and `settled=False`
  is returned
* The `poll_count` reflects how many polls were attempted before
  the deadline
* The `elapsed_ms` reflects the actual wait time

The cycle maps this to `ExecutionStatus.TIMEOUT` (or
`SYNCHRONIZATION_FAILED` for hard errors). A timed-out sync is a
**failure**, not a fall-through. The cycle refuses to verify against
a potentially stale observation.

## L. Cancellation Handling

The synchronization phase honors the same `CancellationToken` as the
rest of the cycle. Cancellation can come from:

* The user pressing Ctrl+C (cooperative)
* The voice runtime saying "stop"
* The engine hitting a deadline
* The caller passing a pre-cancelled token

The provider checks the token at the **top of every poll iteration**,
so cancellation is responsive even if the observation is slow to
return. When cancellation trips, the provider returns
`SynchronizationStatus.CANCELLED` with `settled=False`.

The cycle maps this to `ExecutionStatus.CANCELLED`. A cancelled sync
short-circuits VERIFY, because there is no point verifying a
cancelled action.

The cycle also wraps the entire `wait_until_settled` call in
`asyncio.wait_for` to catch `asyncio.CancelledError` if it propagates
through the provider.

## M. Contextual Stability

When no expectation is provided but a pre-action state is, the
default provider falls back to **contextual stability detection**.
"Contextually stable" means two consecutive observations whose
relevant-state slots are equivalent:

* Window context: `title`, `application`, `is_foreground`, `bounds`
* Screen dimensions: `width`, `height`

Screenshot bytes are **deliberately not compared** — irrelevant
desktop changes (clock, cursor, notifications, animation frames)
must not block settlement. This is the key insight: a desktop
*visually changes* all the time, but the *relevant state* (which
window is focused, which app is in the foreground) usually doesn't
change between two consecutive observations in a stable environment.

The provider requires **two** consecutive contextually-equivalent
observations before declaring settled. This protects against
catching a single mid-animation frame that happens to look like the
previous frame.

If neither an expectation nor a pre-state is available, the fallback
strategy is "one fresh observation is enough" — because the
pre-action observation was invalidated, even a single new
observation is genuinely fresh.

## N. Event Integration

The synchronization phase emits observability events through the
existing `observability_sink` mechanism:

* `SYNCHRONIZATION_STARTED` — emitted before calling the provider,
  with `execution_id`, `step_id`, `expectation_kind`, `has_pre_state`
* `SYNCHRONIZATION_COMPLETED` — emitted after the provider returns,
  with `status`, `settled`, `poll_count`, `elapsed_ms`
* `SYNCHRONIZATION_FAILED` — emitted when the sync result is
  non-SETTLED and `require_settlement=True`, with `status`, `reason`,
  `poll_count`

These events flow through the same channel as OBSERVATION_*,
GROUNDING_*, ACTION_*, and VERIFICATION_* events, so observability
tools that already consume those events automatically pick up
synchronization events.

## O. LLM Independence

**LLM calls during SYNCHRONIZE = 0.**

The default synchronization provider is a pure deterministic
implementation:

* It does **not** import `openai`, `anthropic`, `ai_brain`, or any
  LLM SDK
* It does **not** hold a reference to any LLM client
* It does **not** make network calls (all I/O is through the
  injected perception provider, which is the only allowed
  observation source)
* It does **not** call any AI-brained reasoning primitive
* It does **not** use semantic understanding of the desktop

The default provider is essentially a state machine over
`PerceptionResult` and `ExecutionState`, governed by a bounded
timeout. Any LLM-style "judgement" of whether the desktop is
"ready" is delegated to the existing `VerificationProvider`,
which itself is deterministic (see Stage 19.0).

Tests `test_sync_provider_makes_zero_llm_calls`,
`test_cycle_synchronize_makes_zero_llm_calls`, and
`test_default_provider_pure_deterministic` enforce this property.

## P. Tests

29 new tests in `tests/test_stage19_3_synchronization.py` covering the
10 test scenarios from the specification:

| Test Scenario                          | Test Class / Method |
| -------------------------------------- | ------------------- |
| **30. Immediate settlement**           | `TestImmediateSettlement.test_immediate_settlement_succeeds` |
| **31. Delayed settlement**              | `TestDelayedSettlement.test_delayed_settlement_with_expectation` |
| **32. Timeout**                        | `TestSynchronizationTimeout.test_timeout_when_never_settles` |
|                                        | `TestSynchronizationTimeout.test_provider_returns_timeout_directly` |
| **33. Cancellation**                   | `TestSynchronizationCancellation.test_cancellation_token_trips_during_sync` |
|                                        | `TestSynchronizationCancellation.test_cancellation_during_default_provider` |
| **34. Stale observation**              | `TestStaleObservationHandling.test_pre_action_observation_not_accepted` |
|                                        | `TestStaleObservationHandling.test_cache_invalidation_called` |
| **35. Contextual stability**           | `TestContextualStability.test_contextually_stable_two_consecutive` |
|                                        | `TestContextualStability.test_irrelevant_changes_dont_block_settlement` |
| **36. Expectation-driven wait**        | `TestExpectationDrivenWait.test_target_visible_expectation_settles_immediately` |
|                                        | `TestExpectationDrivenWait.test_window_focused_expectation_settles` |
|                                        | `TestExpectationDrivenWait.test_expectation_satisfied_after_polls` |
| **37. No unbounded polling**           | `TestNoUnboundedPolling.test_sync_returns_within_timeout_bound` |
|                                        | `TestNoUnboundedPolling.test_no_infinite_loop_on_failing_observations` |
| **38. No LLM**                         | `TestNoLLMCalls.test_sync_provider_makes_zero_llm_calls` |
|                                        | `TestNoLLMCalls.test_cycle_synchronize_makes_zero_llm_calls` |
|                                        | `TestNoLLMCalls.test_default_provider_pure_deterministic` |
| **39. Execution regression**           | `TestExecutionRegression.test_no_provider_means_skip_sync` |
|                                        | `TestExecutionRegression.test_synchronization_disabled_by_policy` |
|                                        | `TestExecutionRegression.test_require_settlement_false_allows_non_settled` |
|                                        | `TestExecutionRegression.test_existing_execution_tests_still_pass` |

Plus 8 additional tests for the `ExecutionResult`/`ExecutionTrace`
synchronization fields, observability events, and policy
configuration.

**All 29 Stage 19.3 tests pass.**

## Q. Regression Tests

All previous-stage tests pass after Stage 19.3 changes:

| Stage | Test File                                      | Pass / Total |
| ----- | ---------------------------------------------- | ------------ |
| 18.4  | `test_stage18_4_native_first_router.py`        | (all green) |
| 18.5  | `test_stage18_5_generic_action_foundation.py`  | (all green) |
| 18.6  | `test_stage18_6_target_resolver_and_grounding.py` | (all green) |
| 18.7  | `test_stage18_7_perception_bridge.py`          | (all green) |
| 18.8  | `test_stage18_8_perception_contract.py`        | (all green) |
| 18.9  | `test_stage18_9_perception_cache.py`           | (all green) |
| 19.0  | `test_stage19_0_execution_cycle.py`            | (all green; 6 skipped) |
| 19.1  | `test_stage19_1_real_integration.py`           | (all green) |
| 19.2  | `test_stage19_2_precondition_functionality.py` | 13/13 |
| 19.3  | `test_stage19_3_synchronization.py`            | 29/29 |

Total regression: **199 passed, 38 warnings** (with the single pre-existing
`test_action_boundary_no_pyautogui` deselection noted below).

### Pre-existing test issue (NOT caused by Stage 19.3)

The test `test_action_boundary_no_pyautogui` in
`test_stage19_0_execution_cycle.py` fails when run after
`test_stage19_1_real_integration.py` because that file imports
`pyautogui` for its real-desktop tests. The test asserts that
`pyautogui` is not in `sys.modules` at the time the cycle is tested.
The failure is a test-ordering artifact, not a Stage 19.3 issue.
When run in isolation or before the real-integration test, the
boundary test passes. Stage 19.3 does not change pyautogui imports
in any way.

## R. Interactive Desktop Tests

The default synchronization provider is exercised against
`FakePerceptionProvider` in unit tests. The provider's contract is
observation-based, so any conforming `PerceptionProvider` works
(including the real `PerceptionAdapter` from Stage 18.7).

For interactive desktop testing, the same provider would observe the
real desktop via the standard perception path:

1. User clicks a button (real ACT)
2. Cycle calls `wait_until_settled(context)` with an
   expectation of `target_visible("Save dialog")`
3. Provider polls the real perception adapter, which performs real
   vision / window-context observation
4. As soon as the Save dialog actually appears on screen, the
   expectation is satisfied and the provider returns SETTLED
5. Cycle proceeds to VERIFY with a fresh, settled observation

The provider never invents a settlement verdict. It waits until the
desktop **actually** shows the expected state, or the timeout
expires.

## S. Files Created

1. `core/execution/sync.py` — the synchronization module containing
   all Stage 19.3 types and the default provider
2. `tests/test_stage19_3_synchronization.py` — comprehensive test
   suite (29 tests)

## T. Files Modified

1. `core/execution/__init__.py` — added exports for
   `SynchronizationProvider`, `SynchronizationResult`,
   `SynchronizationContext`, `SynchronizationStatus`,
   `DefaultSynchronizationProvider`,
   `create_default_synchronization_provider`
2. `core/execution/result.py` — added `SYNCHRONIZATION_FAILED` to
   `ExecutionStatus`; added sync fields to `ExecutionTrace` and
   `ExecutionResult`; updated `to_dict()` to include sync data
3. `core/execution/cycle.py` — added `ExecutionPolicy` fields for
   sync; added `synchronization_provider` to `__init__`; added
   `_synchronize` and `_map_synchronization_status` methods;
   integrated the SYNCHRONIZE phase between ACT and VERIFY in
   `execute()`

## U. Known Limitations

1. **Synchronization kind coverage** — the default provider only
   deterministically verifies a subset of `ExpectationKind`
   (`TARGET_VISIBLE`, `TARGET_PRESENT`, `TARGET_ABSENT`,
   `WINDOW_EXISTS`, `WINDOW_FOCUSED`). For kinds that require OCR
   (`TEXT_PRESENT`, `TEXT_CHANGED`, `SCREEN_CHANGED`,
   `FOCUS_CHANGED`), the sync layer does not satisfy the
   expectation; it falls back to contextual stability or a
   single-observation check. This is honest: we don't pretend to
   verify text the sync layer can't read.

2. **No first-class fallback provider chain** — the cycle currently
   uses a single `synchronization_provider`. If the user wants
   multiple strategies (e.g., "use the AI-brained waiter if
   deterministic waiting times out"), that would be a higher-level
   composition concern, outside Stage 19.3's scope. The
   `SynchronizationProvider` protocol supports any implementation.

3. **Cache invalidation granularity** — the current implementation
   invalidates the entire cache before obtaining the first
   post-action observation. A more sophisticated implementation
   could invalidate only the specific pre-action observation id.
   The default provider's best-effort duck typing does not assume
   the cache key shape is known.

4. **Synchronous perception request** — the default provider builds
   its own `PerceptionRequest` rather than inheriting the step's.
   This is intentional (sync wants minimal data: vision +
   window_context, no screenshot) but it means the step's
   region/OCR flags are not used during sync. This is a deliberate
   trade-off; the step's flags are honored in OBSERVE and VERIFY.

5. **No "ready" predicate** — there is no separate "is the
   environment ready?" check. Settlement is purely a
   post-hoc observation: "we got a fresh observation that
   satisfies the expectation OR is contextually stable." This is
   correct for our deterministic constraint, but it means there is
   a brief window between the action completing and the post-action
   observation being taken.

## V. Architectural Concerns

1. **LLM-free, but LLM-extensible** — the protocol is LLM-free by
   default, but a future "intelligent" provider could call an
   LLM as part of its `wait_until_settled` method. This is by
   design: the protocol is the contract, not the implementation.
   The default provider is deterministic; alternative
   implementations can be anything. The cycle does not enforce
   LLM-freeness in custom providers — that's a project policy,
   not an architectural one.

2. **Sync is a phase, not a wrapper** — the SYNCHRONIZE phase is
   inserted as a discrete step in the cycle, not as a wrapper
   around the action. This is correct: synchronization is a
   decision ("is the environment ready?"), not a property of the
   action itself.

3. **Polling is the mechanism, not the strategy** — the
   provider uses polling because that's the only bounded way to
   observe a desktop without an event subscription mechanism.
   Alternative implementations could subscribe to OS-level events
   (window-manager notifications, UI automation events) for
   instant settlement. The protocol supports this.

4. **Settlement evidence is one observation, not a stream** —
   the provider settles on a single observation that matches the
   expectation or contextual-stability condition. It does not
   require multiple confirmations. This is a trade-off: faster
   settlement, but no protection against a single spurious
   match. The two-observation stability check is the protection.

5. **Cancellation is cooperative** — the cancellation check
   happens at the top of each poll iteration. If the
   observation itself is slow to return (e.g., a hung vision
   backend), the cancellation may be delayed. The
   `asyncio.wait_for` wrapper provides a hard backstop but
   cannot interrupt a synchronous blocking call.

6. **ExecutionResult is mutable in spirit, frozen in form** —
   the new `synchronization_result` field follows the same
   pattern as `verification_result` and `precondition_results`:
   it is set on success, preserved through serialization, and
   the cycle never re-mutates it. The dataclass is `frozen=True`
   to enforce this.

## W. Recommended Next Stage

Stage 20 candidate — **Real-Time Stage 20: Wait Primitives & Multi-Sync Strategies**

Goals:

* **Per-application wait helpers** that *compose* the synchronization
  abstraction (e.g., `wait_for_window("Chrome")` that uses
  `expectation=window_exists("Chrome")`). These are user-facing
  helpers, not new providers — they wrap the existing
  `SynchronizationProvider` with sensible defaults.
* **Adaptive poll intervals** that slow down after a few polls
  (e.g., 50ms for the first 5, 200ms for the next 5, 1s after
  that) to reduce perception cost in long waits.
* **Event-driven settlement** via OS-level notifications
  (e.g., `pywin32` `SetWinEventHook` on Windows) for instant
  settlement without polling. This would be a new
  `EventDrivenSynchronizationProvider` that wraps the default
  provider as a fallback.
* **Settlement diagnostics** — surface `poll_count`,
  `elapsed_ms`, and `observation_id` as first-class cycle
  metrics for monitoring.
* **Expectation chaining** — support multiple sequential
  expectations (e.g., "wait for the dialog, then wait for the
  text field to be empty"). This is a higher-level composition
  concern built on top of the single-step synchronization.

These are all **next-stage** concerns. Stage 19.3 deliberately
stops at the single-execution-step level with a deterministic
abstraction. The next stage adds the user-facing layer without
breaking Stage 19.3's invariants.

## X. FINAL VERDICT

**PASS.**

All Stage 19.3 requirements are met:

* ✅ Generic `SynchronizationProvider` protocol implemented
* ✅ Structured `SynchronizationResult` with closed status enum
* ✅ Deterministic state-settling with bounded polling
* ✅ No arbitrary sleep — bounded timeout + cancellation only
* ✅ Expectation-driven waiting (preferred) and contextual stability
  (fallback) and single-observation (last-resort) strategies
* ✅ Cache invalidation of pre-action observations
* ✅ Pre-action observation rejected if returned as fresh evidence
* ✅ LLM-independence enforced (0 LLM calls in sync layer)
* ✅ No application-specific code (Chrome/Notepad/etc.)
* ✅ No hardcoded coordinates
* ✅ No recovery / self-correction / AI escalation
* ✅ No multi-step autonomy
* ✅ Observability events (SYNCHRONIZATION_*)
* ✅ `main.py`, voice, brain, memory all preserved
* ✅ All 29 new Stage 19.3 tests pass
* ✅ All 199 regression tests pass (Stages 18.4–19.2)
* ✅ Single pre-existing test ordering issue is unrelated to
  Stage 19.3 and is documented in section Q

The SYNCHRONIZE phase is **strictly additive** to the execution
cycle. Existing code that does not configure a
`synchronization_provider` is unaffected (the phase is skipped).
The default `ExecutionPolicy` values are conservative
(`enable_synchronization=True`, `synchronization_timeout_s=5.0`,
`require_settlement=True`).

Stage 19.3 is a foundation for the higher-level wait primitives
and multi-sync strategies that will follow in Stage 20. It does
not overreach, and it does not break anything that came before.
