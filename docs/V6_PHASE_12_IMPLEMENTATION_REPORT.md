# OMNIX V6 — PHASE 12: REAL AUTOMATION EXECUTION LAYER

**Status:** PHASE 12 COMPLETE — canonical execution path is real, deterministic,
and safe; Phase 12 deliverables are wired into the existing V6 architecture
without duplicating any subsystem.

**Scope:** Make the existing V6 execution architecture capable of performing
real automation tasks through its canonical execution path:

```
User → Intent → Goal → Brain → Planner → Plan → Agent
    → PlanExecutor → CapabilityRouter → Capability → V6 Service
    → Windows / Browser / Filesystem → Observation → Verification
    → Recovery → OmnixResponse
```

Phase 12 is **NOT** a redesign.  It is a *completion* of the surfaces that
Phases 1–11 left as contracts or placeholders:

* The standard filesystem capability set was missing `create`, `delete`,
  `folder.create`, and `directory.list`.  Phase 12 adds them as concrete
  capabilities, each behind the canonical Router.
* The standard process capability set had no read-only observation primitive.
  Phase 12 adds `process.is_running` for verifying that a launch actually
  worked.
* The `PlanExecutor` had an `observability_sink` for test introspection, but
  no canonical wiring to the engine's `EventBus`.  Phase 12 adds an
  `event_bus=` keyword that publishes `REQUEST_ACTION_EXECUTED`,
  `REQUEST_OBSERVATION_CAPTURED`, and `REQUEST_RECOVERY_STARTED` events
  through the same bus the pipeline already uses.
* The `CapabilitySpec.parameters` field was declared as a tuple, but every
  concrete capability in the codebase registered a dict.  Phase 12 hardens
  the router to accept either form (the canonical form, and the historical
  dict form) so the existing capabilities are now actually dispatchable
  through the canonical Router path.
* The `DeterministicPlanner` previously mapped `file_delete` to
  `file.read` -- a placeholder that no real automation can act on.  Phase
  12 maps it to the new `file.delete` capability with a
  `requires_intent_params=("path",)` discipline.

Phase 12 is **NOT** Phase 13.  It does not introduce vision-grounded action
targeting, does not start final bug-fix / hardening work, and does not
modify the user's `main.py` automation surface.

**Date:** 2026-08-31

---

## 1. Architecture map (Phase 12)

The Phase 12 work sits in five files.  No new engines, brains, planners,
agents, routers, registries, services, or pipelines were created -- the
existing canonical V6 surface was extended.

| File | What changed | Why |
| --- | --- | --- |
| `core/capability.py` | `coerce_parameters` now accepts `spec.parameters` as either a tuple of `CapabilityParameter` (the canonical form) or a dict mapping `name → CapabilityParameter` (the historical form). | The dataclass field was declared as `tuple`, but every concrete capability registered a dict.  The router silently could not dispatch any of them.  Phase 12 makes the router the source of truth and accepts both. |
| `core/capabilities/filesystem.py` | Added `FileCreateCapability`, `FolderCreateCapability`, `FileDeleteCapability`, `DirectoryListCapability`. | The standard set previously only had `file.read` and `file.write` -- the surface the user actually needs to create / delete / inspect files did not exist. |
| `core/capabilities/process.py` | Added `ProcessIsRunningCapability` (read-only observation). | The standard set had `process.run` (dangerous) and no read-only observation; the Agent had no canonical way to *verify* a launch worked. |
| `core/capabilities/__init__.py` | Registered the new capabilities. | Standard capability set is now 30 capabilities (was 25). |
| `core/orchestration/plan_executor.py` | New optional `event_bus=` keyword; new `publish_recovery_started` method.  Publishes `REQUEST_ACTION_EXECUTED` and `REQUEST_OBSERVATION_CAPTURED` for every dispatched step. | Closes the canonical observability loop: the bus now sees every action the executor runs, not just the boundaries the Brain / Pipeline touch. |
| `core/omnix_engine.py` | Wires the engine's `EventBus` into the `PlanExecutorImpl` at construction time. | Makes the new bus publishing actually happen in production paths; tests can still inject a custom bus. |
| `ai/brain/deterministic.py` | `file_delete` now maps to `file.delete` with a `requires_intent_params=("path",)` discipline. | The previous mapping (`file.read`) was a placeholder that no real automation can act on. |

## 2. New capabilities (Phase 12)

