# V6 Phase 5C+5D — Brain & Planner (Implementation Report)

**Phase:** 5C+5D — *Goal → Plan* (Brain orchestration + Planner implementation)
**Status:** COMPLETE
**Date:** 2026-08-30
**Test result:** 69 / 69 Brain-layer tests passing (45 functional + 24 isolation)
**Full regression:** 359 passed, 1 skipped, 2 pre-existing unrelated failures
**Stop condition:** Ready for Phase 6 (Agent / PlanExecutor).

This report covers the Brain layer that turns a validated `Intent`
(Phase 5B) into a validated, structured `Plan` (Phase 4 contract),
what was deliberately *not* built, and the boundaries that the
Phase 6 Agent / PlanExecutor will inherit.

---

## 1. Scope

Phase 5C+5D was authorised to do three things:

1. **Build the Brain.** Take an `Intent`, route it through a
   `Planner`, and return a trusted `Plan` (or a structured
   clarification / error result).
2. **Build two Planner implementations.**
   * `LLMPlanner` — the LLM-backed planner that asks the
     Phase 5A `LLMProvider` for a structured plan.
   * `DeterministicPlanner` — the rule-based planner that maps
     `IntentKind` → capability + parameters without any LLM call.
3. **Build the validation gate.** Define the legal shape of every
   `PlanStep` (capability exists, parameters coerce, types match,
   dependencies form a DAG, timeouts are finite, expected effects
   are well-formed, safety classifications are honest).

The directive was explicit about what is out of scope (see §7).

---

## 2. Architecture

### 2.1 The two-stage AI pipeline

```
User text ──► LLMIntentInterpreter.interpret(text)         [Phase 5B]
              │
              ├──► LLMProvider.generate(LLMRequest)         [Phase 5A]
              │
              ├──► validate_intent_payload(...)
              │
              └──► Intent  ──►  intent.to_goal(goal_id)
                                       │
                                       ▼
                          Brain.handle_text(text)           [Phase 5C+5D]
                          ┌────────────────────────────────────┐
                          │ Stage 1: text → Intent             │
                          │ Stage 2: Intent → Goal → Plan      │
                          │         via Planner.plan(goal,...) │
                          └────────────────────────────────────┘
                                       │
                                       ▼
                              validate_plan_payload(payload)  [§3]
                                       │
                                       ▼
                              Plan  (trusted, ready to execute)
```

The Brain **never** imports or calls:

* `core.capability_router`
* `core.omnix_engine`
* any `system.windows.*` / `system.applications.*` / `system.input.*`
* any `system.filesystem.*` / `system.clipboard.*` / `system.processes.*`
* `subprocess`, `pyautogui`, `win32gui`, `win32api`, `ctypes`
* `Capability.execute(...)` directly

This is enforced by `tests/test_brain_isolation.py` (24 tests).

### 2.2 The package

```
ai/brain/
├── __init__.py        # public exports
├── brain.py           # Brain, BrainResult (orchestration seam)
├── deterministic.py   # DeterministicPlanner (rule-based)
├── discovery.py       # discover_capabilities(), CapabilitySummary,
│                      #   summarize_for_prompt(), find_capability()
├── exceptions.py      # BrainError hierarchy (typed error codes)
├── llm_planner.py     # LLMPlanner (LLM-backed)
└── validation.py      # validate_plan_payload() — the validation gate
```

### 2.3 The contracts

| Type                          | Purpose                                                  |
|-------------------------------|----------------------------------------------------------|
| `Brain`                       | Two-stage pipeline: text → Intent → Goal → Plan          |
| `BrainResult`                 | Structured result (`ok` / `clarification` / `unknown` / `error`) |
| `DeterministicPlanner`        | Rule-based planner, no LLM                               |
| `LLMPlanner`                  | LLM-backed planner (uses Phase 5A `LLMProvider`)         |
| `CapabilitySummary`           | Planner-friendly view of a capability                    |
| `discover_capabilities()`     | Walk the `CapabilityRegistry` and build a summary list   |
| `summarize_for_prompt()`      | Build a bounded-text catalog of capabilities for the LLM |
| `validate_plan_payload()`     | The validation gate: every step is checked               |
| `BrainError` + 14 subclasses   | Typed errors with stable `code` for routing              |

---

## 3. Validation gate (`ai/brain/validation.py`)

`validate_plan_payload(payload, registry) -> Plan` is the *only* path
that can turn a raw plan dict (LLM or deterministic) into a trusted
`Plan` object. It rejects payloads that fail any of:

* **Shape.** Top-level must be a dict with `goal_id`, `steps`,
  optional `version`, optional `metadata`. Each step must have
  `step_id`, `capability_name`, `action`, `description`, `parameters`,
  `expected_effects`, optional `timeout_s`, `retries`, `depends_on`,
  optional `safety_classification`.
