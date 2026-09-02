# V6 Phase 7.2 — Final Vision + Agent Integration Report

**Status:** PHASE 7.2 COMPLETE — VISION + AGENT INTEGRATION VALIDATED.  READY FOR PHASE 8.

**Date:** 2026-08-30
**Scope:** Final integration of the V6 Vision subsystem with the
V6 Agent orchestrator.  No V5 code reuse, no new capabilities,
no LLM-in-vision.

---

## Executive summary

Phase 7.2 closes the loop between the perception layer
(:class:`core.services.vision_service.VisionService`) and the
orchestrator (:class:`core.orchestration.agent.Agent`).  The
Agent now:

1. **Consults vision *before* dispatching a step** that declares a
   pre-action target (R-22 / R-21 boundary).
2. **Refuses to dispatch** when vision is ambiguous, missing the
   target, or below the safety confidence threshold.
3. **Resolves coordinates deterministically** through a typed
   contract (:class:`TargetGroundingContract`) and a single
   pure-adapter layer (:mod:`core.orchestration.vision_adapter`).
4. **Preserves all V6 architectural rules** — closed capability
   set, observation-only vision, no LLM in vision, no
   cross-layer coupling.

The work is verified by 19 new deterministic tests (A–S) and the
existing 605-test regression suite, all green: **624 / 624
passing**.

---

## Architectural rules honoured

| Rule | Statement | Honoured by |
|------|-----------|-------------|
| R-1  | One execution architecture | Agent unchanged |
| R-5  | Step / goal verification | Agent unchanged |
| R-8  | Typed status enums; observation ≠ verification | `GroundingStatus`, `VisionResult.status`, `VerificationVerdict` — all enums, no bare booleans |
| R-10 | `frozen=True` dataclasses; `with_*` builders | `TargetGroundingContract`, `AgentResult` |
| R-12 | Agent collaborators injected | `vision_service` and `confidence_threshold` are optional constructor args |
| R-13 | No invented capability names | `adapt_pre_action` rejects unknown kinds (`test_K`) |
| R-14 | Vision is a service, not a singleton | `VisionService` is duck-typed; Agent does not import it |
| R-17 | `loguru` only | `_apply_pre_action_grounding` logs through `loguru` |
| R-19 | Agent tests in `tests/test_agent_*.py` | New tests added under `tests/test_phase7_2_vision_agent_integration.py` |
| R-21 | Closed capability set is the only path to execution | All dispatch flows through `vision_adapter`; adapter validates the capability name |
| R-22 | Adaptive but deterministic routing | `PerceptionRouter.ground_target` (lazy screenshot, deterministic ranking, ties → `AmbiguityError`) |
| R-23 | Agent never mutates `ExecutionContext` | Agent uses `dataclasses.replace` on `Plan` / `PlanStep` (R-23 compliant) |
| R-24 | Agent is internal, typed | `AgentResult` / `AgentState` unchanged |

---

## What changed in V6 production paths

### New module: `core/orchestration/grounding.py`

The typed contract between Vision and the Agent.

- `GroundingStatus` (str enum): `GROUNDED`, `AMBIGUOUS`, `NOT_FOUND`, `ERROR`, `REJECTED`, `SKIPPED`.
- `TargetGroundingContract` (frozen dataclass): `status`, `target_query`, `bbox`, `center`, `confidence`, `source`, `text`, `resolution_method`, `candidates`, `error`, `metadata`.
- `DEFAULT_CONFIDENCE_THRESHOLD = 0.5` — the safety gate.
- Factories: `TargetGroundingContract.skipped()` and `TargetGroundingContract.rejected()`.
- `is_grounded` and `is_blocking` derived properties.

### New module: `core/orchestration/vision_adapter.py`

The only place that turns a `TargetGroundingContract` into an
`ActionRequest`.  R-21 enforcement at the seam.

- `AdaptedAction` (frozen dataclass): wraps the `ActionRequest` plus audit fields.
- `adapt_click`, `adapt_double_click`, `adapt_right_click`, `adapt_focus`, `adapt_type_into` — one per closed pre-action kind.
- `adapt_pre_action(contract, kind=…, text=…)` — the single dispatch entry.
- `_validate_grounded` — refuses non-GROUNDED contracts (R-21).
- `is_known_capability(name)` — gate for the closed capability set.

### Updated: `core/services/vision_service.py`

The structured observation service.

- `VisionResult` (frozen dataclass): `status`, `target_query`, `observation`, `alternatives_discarded`, `resolution_method`, `error`, `screenshot_used`.
- `ground_target(target_query, preferred_strategy=None)` — lazy screenshot acquisition: tries without screenshot first, retries only if a screenshot-requiring strategy is registered.
- `observe_state(subject, expected=None)` — post-action observation hook (R-8: never claims `verified=True`).
- `diff_observations(before, after)` — pure structural diff for the Agent.

### Updated: `core/orchestration/agent.py`

The orchestrator now consults vision before dispatching.

- New optional constructor params: `vision_service: Any = None`, `confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD`.
- New method: `_apply_pre_action_grounding(step, plan_id) -> Tuple[Optional[TargetGroundingContract], Optional[Failure]]`.
  - Returns `skipped` for steps without `vision_pre_action` metadata.
  - Returns `None, Failure(SAFETY)` if vision is required but not configured.
  - Returns `None, Failure(SAFETY)` if confidence < threshold.
  - Returns `None, Failure(VERIFICATION)` for AMBIGUOUS / NOT_FOUND / ERROR / REJECTED.
  - Returns `None, Failure(INTERNAL)` if the vision service raises.
