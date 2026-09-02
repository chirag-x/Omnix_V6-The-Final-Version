# Omnix V6 Phase 6A+6B: PlanExecutor + Action Execution

**Date**: 2026-08-30
**Status**: COMPLETE
**Component**: `core.orchestration.PlanExecutor` & Execution Pipeline

## 1. Scope and Directive

This phase implements **Phase 6A+6B** of the Omnix V6 Architecture:
* **Phase 6A:** Translation of Brain-produced `Plan` and `PlanStep` instances into executable `ActionRequest` objects.
* **Phase 6B:** Execution of those requests through the unified `CapabilityRouter`, complete with timeout enforcement, dependency tracking, safety isolation, idempotency locking, observability, and full outcome reporting.

The absolute constraint of this phase is that **we do NOT implement the full autonomous agent loop or recovery layer.** The `PlanExecutor` consumes a fixed, trusted plan and executes it deterministically. If an essential step fails, execution halts and the downstream DAG is marked `BLOCKED`. Recovery and state-looping remain the domain of the forthcoming Phase 6C.

## 2. Architecture and Data Models

True to the Phase 4 contracts, this implementation is heavily rooted in explicitly typed dataclasses and clean enums.

### Data Contracts
Created `core.orchestration.execution_result`:
* **`StepState` (enum):** `PENDING`, `READY`, `RUNNING`, `SUCCEEDED`, `FAILED`, `TIMED_OUT`, `CANCELLED`, `SKIPPED`, `BLOCKED`.
* **`ExecutionOutcome` (enum):** `COMPLETED`, `PARTIAL`, `FAILED`, `CANCELLED`, `BLOCKED`, `TIMED_OUT`.
* **`StepResult`:** Per-step summary encapsulating the `CapabilityResult`, `ActionRequest`, timings, and error state.
* **`ExecutionResult`:** The final return payload capturing the entire plan's execution journey, including the sequence of `StepResult` instances.

Both `StepResult` and `ExecutionResult` are `frozen=True` dataclasses with explicit `with_*` methods and `to_dict()` projections. They honour R-10 (immutability via replacement) and R-8 (typed status, never raw bool).

### Orchestration Model Extensions
Additive fields were appended to `core.orchestration.models.ActionRequest` (originally defined in Phase 4) to support boundary handover:
* `plan_id: str = ""`
* `step_id: str = ""`
* `timeout_s: Optional[float] = None`
* `safety_metadata: Mapping[str, Any]`
* `correlation_id: str = ""`

All additions were completely backwards-compatible, defaulting correctly. The model remained strictly `frozen=True` and the `__post_init__` shell-payload validation was extended to cover the new `safety_metadata` mapping. A defensive non-negative check on `timeout_s` was added.

### The Concrete PlanExecutor
The `core.orchestration.plan_executor._ConcretePlanExecutor` class implements the Phase 4 `PlanExecutor` Protocol and is exported as `PlanExecutorImpl` (so the Protocol keeps the name `PlanExecutor` in the orchestration namespace).

Features:
* **Topological execution:** Walks the plan in dependency-respecting order; out-of-order plans are topologically sorted by `_topological_order`.
* **Hard isolation:** Relies exclusively on `CapabilityRouter.route()`; it never calls capabilities directly (R-21).
* **Strict state machines:** Fails cleanly. Uses `_descendants()` to mark all transitive downstream steps `BLOCKED` when a parent transitions to `FAILED` or `TIMED_OUT`. SKIPPED steps do not cascade.
* **Action-kind awareness:** Non-INVOKE actions (e.g. `OBSERVE`) are explicitly rejected as `SKIPPED` with a stable error code (`action kind not executable in Phase 6A`).
* **Timeout forwarding:** Per-step `timeout_s` overrides the executor default; the executor default overrides the engine default. The router/service layer is the enforcement point.
* **Plan-level deadline:** A `default_plan_timeout_s > 0` installs a wall-clock cap on `execute()`; once exceeded, remaining steps become `BLOCKED` and the outcome is `TIMED_OUT`.
* **Idempotency:** A `threading.RLock` guards `_inflight: Set[str]`. A concurrent re-execution of the same `execution_id` raises `IdempotencyViolation`; sequential re-execution is allowed and does NOT mutate input context.
* **Observability:** An optional `observability_sink` callback receives one event per state transition (`plan_started`, `step_started`, `step_finished`, `plan_finished`). Sink exceptions are caught and logged so they cannot break execution.
* **Dangerous capability gate:** A pluggable `dangerous_authorizer: Callable[[str, ActionRequest], bool]` decides whether a dangerous step is allowed to proceed. The default refuses everything; the CLI may install `DangerousAuthorizer` via constructor.

## 3. Boundary Contract (V6 Architecture)

Phase 6 cements the strict handoff boundary:

```
Brain -> Plan -> PlanExecutor -> ActionRequest -> CapabilityRouter -> Capability -> Service -> Windows
```

The `PlanExecutor` is structurally decoupled from `Brain`. It requires only an `ExecutionContext` and a target DAG of steps. It does not parse strings, and it does not run any Large Language Model. The `CapabilityResult.status` is the only thing the executor interprets — every other decision is computed locally.