* **Capability resolution.** Every `capability_name` must exist in
  the supplied `CapabilityRegistry`. Unknown capabilities raise
  `UnknownCapabilityError` (`BRAIN_UNKNOWN_CAPABILITY`).
* **Action legality.** Each step's `action` must be a legal
  `StepAction` (`INVOKE` is the only one Phase 5C+5D allows).
* **Argument coercion.** Every parameter is run through the
  capability's `CapabilityParameter.coerce()` machinery. Type
  errors (custom `core.errors.ValidationError` *and* plain
  `ValueError`) raise `InvalidArgumentError`
  (`BRAIN_INVALID_ARGUMENT`).
* **Required parameters.** Missing required parameters raise
  `InvalidArgumentError`.
* **Timeout range.** Must be in `[MIN_STEP_TIMEOUT_S=0.0,
  MAX_STEP_TIMEOUT_S=600.0]` seconds.
* **Retries range.** Must be in `[MIN_RETRIES=0, MAX_RETRIES=5]`.
* **Expected effects.** Each effect must be a dict with
  `kind in {STATE, OBSERVABLE, USER_CONFIRMATION}`, a non-empty
  `check` name, and a `description`. Effects with `USER_CONFIRMATION`
  must include a `prompt`.
* **Dependency DAG.** `depends_on` references must:
  * be a list of strings (each a known `step_id`);
  * never include the step's own `step_id`;
  * never create a cycle (topological sort check);
  * never reference a non-existent step.
  Violations raise `InvalidDependencyError`
  (`BRAIN_INVALID_DEPENDENCY`).
* **Safety classification.** A step may not *downgrade* a
  capability's `dangerous` flag. If the capability is `dangerous=True`,
  the step's `safety_classification` must be
  `"requires_confirmation"` (or absent — the executor will apply the
  default). Attempting to classify a dangerous operation as
  `"safe"` raises `SafetyClassificationError`
  (`BRAIN_SAFETY_VIOLATION`).
* **Size bound.** A plan with more than `MAX_PLAN_STEPS=50` steps
  raises `PlanSizeExceeded` (`BRAIN_PLAN_SIZE_EXCEEDED`).

The gate is **exhaustive** — there is no `validate_plan_payload`
that *partially* accepts a payload. Either you get a `Plan` you can
trust, or you get a `BrainError` subclass with a stable `code`.

---

## 4. Discovery (`ai/brain/discovery.py`)

The Brain does not have access to private Phase 3 internals. It sees
the world through the canonical `CapabilityRegistry`, and that view
is mediated by `discover_capabilities()`.

* `CapabilitySummary` is a frozen dataclass holding the planner-safe
  view of one capability: `name`, `description`, `parameters`
  (with `name`, `type`, `required`, `description`), `tags`,
  `dangerous`, `requires_capabilities`, `requires_services`.
* `discover_capabilities(registry)` walks the registry, dropping
  any `requires_capabilities` / `requires_services` reference that
  is not itself a known capability / service (this protects the LLM
  from being asked to depend on a non-existent service).
* `summarize_for_prompt(summaries, ...)` builds a bounded, text-only
  catalog for the LLM system prompt. It enforces a per-capability
  cap (`MAX_SUMMARY_BYTES_PER_CAPABILITY`) and a total cap
  (`MAX_TOTAL_SUMMARY_BYTES`). A capability that would push the
  catalog over the cap is dropped with a warning — the LLM can
  never see a half-rendered catalog.
* `find_capability(summaries, name)` is the planner-side lookup
  helper used by the `LLMPlanner` after a model call to verify the
  capabilities it referenced are real.
* `required_parameter_names(cap_summary)` returns the set of
  parameter names that must be present for an `INVOKE` step.

Discovery never invents capabilities, never rewrites descriptions,
and never adds metadata. The catalog the LLM sees is a faithful
projection of the registry at plan time.

---

## 5. The two planners

### 5.1 `DeterministicPlanner`

A rule-based planner. For each legal `IntentKind`, it knows the
canonical capability to invoke and how to map `Intent.parameters` →
`PlanStep.parameters`. The mapping is intentionally tiny (the
authoritative list lives in `deterministic.py`; see the source for
the full table). It exists so that:

* the dev CLI (`main.py plan <text>`) can demonstrate the full
  Brain pipeline with **no LLM configured**;
* tests can run end-to-end without mocking the provider;
* the orchestrator always has a non-LLM fallback.

It uses the same validation gate as the LLM planner, so the
contract on what a `Plan` looks like is identical regardless of
which planner produced it.

### 5.2 `LLMPlanner`

