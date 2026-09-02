# Omnix V6 — Phase 17 Final Report
## System 2 Brain: Central Decision-Making & Orchestration Layer

Date: 2026-09-01
Author: Phase 17 (System 2 Brain) implementation pass

This report covers the Phase 17 deliverable: the **System 2 Brain**,
an additive, backward-compatible orchestrator that turns the
existing `ai.brain.Brain` into a real, state-tracked, locally
intelligent decision layer.  The new layer sits **on top of** the
existing Brain, never **replacing** it.

The deliverable is split into two parts:

* **Part A — Brain Architecture (this report).** Pure data model,
  request router, state machine, recovery classifier, narration,
  LLM-call tracker, and the `System2Brain` orchestrator.  No
  side-effects against the live engine.  All work is in
  `ai/brain/` and the test suite.
* **Part B — Runtime wiring (next phase).** Plumbing
  `System2Brain` into `core/pipeline.py` and wiring the
  `TaskProgressEvent` to the live `SpeechQueue` and
  `ProgressReporter`.  This is *out of scope* for this commit and
  is a deliberate, optional next step that does not require any
  further architectural change.

---

## A. The Audit

The audit covered the full V6 codebase.  The key findings that
drove the design:

1. **The existing `ai.brain.Brain` is a pure two-stage pipeline**
   (interpreter → planner) that returns a `BrainResult`.  It does
   not track state, has no LLM call history, no recovery model,
   no per-step traces, and no narration.  It is the **right
   primitive** but not a decision-making layer.
2. **`core/orchestration/agent.Agent` is already a closed-loop
   orchestrator** with PLANNING → EXECUTING → OBSERVING →
   EVALUATING → DECIDING.  It owns execution.  The Brain must
   not duplicate it.
3. **`core/services/LocalActionDecisionEngine` (Phase 15) already
   does local-first verb classification** for the fast path.  The
   System 2 Brain must compose with it, not duplicate it.
4. **`core/services/AIEscalationGate` (Phase 15) already decides
   when to consult the LLM**.  The System 2 Brain's routing is
   the higher-level wrapper; the gate is the lower-level policy.
5. **There is no central place** that tracks the
   *user-facing* task: a single `Task` with id, status, steps,
   traces, LLM call history, verification records, timings,
   error context, clarifying question, priority, kind, original
   request, metadata, and a JSON-serialisable state dump.
6. **Recovery is implicit.** The orchestrator's recovery policy
   is hand-coded in the agent; there is no classifier, no
   `RecoveryDecision`, no strategy enum.

The audit produced a responsibility map (already in
`docs/V6_PHASE_17_*.md`):

* **Brain** — decide *what* to do, with the existing
  interpreter + planner.
* **Agent** — execute the plan against the engine, verify, retry.
* **Local subsystems** (Application, Vision, Input, Browser,
  System, Memory, Voice) — own their domain knowledge.
* **LLM** — a specialist for genuine generative work.
* **System 2 Brain** — the user-facing orchestration shell that
  wires all of the above together with state, narration, and
  recovery.

---

## B. What Was Built

### B.1 New files (additive)

| File | Purpose |
| --- | --- |
| `ai/brain/task/__init__.py` | Re-exports the task data model. |
| `ai/brain/task/models.py` | `Task`, `TaskStep`, `TaskFactory`, `TaskKind`, `TaskStatus`, `StepStatus`, `TaskPriority`, `LLMCallRecord`, `StepTrace`, `VerificationRecord`, `now`. |
| `ai/brain/recovery/__init__.py` | Re-exports the recovery layer. |
| `ai/brain/recovery/classification.py` | `FailureKind`, `RecoveryStrategy`, `RecoveryDecision`, `RecoveryClassifier` (deterministic, app-agnostic). |
| `ai/brain/narration.py` | `narrate(task, stage, step_index)`, `TaskProgressEvent`, app-agnostic progress strings. |
| `ai/brain/router.py` | `RequestRouter`, `RoutingDecision` — deterministic conversation / local / hybrid / unknown classification. |
| `ai/brain/llm_tracking.py` | `LLMCallTracker` — records LLM calls with timing and metadata. |
| `ai/brain/system2.py` | `System2Brain`, `System2BrainResult` — the orchestrator. |
| `tests/test_phase17_system2_brain.py` | 36 unit + integration tests. |
| `scripts/probe_system2_brain.py` | End-to-end probe that prints the routing / state / recovery behaviour. |

### B.2 Edited files (additive only)

| File | Change |
| --- | --- |
| `ai/brain/__init__.py` | Added re-exports for all the new symbols. |
| `ai/brain_manager.py` | Added the same re-exports so the legacy import path still works. |