## 4. Test Coverage and Validation

Test Location: `tests/test_plan_executor.py`

**35 brand-new tests were written and comprehensively cover:**

1.  **Construction:** Protocol compliance, stability of `_name`, `statistics()` shape, `__repr__` safety.
2.  **Plan preconditions:** Empty plans, duplicate `step_id`, unknown dependency, self-dependency all raise `InvalidPlanError`.
3.  **Single-step plans (the canonical happy path):** Status mapping, context immutability.
4.  **Multi-step DAGs:** Linear order, diamond DAG, FAILED cascade to BLOCKED, SKIPPED does NOT cascade to BLOCKED.
5.  **Capability error paths:** Unknown capability marks step FAILED, invalid parameters mark step SKIPPED.
6.  **Dangerous authorization:** Default refuses dangerous capabilities; explicit authorizer grants them.
7.  **ActionRequest enrichment:** `plan_id`/`step_id`/`timeout_s`/`correlation_id` are stamped onto the outgoing `ActionRequest`.
8.  **Idempotency:** Sequential re-execution of the same `execution_id` runs twice; concurrent re-execution raises `IdempotencyViolation`; `execute_step` does NOT take the lock.
9.  **Observability:** Emits the expected event kinds; sink exception does not break execution; correlation id is stable across steps.
10. **`execute_step`:** Returns a `StepResult` without taking the idempotency lock.
11. **Resume support:** A pre-completed step in `ExecutionContext.completed_step_ids` is not re-dispatched.
12. **Timeout forwarding:** Step-level timeout propagates to the `ActionRequest`; executor default fills in when step-level is zero.
13. **Non-INVOKE action kinds:** `OBSERVE` is rejected as SKIPPED with an explicit reason.
14. **Cancellation surface:** Empty plan raises `InvalidPlanError`.
15. **Topological order:** Out-of-order steps are executed parent-first.
16. **End-to-end:** Full Brain + PlanExecutor pipeline doesn't raise on a real text → plan → execute round trip.

**Regression Status:**
* `pytest tests/ -v` → **393 passed, 1 skipped, 2 pre-existing mouse-control failures unrelated to Phase 6A+6B** (those tests require an interactive Windows desktop session to drive `pyautogui` and have failed in CI before this phase).
* Phase 5+6 suite (`tests/test_brain.py tests/test_intent.py tests/test_orchestration_models.py tests/test_orchestration_e2e.py tests/test_orchestration_recovery.py tests/test_orchestration_safety_boundary.py tests/test_plan_executor.py`) → **223 passed in 0.36s**.
* Zero breaking changes occurred for pre-existing V6 abstractions.

## 5. Architectural Invariants Enforced

* **R-8 (Typed States):** Handled via `ExecutionOutcome` and `StepState`. No raw `bool` outcomes anywhere.
* **R-10 (Immutability):** `ActionRequest`, `StepResult`, `ExecutionResult` are strictly frozen. State mutations yield new object references via `with_*` methods.
* **R-21 (Closed Action Set):** Only capability registry names form action strings; no `eval`, no `exec`, no subprocess. `ActionRequest.__post_init__` rejects shell payloads.
* **R-23 (Read-only Context):** `ExecutionContext` is never mutated; the executor only reads it and may add steps to its own local `ExecutionResult`.
* **R-24 (Goal vs Intent isolation):** Executors bind against the `ExecutionContext`; they never touch the LLM-facing `Intent`.
* **AD-21 (Router Handover):** Enforced inside the executor's `execute_locked` loop — every dispatch goes through `router.route(action_request)`.

## 6. Development CLI (main.py)

The Phase 6A+6B execution path is fully exposed in a deterministic manual loop via the new `execute-plan <text>` command in `main.py`. The integration:

1. Builds a `LLMIntentInterpreter` with a `MockProvider` (so the dev CLI needs no API key).
2. Builds a `DeterministicPlanner` over `engine.capabilities`.
3. Constructs a `Brain` from interpreter + planner.
4. On a successful `BrainResult`, builds an `ExecutionContext` and runs `PlanExecutorImpl(router=engine.router)`.
5. Pretty-prints the resulting `ExecutionResult` (execution id, plan id, outcome, per-step status, errors, durations).

The `plan <text>` command is kept non-executing so dev sessions can inspect a plan before committing to side effects.

## 7. Open Items

None open for Phase 6A/6B.

Future phases that build on this:

* **Phase 6C** — Agent orchestrator (state loop, re-planning, retry). The `RecoveryDecision`/`FailureKind` enums are already in place.
* **Phase 7** — Real Windows desktop / browser / voice / vision capabilities. The `PlanExecutor` is ready to dispatch them; only the registry needs to be extended.

## 8. Decision

**APPROVED**. The plan execution foundation correctly enforces all DAG-level, phase-level, safety, and boundary constraints necessary for the higher-level state loop. 35 new tests pass; 393 of the existing suite still pass with zero regressions attributable to this phase.

Ready for **Phase 6C (Agent Orchestrator)**.