The LLM-backed planner. It:

1. Builds a system prompt that includes a compact catalog of
   capabilities (via `summarize_for_prompt`).
2. Sends an `LLMRequest` through the Phase 5A `LLMProvider`.
3. Strips JSON fences from the response.
4. Parses the response as a plan payload.
5. Runs the payload through `validate_plan_payload`.
6. Returns the resulting `Plan`.

Provider errors are caught at the boundary and converted to typed
`BrainError` subclasses:

| Provider failure     | Typed error               | Code                          |
|----------------------|---------------------------|-------------------------------|
| Auth / generic       | `ProviderFailure`         | `BRAIN_PROVIDER_FAILURE`      |
| Timeout              | `ProviderTimeout`         | `BRAIN_PROVIDER_TIMEOUT`      |
| Cancelled            | `ProviderCancelled`       | `BRAIN_PROVIDER_CANCELLED`    |
| Malformed JSON       | `ProviderMalformedResponse` | `BRAIN_PROVIDER_MALFORMED`  |

`ProviderMalformedResponse` is **not** silently repaired. If the
LLM produces invalid JSON, the Brain raises. The orchestrator
decides whether to retry, fall back to the deterministic planner,
or surface the error to the user. There is no "best-effort"
recovery in the Brain layer (R-7: never silently repair dangerous
output).

---

## 6. The `Brain` orchestration seam

`Brain.handle_text(text) -> BrainResult` is the public entry point.
It is intentionally a thin layer that:

1. Calls `interpreter.interpret(text, ...)` (the Phase 5B seam).
2. Translates the `Intent` into a `Goal` via `intent.to_goal()`.
3. Calls `planner.plan(goal, intent=..., context_snapshot=...)`.
4. Catches `CannotPlanError` (soft planning failure) and returns
   `BrainResult(status="error", error_code=exc.code, ...)`.
5. Catches any other `BrainError` and re-raises — the orchestrator
   is the right place to decide how to route a hard failure.
6. Returns a structured `BrainResult` for every terminal state.

`BrainResult.status` is one of:

* `"ok"` — `result.plan` is a trusted, validated `Plan`.
* `"clarification"` — the interpreter asked the user a question.
  `result.clarifying_question` is populated.
* `"unknown"` — the interpreter could not classify the input.
* `"error"` — `result.error_code` and `result.error_message` carry
  the structured failure.

The Brain also exposes a `plan(goal, ...)` entry point for the
orchestrator to use on replans and hand-built goals.

---

## 7. What was *not* built

The directive was explicit. Phase 5C+5D does **not** include:

* **No execution path.** The Brain does not call
  `Capability.execute`, does not call `CapabilityRouter`, does not
  import `core.omnix_engine`. Execution belongs to Phase 6.
* **No `ActionRequest` construction.** The Brain produces
  `Plan` / `PlanStep`. The future `PlanExecutor` will turn
  `PlanStep`s into `ActionRequest`s.
* **No retries or fallbacks across planners.** The orchestrator
  decides whether to retry, fall back to the deterministic planner,
  or surface an error. The Brain does not silently fall back.
* **No LLM secrets / provider configuration.** The Brain only sees
  the `LLMProvider` interface from Phase 5A. Configuration belongs
  to the engine boundary.
* **No Windows automation.** The Brain cannot import `pyautogui`,
  `win32gui`, `ctypes`, `subprocess`, or any V6 Windows service.
  This is enforced by `tests/test_brain_isolation.py`.
* **No automatic discovery of new capabilities.** The Brain sees
  exactly what the `CapabilityRegistry` exposes at plan time.

---

## 8. Architectural invariants (enforced by tests)

| Invariant                                                              | Test                                                     |
|------------------------------------------------------------------------|----------------------------------------------------------|
| Brain cannot import `core.omnix_engine`                                | `test_brain_file_has_no_forbidden_imports`               |
| Brain cannot import `core.capability_router`                           | same                                                      |
| Brain cannot import any V6 Windows service                             | same                                                      |
| Brain cannot import `subprocess` / `pyautogui` / `win32gui` / `ctypes` | same                                                      |
| Brain does not wrap forbidden imports in `try/except ImportError`      | `test_brain_does_not_silently_swallow_missing_module_imports` |
| `ai.brain.__init__` docstring states the isolation rule                | `test_brain_package_docstring_mentions_isolation`        |
| `ai.brain` does not export `OmnixEngine`, `CapabilityRouter`, `ActionRequest`, `execute`, `dispatch` | `test_brain_public_surface_has_no_engine_or_router` |
| `ai.brain_manager` does not leak engine symbols                        | `test_brain_manager_does_not_export_engine_or_router`    |
| `LLMPlanner` does not call `.execute(...)` on a capability             | `test_llm_planner_does_not_dispatch_capabilities`        |
| `DeterministicPlanner` does not call `.execute(...)` on a capability   | `test_deterministic_planner_does_not_dispatch_capabilities` |
| `Brain` does not call `.execute(...)` on a capability                  | `test_brain_class_does_not_dispatch_capabilities`        |

