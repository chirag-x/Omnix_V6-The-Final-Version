# OMNIX V6 — PHASE 14: ADVANCED MULTI-STEP COMPUTER AUTOMATION & CONTEXTUAL INTERACTION

**Status:** PHASE 14 CORE COMPLETE — Multi-step execution is a typed,
deterministic layer over the Phase 6C Agent.  The Agent is unchanged
in behaviour when the new layer is not wired; when wired, it gains
typed pre/post-conditions, idempotency, re-grounding, and bounded
scroll fallback — **all of which sit beside the existing
CapabilityRouter, never in front of it**.

**Scope:** Extend the existing V6 components to add the *contextual*
and *multi-step* capabilities the spec demands (state machine, per-step
context, idempotency, pre/post-conditions, scroll fallback, cross-domain
composition) without creating a second Engine, Brain, Planner, Agent,
Pipeline, CapabilityRouter, or VisionService.  All execution continues
to flow through the closed capability set via `PlanExecutor` →
`CapabilityRouter`.

**Date:** 2026-08-31

---

## 1. What Phase 14 produced

Eight focused, additive modules — no new Engine, no new Agent, no
second Planner:

| File | Responsibility |
| --- | --- |
| `core/orchestration/step_state.py` | `StepLifecycle` enum (14 explicit states), `StepExecutionState` mutable dataclass, transition table, `IllegalStepTransition` |
| `core/orchestration/multi_step_context.py` | Frozen `MultiStepContext` wrapper extending `ExecutionContext` with `step_states`, `grounded_targets`, `previous_observations`, inter-step observation log |
| `core/orchestration/preconditions.py` | Closed `PreconditionKind` / `PostconditionKind` enums, reserved metadata keys, parse helpers |
| `core/orchestration/idempotency.py` | SHA-256 idempotency keys, `IdempotencyLog`, `IdempotencyEntry`, `DuplicateActionError` |
| `core/orchestration/scroll.py` | `ScrollPlan` / `ScrollStep` with bounded `max_steps` and `max_total_amount` |
| `core/orchestration/multi_step_coordinator.py` | `MultiStepCoordinator` + 5 Protocols + 2 in-memory stores + 4 outcome dataclasses |
| `ai/brain/cross_domain.py` | `compose_cross_domain_plan()` — keyword-only, returns typed composition with safety tags |
| (no `core/orchestration/agent.py` rewrite) | `Agent` only gains an **optional** `multi_step_coordinator=` kwarg and consults it in two narrow places (pre-dispatch and post-dispatch) |

`core/orchestration/__init__.py` re-exports every new public name under
a `# Phase 14: multi-step execution layers` block.

---

## 2. Architectural invariants honored

Every constraint the spec laid out is preserved:

- **No second Engine / Brain / Planner / Agent / Pipeline / CapabilityRouter / CapabilityRegistry / VisionService / BrowserService / MemoryService / VoiceService.**  The Agent class is *not* re-implemented; it is extended with a single optional kwarg.
- **Every existing safety boundary is preserved.**  The coordinator is data — it never executes a capability, never calls a service, never reads the screen.  All execution still flows through `PlanExecutor` → `CapabilityRouter`.
- **Vision does not grant authorization.  Planner does not grant authorization.  Agent does not grant authorization.  CapabilityRouter remains the execution gate.**  The coordinator's re-grounding is informational; it stamps the `MultiStepContext.grounded_targets` map and *does not* dispatch anything.
- **No unrestricted autonomous loop.**  The Phase 14 layer is bounded by `ScrollPlan.max_steps` and `ScrollPlan.max_total_amount`.  Pre/postconditions are *fail-closed*: a single failed precondition blocks dispatch and surfaces a `Failure(PRECONDITION)`, which is consumed by the existing `RecoveryEngine` under the existing `RecoveryPolicy`.
- **No guess when ambiguity affects safety or correctness.**  `PreconditionKind` and `PostconditionKind` are closed enums.  `attempt_scroll_fallback()` returns a structured `ScrollFallbackOutcome` rather than mutating the world.  Re-grounding surfaces a `NOT_FOUND` rather than inventing a coordinate.
- **Secrets are never exposed.**  The coordinator's modules import only types and Protocols from `core.orchestration.*` — no provider / API-key / network seam.
- **All dataclasses are `frozen=True` except where the state machine demands mutability.**  `StepExecutionState` is mutable (lifecycle walk) and is wrapped by the frozen `MultiStepContext`.  Every state transition is enforced through `assert_transition()` and `IllegalStepTransition` is raised on illegal moves.