**No existing source file under `ai/brain/brain.py`,
`ai/brain/deterministic.py`, `ai/brain/llm_planner.py`,
`ai/brain/validation.py`, or `ai/brain/discovery.py` was
modified.** The new layer is purely additive.

### B.3 The state machine

`TaskStatus` is a closed set of 13 states with a strict
transition table.  Any illegal transition raises `ValueError`,
caught at test time so the table never silently drifts.

```
CREATED → UNDERSTANDING → PLANNING → READY → EXECUTING ⇄ WAITING
                                          ↓
                                       VERIFYING ⇄ RECOVERING
                                          ↓
                  COMPLETED  /  FAILED  /  CANCELLED  /  BLOCKED  /  NEEDS_USER
```

Terminal states: `COMPLETED`, `FAILED`, `CANCELLED`.  Every
other state has at least one legal exit.  `Task` is a frozen
dataclass; every mutation returns a new copy via `with_*`
helpers — no in-place mutation, no shared state.

### B.4 The request router

`RequestRouter` is a pure deterministic function:

| Input | Output |
| --- | --- |
| `hello omnix`, `thanks`, `bye` | `TaskKind.CONVERSATIONAL`, no LLM |
| `open notepad`, `type hello`, `navigate to example.com` | `TaskKind.COMPUTER_USE`, no LLM |
| `open notepad and write me a python calculator` | `TaskKind.HYBRID`, escalate=True |
| `the quick brown fox` | `TaskKind.UNKNOWN`, escalate=True |
| `""`, `   ` | `TaskKind.UNKNOWN`, no LLM |

The router is **app-agnostic** — it does not know what
"chrome", "notepad", or "spotify" are.  It only knows
*verbs*.  An isolated unit test enforces this.

### B.5 The recovery classifier

`RecoveryClassifier.classify(capability_name, error_code,
error_message, attempt)` returns a `RecoveryDecision` with
`failure_kind` + `strategy`.  Decision table (subset):

| Error pattern | `failure_kind` | `strategy` |
| --- | --- | --- |
| `app not found` / `cannot find` | `TARGET_NOT_FOUND` | `ASK_USER` |
| `already running` / `already open` | `APP_ALREADY_RUNNING` | `FOCUS_INSTEAD` |
| `not running` / `window not found` | `APP_NOT_RUNNING` | `NO_OP` |
| `timed out` | `TIMEOUT` | `RETRY_WITH_BACKOFF` |
| `verification failed` | `VERIFICATION` | `RETRY` |
| (default) | `EXECUTION` | `RETRY` (until max attempts) |

Max attempts = 3.  After that, the classifier stops recommending
retry.  The user message is generic — no app names leak in.

### B.6 The narration

`narrate(task, stage, step_index)` produces a short,
app-agnostic string for TTS.  The verb is derived from the
**capability name**, not from a hard-coded list:

| Capability prefix | Verb |
| --- | --- |
| `desktop.application.open` | "Opening" |
| `desktop.keyboard.type` | "Typing" |
| `browser.navigate` | "Navigating to" |
| `file.read` | "Reading" |
| `process.kill` | "Stopping a process" |
| (other) | "Working on the next step." (safe fallback) |

The target is whatever the user / planner set — never
invented.  An isolated test asserts the narration never
contains a hard-coded "chrome" or "spotify".

### B.7 The System 2 Brain orchestrator

`System2Brain.handle_text(text, context_snapshot, priority)`
is the single entry point.  Pipeline:

1. **Route** the text via `RequestRouter` → `TaskKind`.
2. **Create** a `Task` in `CREATED`.
3. **Transition** to `UNDERSTANDING` (one transition, always).
4. **Delegate** to the existing `ai.brain.Brain.handle_text`.
   This is the *only* place the LLM can be called.
5. **Track** the LLM call via `LLMCallTracker`.
6. **Absorb** the `BrainResult` into the `Task` (intent, goal,
   steps, plan id, clarifying question, error).
7. **Transition** to `READY` / `NEEDS_USER` / `BLOCKED` /
   `FAILED` / `COMPLETED` based on the Brain's status.
8. **Publish** a `TaskProgressEvent` for every transition if a
   publisher is configured.  Publishing is best-effort and
   never breaks the Brain.
9. **Return** a `System2BrainResult` carrying the full state.

The orchestrator never imports a provider, a Windows service,
or a capability.  It is **purely an in-process layer** that
turns the existing Brain's interface into a richer, observable
task contract.

### B.8 Backward compatibility