### `file.create`  --  create an empty file

| Property | Value |
| --- | --- |
| Dangerous | False |
| Tags | `filesystem`, `create`, `destructive` |
| Parameters | `path: path` (required), `overwrite: boolean` (default `False`) |
| Returns | `VERIFIED` with `details={path, bytes_written=0, overwrite}` |
| Refuses to | overwrite an existing file unless `overwrite=True`; create over a directory |
| Tested in | `tests/test_phase12_real_automation.py` (3 cases) |

### `folder.create`  --  create a directory tree (idempotent)

| Property | Value |
| --- | --- |
| Dangerous | False |
| Tags | `filesystem`, `create`, `destructive` |
| Parameters | `path: path` (required) |
| Returns | `VERIFIED` regardless of whether the directory already existed (idempotent `mkdir -p` semantics) |
| Refuses to | create a directory over an existing file |
| Tested in | `tests/test_phase12_real_automation.py` (2 cases) |

### `file.delete`  --  delete a file or empty directory

| Property | Value |
| --- | --- |
| Dangerous | True (safety layer must authorise) |
| Tags | `filesystem`, `delete`, `destructive` |
| Parameters | `path: path` (required) |
| Returns | `VERIFIED` on success; `FAILED` with a safe error message otherwise |
| Refuses to | delete anything under `%SystemRoot%`, `%ProgramFiles%`, or `%ProgramFiles(x86)%`; delete a non-empty directory |
| Helper | `_is_under_reserved_dir` (case-insensitive on Windows) |
| Tested in | `tests/test_phase12_real_automation.py` (3 cases) |

### `directory.list`  --  list the entries of a directory (read-only)

| Property | Value |
| --- | --- |
| Dangerous | False |
| Tags | `filesystem`, `read`, `observation` |
| Parameters | `path: path` (required), `include_hidden: boolean` (default `False`) |
| Returns | `VERIFIED` with `details={path, entries, count}` |
| Tested in | `tests/test_phase12_real_automation.py` (2 cases) |

### `process.is_running`  --  read-only process observation

| Property | Value |
| --- | --- |
| Dangerous | False |
| Tags | `process`, `observation`, `read` |
| Parameters | `name: string` (required) |
| Returns | `VERIFIED` always; `details={name, running, pids, platform}` |
| Implementation | `psutil` (already a project dep) with a `tasklist` fallback for hosts without `psutil` |
| Tested in | `tests/test_phase12_real_automation.py` (3 cases) |

## 3. Capability router hardening (Phase 12)

The `CapabilitySpec.parameters` field was declared as `tuple`, but every
concrete capability in the codebase registered a dict.  The router was
silently unable to dispatch any of them through the canonical path.
Phase 12 hardens `coerce_parameters` to accept either form:

* **Tuple of `CapabilityParameter`** -- the canonical form, declared in
  the dataclass.
* **Dict mapping `name → CapabilityParameter`** -- the historical form,
  used by every existing capability because it is more convenient at
  the call site.

The router's validation still rejects:

* unknown parameters (`CAPABILITY_PARAM_UNKNOWN`),
* missing required parameters (`CAPABILITY_PARAM_MISSING`),
* type mismatches (`CAPABILITY_PARAM_TYPE`),
* range violations (`CAPABILITY_PARAM_RANGE`),
* enum violations (`CAPABILITY_PARAM_ENUM`).

The two forms are interchangeable from the caller's perspective: an
existing capability that registered `parameters={...}` continues to
work, and a new capability can choose either form.

## 4. Event-bus observability (Phase 12)

The pipeline already published the canonical request stages
(`REQUEST_RECEIVED`, `REQUEST_INTENT_RESOLVED`, `REQUEST_PLAN_CREATED`,
`REQUEST_EXECUTION_STARTED`, `REQUEST_VERIFICATION_COMPLETED`,
`REQUEST_REPLAN_STARTED`, `REQUEST_COMPLETED`, `REQUEST_CANCELLED`,
`REQUEST_TIMED_OUT`, `REQUEST_REJECTED`).  What it did NOT publish
was what happened *between* `REQUEST_EXECUTION_STARTED` and
`REQUEST_VERIFICATION_COMPLETED`.

Phase 12 closes that gap by adding three new bus emissions inside the
`PlanExecutor`:

* `REQUEST_ACTION_EXECUTED` -- emitted after every dispatched step.
  Carries the canonical `correlation_id`, the plan id, the step id,
  the capability name, the capability status, and the error (if any).