The AST-based isolation tests skip string literals, so docstrings
that *mention* the forbidden names do not produce false positives.

---

## 9. Test coverage

* `tests/test_brain.py` — 45 tests covering:
  * Discovery: 3 tests (registry projection, missing fields,
    truncation guard).
  * Validation: 15 tests (shape, capability resolution, parameter
    coercion, required parameters, timeout bounds, retry bounds,
    expected-effect structure, dependency DAG, cycles,
    self-dependency, missing dependency, safety classification,
    size bound, malformed JSON).
  * `DeterministicPlanner`: 5 tests (single-step, multi-step,
    unknown intent kind, parameter pass-through, error path).
  * `LLMPlanner`: 8 tests (provider call, response parsing,
    fence stripping, malformed response, validation failure,
    timeout, cancellation, safety violation).
  * `Brain`: 8 tests (full pipeline, clarification path, unknown
    path, error path, replan entry point, type errors, type
    errors on the `plan` entry point).
  * Architectural invariants: 2 tests (parametrize + AST walk).
* `tests/test_brain_isolation.py` — 24 tests, see §8.

Total Brain-layer tests: **69** (45 functional + 24 isolation).
Full repository regression: **359 passed, 1 skipped**.

---

## 10. Dev CLI integration

`main.py` now exposes a `plan <text>` meta command. It is a
developer-only demonstration of the Brain layer end-to-end:

```
omni> plan open spotify
[plan] id=plan_a1b2c3d4e5f6 goal_id=goal_… steps=1
  1. step_1 invoke -> desktop.application.open
     description: Open the application named spotify.
     parameters : {"app_name": "spotify"}
```

The command wires:

* `LLMIntentInterpreter(MockProvider(), build_default_registry())`
* `DeterministicPlanner(engine.capabilities)`
* `Brain(registry=engine.capabilities, interpreter=..., planner=...)`

It does **not** execute the plan. Execution belongs to Phase 6.
The command is documented in the `main.py` module docstring.

---

## 11. Boundary contract for Phase 6

Phase 6 (Agent / PlanExecutor) will inherit a Brain layer that:

* **Produces a `Plan` it can fully trust.** Every step has been
  validated; capability names are real; parameters coerce;
  dependencies are a DAG; safety classifications are honest;
  timeouts are finite; the plan is ≤ `MAX_PLAN_STEPS`.
* **Surfaces errors as data, not exceptions.** Soft failures
  (cannot plan, clarification required) are returned as
  `BrainResult(status="error"/"clarification", ...)`. Hard failures
  (provider, validation) are raised as `BrainError` subclasses
  with stable `code` values.
* **Cannot accidentally reach Windows.** Even with a misconfigured
  LLM, the Brain cannot import `pyautogui`, `win32gui`, or any
  V6 service. The isolation tests catch this at CI time.
* **Holds exactly one canonical `CapabilityRegistry`.** The Brain
  reads from the registry; it does not own a parallel one. There
  is one source of truth for capabilities (R-3).
* **Never silently repairs dangerous output.** A malformed plan
  payload, a misclassified dangerous operation, or a cycle in
  the dependency graph are all hard failures that the Brain
  surfaces up the stack.

The Phase 6 Agent will consume `Plan` objects and turn each
`PlanStep` into an `ActionRequest` for the `CapabilityRouter`.
That translation is the only place in the V6 architecture that
crosses the brain→engine seam.

---

## 12. Open items

* The `LLMPlanner`'s system prompt is currently auto-generated from
  the capability catalog. A future iteration may add few-shot
  examples; this is a Phase 6 / Phase 7 concern.
* The `DeterministicPlanner` covers the Phase 3 / Phase 4
  capability names. New capabilities require explicit mapping
  updates; this is intentional (the deterministic planner should
  never invent a mapping).
* The Brain does not yet support "streaming" plans (partial
  generation that can be cancelled). Cancellation is supported at
  the `LLMProvider` level; partial `Plan` delivery is not.

---

## 13. Decision

The Brain layer is **strictly read-only**, **strictly validated**,
and **strictly isolated** from the engine. The LLM cannot drive
Windows through the Brain. The next phase can build the
`PlanExecutor` against a stable, tested contract.

**PHASE 5C+5D COMPLETE — BRAIN AND PLANNER VALIDATED. READY FOR PHASE 6.**