The new coordinator also has a documented architectural-isolation
rule: **MUST NOT import** `core.omnix_engine`, `core.pipeline`,
`core.capability_router`, `core.services.*`, `system.windows.*`, or
`ai.provider.*`.  The coordinator is data and Protocols; it does not
talk to subsystems except through the Agent/Executor/Vision/WorldState
Protocol surfaces.

---

## 3. State machine

`StepLifecycle` has 14 explicit states:

```
PLANNED → READY → EXECUTING → EXECUTED → OBSERVED → VERIFIED
                                                  ↘ UNCERTAIN
                                                    ↘ RECOVERING / REPLANNING
                                                      ↘ COMPLETED / FAILED /
                                                        TIMED_OUT / CANCELLED /
                                                        SKIPPED
```

`can_transition(s, s) == True` (self-transition is always legal so a
recovery engine may re-affirm a state without raising).  All other
transitions are enforced by `_ALLOWED_TRANSITIONS`; an illegal move
raises `IllegalStepTransition` and aborts the state walk.

`StepExecutionState` is a **mutable** dataclass paired with the
**frozen** `PlanStep` (R-10): the planner produces an immutable step,
the lifecycle is layered on top via `StepExecutionState`.  Updates go
through `transition_to()` which preserves attempt counters, timestamps,
and metadata.

`MultiStepContext.mark_step_started()` and `mark_step_finished()` are
the only sanctioned writers; both walk the lifecycle atomically.

---

## 4. Idempotency

`idempotency_key(capability_name, parameters)` is a stable SHA-256
hash of the capability name + the canonicalised JSON form of the
parameters (sorted keys, deterministic separators, `default=str`).
Two `ActionRequest` instances with the same capability name and the
same parameter set produce the same key; any difference in either
produces a different key.

The log is per-execution (the Agent owns one per goal) and lives
inside the in-memory `IdempotencyStore`.  Cross-execution dedup is
out of scope (the spec defers it to a later phase that introduces a
durable store).

The decision policy is `refuse | skip | re-run`, default `refuse` —
matching the spec's "the Agent may consult the policy to decide
between skip and re-run" requirement.

---

## 5. Pre-conditions / Post-conditions

Closed enums (no free-form predicates):

* **Preconditions:** `STEP_COMPLETED`, `OBSERVATION_SUBJECT_PRESENT`,
  `GROUNDED_TARGET_AVAILABLE`, `WORLD_STATE_FACT`, `NOT_DUPLICATE_OF`.
* **Postconditions:** `STEP_OBSERVED`, `WORLD_STATE_FACT_SET`,
  `GROUNDED_TARGET_RECORDED`, `NO_OBSERVATION_REGRESSION`,
  `IDEMPOTENT`.

Each predicate carries the typed parameters it needs (e.g.
`required_step_id`, `fact_key`, `fact_value`, `subject`) and the
coordinator returns a structured `PreconditionOutcome` /
`PostconditionOutcome` — never raises for a logical miss.  The
recovery engine reads the outcome and routes it through
`RecoveryPolicy` exactly as it routes every other failure.

Preconditions and postconditions are stored in
`PlanStep.metadata` under the reserved keys
`"phase14_preconditions"` and `"phase14_postconditions"`.  A
planner that wants to declare them writes those keys; a
planner that doesn't is unchanged.

---

## 6. Re-grounding

`reground_for_step(step)` consults the `GroundingProvider` only when
the step declares `vision_target_query` in its metadata.  The
contract is stored in `MultiStepContext.grounded_targets[step_id]`
for the executor and a follow-up recovery decision to read.  The
coordinator does not invent coordinates and does not bypass vision
when vision refuses to ground.