* `REQUEST_OBSERVATION_CAPTURED` -- emitted when the capability's
  result has a `details` payload (the canonical observation
  surface).  Carries the keys of the details dict, not the values
  themselves, so secrets in details never reach the bus.
* `REQUEST_RECOVERY_STARTED` -- emitted by the new
  `PlanExecutor.publish_recovery_started` helper.  The recovery
  engine (and tests) call it when a step failure triggers a
  recovery action.

All three publications are best-effort: if the bus is missing or
raises, the executor keeps running.  Observability must never break
the canonical path.

The engine wires the bus at construction time:

```python
plan_executor = PlanExecutorImpl(
    router=self.router,
    event_bus=self.bus,  # Phase 12: bus publishing
)
```

Tests that do not want a bus can still construct the executor
without one.

## 5. Deterministic planner: real `file_delete` mapping

The previous deterministic-planner rule for `file_delete` was
`{"name": "file.read", ...}` -- a placeholder that no real
automation could act on.  Phase 12 replaces it with:

```python
"file_delete": [
    {
        "name": "file.delete",
        "param_overrides": {},
        "requires_intent_params": ("path",),
    },
],
```

The discipline is the same as the existing browser-intent rules:
the deterministic planner never invents a path.  If the
:class:`Intent` does not carry a non-empty `path` parameter, the
planner raises :class:`CannotPlanError` and the orchestrator falls
back to the LLM planner.

The LLM planner is unaffected by this change; it always had
direct access to `file.delete` once the capability was registered.

## 6. Tests

Phase 12 adds one new test file:

* `tests/test_phase12_real_automation.py` -- 27 deterministic tests:
  3 for parameter-coercion hardening, 10 for the new filesystem
  capabilities, 3 for the new process capability, 4 for the router
  dispatching the new capabilities, 2 for the deterministic planner
  mapping, 2 for the bus publishing, and 3 for the standard
  registration of the new capabilities.

Phase 12 also adds one script:

* `scripts/phase12_real_windows_smoke.py` -- a selectable real-Windows
  smoke harness.  Each individual test is opt-in
  (`--tests fs.create,fs.delete,proc.is_running`).  The smoke is
  Windows-only (`os.name == "nt"`); on a non-Windows host it
  exits with code 2 and a clear message.

## 7. What Phase 12 is NOT

To be explicit about scope:

* **NOT** Phase 13.  No vision-grounded action targeting.
* **NOT** a redesign.  Every Phase 12 change is an extension to an
  existing V6 module.
* **NOT** a `main.py` automation implementation.  `main.py` is
  unchanged.
* **NOT** a safety weakening.  `file.delete` is `dangerous=True`
  and is still subject to the canonical dangerous-authorization
  gate; the new `directory.list`, `file.create`, and
  `process.is_running` capabilities are observation or
  non-mutating.
* **NOT** a test that proves Windows automation works.  A green
  pytest collection is a *necessary* signal that the wiring is
  consistent, not a proof that real Windows automation
  succeeds.  The real-Windows smoke exists to make that
  distinction explicit.

## 8. Definition of done (Phase 12)

* [x] Audit of the current V6 execution path completed and
      documented (this report).
* [x] `coerce_parameters` accepts both `tuple` and `dict` forms
      of `spec.parameters`.
* [x] Standard filesystem capability set includes
      `file.create`, `folder.create`, `file.delete`, and
      `directory.list`.
* [x] Standard process capability set includes the read-only
      `process.is_running` observation primitive.
* [x] `PlanExecutor` publishes the canonical
      `REQUEST_ACTION_EXECUTED` / `REQUEST_OBSERVATION_CAPTURED`
      events when an `event_bus` is wired in; the engine wires
      its own bus at construction time.
* [x] `DeterministicPlanner`'s `file_delete` rule maps to the
      real `file.delete` capability with a
      `requires_intent_params=("path",)` discipline.
* [x] 27+ deterministic Phase 12 tests in
      `tests/test_phase12_real_automation.py`.
* [x] Real-Windows smoke script with selectable tests
      (`scripts/phase12_real_windows_smoke.py`).
* [x] V5 source code audit confirmed no V5 code is used in
      Phase 12 paths (the existing V5 audit report stands).
* [x] Phase 12 report written (this file) and roadmap
      pointer updated.