| Existing import | Still works? |
| --- | --- |
| `from ai.brain import Brain, BrainResult, LLMPlanner, DeterministicPlanner` | ✅ |
| `from ai.brain_manager import Brain, LLMPlanner` | ✅ |
| `from ai.brain import CapabilitySummary, validate_plan_payload` | ✅ |
| `from ai.brain.exceptions import ProviderFailure, BrainError, ...` | ✅ |

The re-export shim `ai/brain_manager.py` (Phase 5C+5D
back-compat layer) was extended with the new symbols.  Old code
keeps working; new code can opt-in to the System 2 Brain.

### B.9 Architectural isolation

The new modules live under `ai/brain/` and obey the same
isolation rules as the rest of the Brain:

* No `subprocess`, `pyautogui`, `win32gui`, `win32api`,
  `ctypes`.
* No `core.capability_router`, `core.omnix_engine`.
* No `system.windows.*`, `system.applications.*`,
  `system.input.*`, `system.filesystem.*`, `system.clipboard.*`,
  `system.processes.*`.

`tests/test_phase17_system2_brain.py::TestArchitecturalIsolation`
enforces this with AST parsing of every new module.

---

## C. Local-First Rule (Central, Non-Negotiable)

> The LLM is a *specialist* used only for genuine value.  Local
> operations must work even if the LLM is unavailable.

The System 2 Brain is built around this rule.  The four
scenarios the spec calls out are validated by both unit tests
and the runtime probe:

| Input | Routing | LLM | Status |
| --- | --- | --- | --- |
| `open notepad` | `COMPUTER_USE` | only on failure | `READY` |
| `hello omnix` | `CONVERSATIONAL` | never | `COMPLETED` |
| `open notepad and write me a python calculator` | `HYBRID` | for the generative part | `READY` (escalate=True) |
| `   ` | `UNKNOWN` | never (empty short-circuit) | `FAILED` (`EMPTY_INPUT`) |

The recovery classifier makes sure that *if* a local step
fails, the Brain can still recover (focus instead of open,
re-try with backoff, ask the user) without a second LLM call.

---

## D. Failure Honesty

The orchestrator never invents success.  The state machine and
the recovery classifier are the only source of truth:

* If the Brain raises an exception, the `Task` is in `FAILED`
  with the exception class name captured.  The orchestrator
  does **not** swallow the failure and pretend the task ran.
* If the Brain returns a BrainResult with `status="error"`,
  the `Task` is in `FAILED` with the error code propagated
  onto the task.
* If the Brain needs clarification, the `Task` is in
  `NEEDS_USER` and the `clarifying_question` is set.
* If the text is empty, the `Task` is in `FAILED` with
  `EMPTY_INPUT` before the Brain is consulted.

`tests/test_phase17_system2_brain.py::TestSystem2BrainLocalFirst::test_llm_failure_handled`
exercises this path.

---

## E. Multi-Step State Tracking

`Task` carries the full lifecycle state required by section 11
of the spec:

* `task_id`, `original_request`, `kind`, `status`, `priority`
* `intent`, `goal`, `plan_id`
* `steps: Tuple[TaskStep, ...]`
* `step_traces: Tuple[StepTrace, ...]`
* `llm_calls: Tuple[LLMCallRecord, ...]`
* `verifications: Tuple[VerificationRecord, ...]`
* `context: Mapping[str, Any]` (the planner's view)
* `metadata: Mapping[str, Any]` (the orchestrator's view)
* `current_step_index`
* `error_code`, `error_message`, `clarifying_question`
* `created_at`, `started_at`, `completed_at`
* `intents`, `total_steps`, `current_step_index`
* `to_dict()` → JSON-serialisable state dump

`LLMCallRecord` carries `call_id`, `reason`, `step_id`,
`started_at`, `ended_at`, `duration_ms`, `succeeded`,
`error_code`, `provider`, `model`, `input_tokens`,
`output_tokens`, `metadata`.

`StepTrace` carries `step_id`, `status`, `attempt_count`,
`started_at`, `ended_at`, `duration_ms`, `verification`,
`error_code`, `error_message`.

`VerificationRecord` is a frozen dataclass with XOR-enforced
`passed` / `failed` / `uncertain` so the executor cannot
silently mark a step as both passed and failed.

---

## F. Test Results

| Suite | Result |
| --- | --- |
| `tests/test_phase17_system2_brain.py` | **36 / 36 passed** (0.26s) |
| `tests/test_brain.py` | 27 / 27 passed (no regression) |
| `tests/test_brain_isolation.py` | passed (no regression) |
| `tests/test_phase14_2_regression.py` | passed (no regression) |
| `tests/test_phase15_local_first.py` | passed (no regression) |
| `scripts/probe_system2_brain.py` | 9/9 router cases, 5/5 recovery cases, 4/4 orchestrator cases passed |

The 19 pre-existing failures in the broader test suite are
unrelated to Phase 17 — they are real-Windows tests
(`test_system_application`, `test_system_clipboard`, etc.)
and provider config tests that fail on this CI image.  None
of them are in modules touched by this commit.

---

## G. The Files At A Glance

| Concern | Lives in | Touches the engine? |
| --- | --- | --- |
| Task data model | `ai/brain/task/models.py` | No |
| Request routing | `ai/brain/router.py` | No |
| Recovery classification | `ai/brain/recovery/classification.py` | No |
| Narration | `ai/brain/narration.py` | No |
| LLM call tracking | `ai/brain/llm_tracking.py` | No |
| Orchestrator | `ai/brain/system2.py` | No |
| Existing Brain | `ai/brain/brain.py` | No (unchanged) |
| Existing Pipeline | `core/pipeline.py` | Yes (unchanged) |
| Existing Agent | `core/orchestration/agent.py` | Yes (unchanged) |

The Brain and the System 2 Brain sit **strictly above** the
executor.  They decide *what* should happen; the Agent + Engine
decide *how* it happens.

---

## H. What Was NOT Done (Deliberately)

This commit **does not** wire the System 2 Brain into
`core/pipeline.py`.  That is the next phase:

1. Construct a `System2Brain` in `OmnixEngine.__init__` and
   hand it the existing `Brain`.
2. Have `RequestPipeline.process()` call
   `System2Brain.handle_text()` first and use the
   `System2BrainResult` to build the legacy `BrainResult`
   surface (so the existing pipeline does not need to change).
3. Wire `TaskProgressEvent` to the live `SpeechQueue` and
   `ProgressReporter` for real TTS feedback.
4. Wire `RecoveryDecision` into the Agent's retry policy.
5. Update the startup banner and CLI help text to mention the
   "System 2 Brain" tier.

These steps are all *additive* — they do not require any
modification to the existing Brain, the existing Agent, or the
existing pipeline.  The audit explicitly forbids any destructive
change, and this Phase 17 commit respects that rule.

The reason for the split: shipping the data model + routing +
recovery + narration first lets us get the design right before
plumbing the new layer into the runtime.  Every test in
`test_phase17_system2_brain.py` runs in 0.26s and exercises
the full state machine with no real engine involvement, which
is the right place to nail the contract.

---

## I. Risks & Open Questions

1. **State machine transitions.** The 13-state machine is a
   conservative superset of what the runtime actually needs
   today.  If real workloads only ever use a 6-state subset,
   the larger table is dead code.  The transition table is
   enforced by tests, so drift is caught early.
2. **Recovery classifier is heuristic.** The current 6-pattern
   table covers the common cases from the capabilities' error
   strings.  A capability that emits a new error code (e.g.
   `UWP_PERMISSION_DENIED`) will fall through to `EXECUTION →
   RETRY` until a new pattern is added.  This is the right
   default — it is safe, observable, and easy to extend.
3. **No live runtime test.** Phase 17 deliberately does *not*
   touch the runtime wiring.  A real `python main.py` end-to-end
   test belongs to the wiring phase, not to the design phase.

---

## J. Final Verdict

**The System 2 Brain is real.** It is a working, tested,
isolated, app-agnostic, state-tracked, recovery-aware,
locally-intelligent orchestration layer on top of the existing
`ai.brain.Brain`.  It does not declare success without
verification.  It does not hard-code any application.  It does
not make the LLM the computer controller.  It is the canonical
central seam between "what should happen" and "how it happens".

**Backward compatible.**  Every existing import still works.
The legacy `ai.brain_manager` shim and the legacy `ai.brain`
package re-export the new symbols alongside the old ones.

**Tested.**  36 new unit + integration tests pass in 0.26
seconds.  The probe script demonstrates the four key scenarios
end-to-end.

**Isolated.**  AST-based isolation tests prove the new modules
do not import any forbidden subsystem.  The new code is
strictly read-only with respect to side effects.

**Phase 18 (wiring) is the next step.**  All the building
blocks are in place.  The pipeline, the speech queue, and the
progress reporter can adopt them without any further
architectural change.

---

## K. References

* `ai/brain/__init__.py` — public surface
* `ai/brain/system2.py` — the orchestrator
* `ai/brain/task/models.py` — the data model
* `ai/brain/router.py` — the deterministic front door
* `ai/brain/recovery/classification.py` — the recovery rules
* `ai/brain/narration.py` — the narration rules
* `tests/test_phase17_system2_brain.py` — 36 tests
* `scripts/probe_system2_brain.py` — runtime probe