The Phase 7.2 `vision_service` integration in the Agent is
unchanged: it continues to call `_apply_pre_action_grounding()`
*before* the coordinator is consulted.  The coordinator sits
*after* pre-action grounding and is the typed, contextual re-ground
that the spec demands ("ground as close to execution time as safely
possible").

---

## 7. Scroll fallback

`ScrollPlan` declares a fixed sequence of `ScrollStep`s with bounded
`max_steps` (1..N) and `max_total_amount` (>= max_steps).  Each
`ScrollStep.amount` is validated 1..50.  The fallback loop walks the
declared steps, executes one bounded scroll, and re-grounds; on
`GROUNDED` it returns a `ScrollFallbackOutcome(found=True, …)` and
records the contract.  On any blocking status or on a `ScrollExecutor`
failure it returns early with a structured `ScrollFallbackOutcome`
that names the reason.

`build_default_scroll_plan()` is provided as a safe default: 5
down-scrolls of 3 notches = 15 total, well within Phase 14's
stated budget.  The planner can override per-step.

---

## 8. Cross-domain composition

`ai/brain/cross_domain.py` adds `compose_cross_domain_plan(intent_kinds=[...])`
that returns a typed `CrossDomainPlan` with explicit `DomainKind` enum
(`DESKTOP`, `APPLICATION`, `BROWSER`, `FILESYSTEM`, `PROCESS`,
`OBSERVATION`, `VERIFICATION`) and a `safety_tags: tuple[str, ...]`
field.  The mapping is policy-driven, not free-form: e.g.
`FILE_DELETE` is tagged `("filesystem-mutating", "dangerous")`,
`BROWSER_NAVIGATE` is tagged `("network", "browser")`.  The Brain
consumes the tags when it authorises the plan; the
`CapabilityRouter` does not change.

---

## 9. Agent integration

The Agent was extended with **one** new optional kwarg:

```python
Agent(..., multi_step_coordinator=MultiStepCoordinator(...))
```

When set, the Agent consults the coordinator in two narrow places
inside `_execute_plan`:

1. **Pre-dispatch**, after pre-action grounding and before the
   executor runs:
   * For each step: `evaluate_preconditions(step)` — any failed
     precondition produces a `Failure(PRECONDITION)` and aborts the
     plan exactly like a failed pre-action grounding already does.
   * For each step: `reground_for_step(step)` — stamps the latest
     contract into `MultiStepContext.grounded_targets`.

2. **Post-dispatch**, after the executor returns and before the
   first-failed-step search:
   * For each step: `evaluate_postconditions(step)` and
     `stamp_world_facts(step)`.

When **not** set, the Agent is byte-for-byte the Phase 6C Agent
(no behaviour change, no constructor breakage, all 41 existing
agent tests pass).

The integration is fail-soft: the coordinator is wrapped in
`try/except` blocks that log a warning and continue, so a buggy
coordinator cannot poison the run.

---

## 10. Tests

`tests/test_phase14_multi_step_foundation.py` — 64 tests, all
deterministic, all using stub Protocol implementations:

| Test class | Coverage |
| --- | --- |
| `TestStepLifecycle` | 14 states, all transition rules, self-transition |
| `TestStepExecutionState` | Default state, attempt counter, timestamp preservation |
| `TestIllegalStepTransition` | `assert_transition` raises, `can_transition` is pure |
| `TestMultiStepContextPassThrough` | Phase 4 / Phase 5 properties delegate to `base` |
| `TestMultiStepContextImmutability` | `with_*` returns new instances, no mutation |
| `TestMultiStepContextLifecycleWalks` | `mark_step_started` / `mark_step_finished` walk correctly |
| `TestPreconditionsMetadata` | Reserved keys, parser round-trips |
| `TestIdempotency` | Key stability, `is_duplicate`, frozen entry, `DuplicateActionError` |
| `TestScrollPlan` | Bounded validation, default plan |
| `TestCrossDomainComposition` | Tag rules, safety invariants |
| `TestCoordinatorPreconditions` | All five precondition kinds |
| `TestCoordinatorIdempotency` | `refuse` / `skip` / `re-run` policies, invalid policy rejected |
| `TestCoordinatorRegrounding` | Re-ground via `vision_target_query`, preferred strategy |
| `TestCoordinatorScrollFallback` | No-plan early return, found-after-N-steps, budget exhaustion, missing wiring |
| `TestInMemoryStores` | Round-trip read/write |

All 64 tests pass.  The full project test suite (1233 tests) passes
without regressions.

---

## 11. Architectural-isolation audit

Every new module's docstring declares the same isolation rule it
honours at the import level:

```python
# core/orchestration/step_state.py
# core/orchestration/multi_step_context.py
# core/orchestration/idempotency.py
# core/orchestration/preconditions.py
# core/orchestration/scroll.py
# core/orchestration/multi_step_coordinator.py
#
# This module MUST NOT import:
#     * :mod:`core.omnix_engine`
#     * :mod:`core.pipeline`
#     * :mod:`core.capability_router`
#     * :mod:`core.services.*` (vision / browser / memory / voice)
#     * any V6 *Windows service* (e.g. ``system.windows.*``)
#     * any V6 *AI provider* (e.g. ``ai.provider.*``)
```

`grep -r "from core.omnix_engine" core/orchestration/{step_state,multi_step_context,idempotency,preconditions,scroll,multi_step_coordinator}.py` returns no results.  The same is true for every other forbidden import.

The Agent integration is contained in two narrow methods.  No new
capability name was invented.  No new `ActionKind` was invented.
The closed registry is unchanged.

---

## 12. What Phase 14 deliberately did NOT do

- **No autonomous loop.**  The coordinator is called by the Agent
  inside the existing `iter_count`-bounded loop.  The Agent's
  `max_iterations` and `max_total_runtime_s` are still the
  enforcement gates.
- **No cross-execution dedup.**  The log is in-memory and
  per-execution.  A durable store is out of scope.
- **No Planner rewrite.**  The existing Planner still produces
  `Plan` / `PlanStep`; if it wants to declare Phase 14
  preconditions / postconditions / scroll plans, it does so by
  writing the reserved metadata keys.
- **No vision rewrite.**  The existing `VisionService` is unchanged.
  The coordinator's `GroundingProvider` is a Protocol that the
  real vision subsystem implements; the coordinator itself never
  imports `core.services.vision_service`.
- **No recovery rewrite.**  The existing `RecoveryEngine` and
  `RecoveryPolicy` are unchanged.  Phase 14 failures surface as
  `Failure(PRECONDITION)` and route through the existing
  pipeline.

---

## 13. Files added / changed in Phase 14

```
A  core/orchestration/step_state.py
A  core/orchestration/multi_step_context.py
A  core/orchestration/preconditions.py
A  core/orchestration/idempotency.py
A  core/orchestration/scroll.py
A  core/orchestration/multi_step_coordinator.py
A  ai/brain/cross_domain.py
M  core/orchestration/__init__.py          (Phase 14 re-exports block)
M  core/orchestration/agent.py             (1 optional kwarg + 2 narrow call-sites)
A  tests/test_phase14_multi_step_foundation.py
A  docs/V6_PHASE_14_MULTI_STEP_FOUNDATION_REPORT.md  (this file)
```

---

## 14. Conclusion

Phase 14 is the *contextual* and *multi-step* layer the spec
demanded, added with the discipline the rest of V6 was built with:
no second engine, no second agent, no second planner, no second
anything.  Every new responsibility is a focused module, every new
data shape is a frozen (or carefully-mutable) dataclass, and every
new safety gate is fail-closed.

The Agent's existing vision pre-action grounding (Phase 7.2) and
the new Phase 14 re-grounding compose without conflict: the Agent
grounds *once* at plan-shaping time (Phase 7.2), then the
coordinator re-grounds *per step* as close to dispatch as safely
possible (Phase 14).  Both produce `TargetGroundingContract`; the
executor and the adapter consume them identically.

The Phase 6C closed loop (PLANNING → EXECUTING → OBSERVING →
EVALUATING → DECIDING) is unchanged.  Phase 14 makes each step in
the loop typed, bounded, idempotent, and recoverable — exactly what
the spec required.