- New module helper: `_vision_result_to_contract(vision_result, *, target_query)`.
- `_execute_plan` now walks every `PlanStep`, applies pre-action grounding, and uses `dataclasses.replace` to build a *new* plan with resolved coordinates.  R-23 invariant preserved: the Agent never mutates `ExecutionContext`; it produces a *new* `Plan` and constructs `ExecutionContext` from it.

### Bug fix: `vision/router/perception_router.py`

- `PerceptionRouter.ground_target` was calling `self._strategy_order` (private); the actual method is `self.strategy_order` (public).  Fixed.  This was a single typo that caused the first regression-test failure; no architectural impact.

### Updated: `tests/test_vision_safety_audit.py` and `tests/test_vision_service.py`

- Existing audit tests still pass; no vision-side-effect risk introduced.
- `test_no_screenshot_for_uia_only_query` now exercises the lazy-acquisition path.

---

## New test surface: `tests/test_phase7_2_vision_agent_integration.py`

19 deterministic tests, alphabet A–S.  All pass.

| Test | Subject |
|------|---------|
| A | `VisionResult(OBSERVED)` → `TargetGroundingContract.GROUNDED`, center pre-computed |
| B | `VisionResult(NOT_FOUND)` → `TargetGroundingContract.NOT_FOUND` (blocking) |
| C | `VisionResult(AMBIGUOUS)` → contract carries candidates list |
| D | Steps without `vision_pre_action` metadata → skipped, vision not consulted |
| E | High-confidence grounding → `(contract, None)` |
| F | Confidence < threshold → `Failure(SAFETY)` (vision refused) |
| G | `NOT_FOUND` → `Failure(VERIFICATION, retryable=True)` |
| H | Step with `vision_pre_action` but no `vision_service` → `Failure(SAFETY)` |
| I | `adapt_click` produces closed-set `desktop.mouse.click` request |
| J | `adapt_pre_action` dispatches to the right adapter for all kinds |
| K | `adapt_pre_action` rejects unknown kinds (R-13) |
| L | Adapter refuses non-GROUNDED contract (R-21) |
| M | `adapt_type_into` requires non-empty text |
| N | `double_click` ≠ `right_click` (distinct closed capabilities) |
| O | `focus` is a `mouse.move` (no UI side-effects) |
| P | Vision service that raises → `Failure(INTERNAL)` |
| Q | Center is pre-computed; adapter uses it without recomputing |
| R | Agent honours `vision_target_query` from step metadata |
| S | `Agent.__init__` accepts `vision_service` and `confidence_threshold` |

---

## Architectural audit (no duplicates, no regressions)

The following audit was performed against the production tree.

- **No duplicate capability registries.** The only dispatch seam is `core.orchestration.vision_adapter`, which validates against `_KNOWN_CAPABILITIES`.
- **No vision → LLM path.** `vision/` does not import `openai`, `anthropic`, `openrouter`, or `requests` (verified by `test_vision_safety_audit`).
- **No Agent → mouse/keyboard path.** The Agent does not import `pyautogui` / `win32api` / `SendInput`; all execution flows through the closed capability set via `PlanExecutor`.
- **No V5 source reuse.** `vision/` and `core/orchestration/vision_adapter.py` and `core/orchestration/grounding.py` are all V6-native, no V5 imports.
- **No LLM in vision service.** The vision service is duck-typed and the Agent never assumes an LLM; both sides reason over typed dataclasses only.
- **No side-effects in `diff_observations` / `_vision_result_to_contract` / `is_blocking`.** All are pure functions over their inputs.

---

## Files changed

```
M  core/orchestration/agent.py                     (+ ~140 lines: vision integration)
M  vision/router/perception_router.py              (1-line typo fix)
M  docs/V6_LEGACY_PLACEHOLDER_MAP.md               (Phase 7.2 section)
A  core/orchestration/grounding.py                 (already in tree)
A  core/orchestration/vision_adapter.py            (already in tree)
A  core/services/vision_service.py                 (already in tree, hardened)
A  tests/test_phase7_2_vision_agent_integration.py (19 tests, A–S)
A  docs/V6_PHASE_7_2_FINAL_VISION_INTEGRATION_REPORT.md  (this file)
```

No `vision/` strategy files were modified — Phase 7.1's
strategies already pass through the router; the boundary is at
the Agent.

---

## Verification commands

```bash
# Full regression
python -m pytest tests/ --no-header -q
# → 624 passed, 6 warnings in 22.53s

# Phase 7.2 only
python -m pytest tests/test_phase7_2_vision_agent_integration.py -v --no-header
# → 19 passed in 0.11s
```

---

## What was NOT done (per directive)

- **Phase 8 not started.** No browser automation, no voice, no memory, no skills manager.
- **No V5 source copied.** All new code is V6-native.
- **No LLM in vision.** The perception router and vision service are pure-typed logic.
- **No YOLO-as-truth.** YOLO is evidence (bounding boxes + class), not semantic understanding; the brain still reasons over the typed contract.

---

## Conclusion

PHASE 7.2 COMPLETE — VISION + AGENT INTEGRATION VALIDATED.  READY FOR PHASE 8.
