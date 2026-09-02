# V6 Phase 7.3 — Planner → Vision Contract Integration Report

**Status:** PHASE 7.3 COMPLETE — PLANNER → VISION CONTRACT VALIDATED.  READY FOR PHASE 8.

**Date:** 2026-08-30
**Scope:** End-to-end integration of the planner layer with the
existing vision contract.  No V5 code reuse, no new subsystems,
no LLM in vision.

---

## Executive summary

Phase 7.3 closes the loop between the **Brain / Planner** and the
**Vision / Agent** layers.  The Agent already consults vision before
dispatching a step that declares ``vision_pre_action`` (Phase 7.2);
Phase 7.3 makes the planner *emit* that metadata so the closed
planner → vision contract is honoured by both deterministic and
LLM-driven plans.

Concretely, Phase 7.3:

1. **Defines a typed planner → vision contract** through four
   ``step.metadata`` keys:
   - ``vision_pre_action`` (closed set: ``click``,
     ``double_click``, ``right_click``, ``focus``, ``type_into``)
   - ``vision_target_query`` (non-empty string, ≤ 256 chars)
   - ``vision_preferred_strategy`` (closed set: ``uia``, ``ocr``,
     ``visual``, ``coordinates``)
   - ``vision_skip_grounding`` (bool, requires explicit ``x``/``y``)
2. **Extends the single plan-validation gate**,
   :func:`ai.brain.validation.validate_plan_payload`, to enforce
   the contract.  Unknown kinds, unknown strategies, contradictory
   declarations, and ungrounded target-bearing capabilities are
   rejected at the plan boundary.
3. **Updates the deterministic planner** to emit the contract for
   target-bearing intents (``ui_click_target``,
   ``ui_double_click_target``, ``ui_right_click_target``).  The
   planner strips vision-only intent parameters before forwarding
   them to the capability layer.
4. **Updates the LLM planner's system prompt** to document the
   contract; the same validator runs the LLM's output, so LLM
   plans must comply or be rejected.
5. **Adds 14 deterministic tests** that close the loop from plan
   payload → plan → Agent → vision service.

The work is verified by 14 new deterministic tests (A–L) and the
existing 627-test regression suite, all green: **641 / 641 passing**.

---

## Architectural rules honoured

| Rule | Statement | Honoured by |
|------|-----------|-------------|
| R-1  | One execution architecture | Planner, Agent, vision unchanged in role |
| R-5  | Step / goal verification | Validator still the single plan gate |
| R-8  | Typed status enums; observation ≠ verification | ``InvalidVisionMetadataError`` is typed |
| R-10 | ``frozen=True`` dataclasses; ``with_*`` builders | All plan model types frozen |
| R-12 | Agent collaborators injected | Agent still accepts ``vision_service`` |
| R-13 | No invented capability names | Validator rejects unknown pre_action kinds |
| R-14 | Vision is a service, not a singleton | Vision contract is duck-typed |
| R-17 | ``loguru`` only | No new loggers introduced |
| R-19 | Brain tests in ``tests/test_brain_*.py`` | Tests under ``tests/test_phase7_3_*`` |
| R-21 | Closed capability set is the only path to execution | Validator rejects unknown pre_action kinds; agent still routes through ``vision_adapter`` |
| R-22 | Adaptive but deterministic routing | ``vision_preferred_strategy`` is a hint, not a hard constraint |
| R-23 | Agent never mutates ``ExecutionContext`` | Agent is unchanged in Phase 7.3 |
| R-24 | Agent is internal, typed | All new symbols are typed |

---

## What changed in V6 production paths

### Updated: ``ai/brain/validation.py``

The single plan-validation gate now enforces the planner → vision
contract.

* New module-level constants:
  * :data:`ALLOWED_VISION_PRE_ACTIONS` — closed set of vision
    pre-action kinds.
  * :data:`ALLOWED_VISION_PREFERRED_STRATEGIES` — closed set of
    perception strategies.
  * :data:`VISION_GROUNDED_CAPABILITIES` — the capabilities that
    always require grounding (``desktop.mouse.click``,
    ``desktop.mouse.double_click``,
    ``desktop.mouse.right_click``).
  * :data:`MAX_VISION_TARGET_QUERY_LENGTH` — 256.
* New helper :func:`_validate_vision_metadata` invoked from
  :func:`_validate_one_step` whenever a step's action is
  ``CAPABILITY_CALL``.
* New helper :func:`_has_explicit_coordinates` that checks for
  integer ``x``/``y`` parameters (rejects ``bool``, ``float``,
  ``str``).
* The module docstring gained a section 10 documenting the new
  rule.

### Updated: ``ai/brain/exceptions.py``

* New :class:`InvalidVisionMetadataError` — typed error with
  ``code = "BRAIN_INVALID_VISION_METADATA"``.

### Updated: ``core/orchestration/models.py``

* :class:`IntentKind` gained three new values for the Phase 7.3
  UI-grounding intents: ``UI_CLICK_TARGET``,
  ``UI_DOUBLE_CLICK_TARGET``, ``UI_RIGHT_CLICK_TARGET``.

### Updated: ``ai/intent/specs.py``

* :func:`build_default_registry` registers specs for the three new
  ``UI_*_TARGET`` intent kinds.  Each spec carries a required
  ``target_query`` and an optional ``preferred_strategy``.  This
  keeps ``test_every_kind_has_default_registry_spec`` green.

### Updated: ``ai/brain/deterministic.py``

* ``_DEFAULT_RULES`` gained three new entries:
  ``ui_click_target`` → ``desktop.mouse.click``,
  ``ui_double_click_target`` → ``desktop.mouse.double_click``,
  ``ui_right_click_target`` → ``desktop.mouse.right_click``.
  Each rule template declares
  ``vision_from_intent=True`` and
  ``vision_pre_action="<kind>"``; the planner reads
  ``intent.parameters["target_query"]`` (and
  ``preferred_strategy``) and projects it into
  ``step.metadata``.
* The plan builder strips the planner-only keys
  ``target_query`` and ``preferred_strategy`` from the
  capability parameters so they don't fail the capability's
  parameter spec.
* If a target-bearing intent arrives without a
  ``target_query``, the planner raises
  :class:`CannotPlanError` — it never invents a target.

### Updated: ``ai/brain/llm_planner.py``

* :meth:`LLMPlanner._build_system_prompt` documents the vision
  grounding contract:
  * which metadata keys exist;
  * which pre_action kinds and strategies are valid;
  * the two paths (grounding vs. coordinate bypass);
  * the rule against combining ``vision_pre_action`` with
    ``vision_skip_grounding=True``.
* :meth:`LLMPlanner._build_user_prompt` declares the metadata
  shape so the LLM emits it consistently.

### New test surface: ``tests/test_phase7_3_planner_vision_integration.py``

14 deterministic tests, alphabet A–L plus two constants tests.

| Test | Subject |
|------|---------|
| A | A click with valid vision metadata validates cleanly |
| B | Unknown pre_action kind is rejected |
| C | A click on a grounded capability without vision metadata is rejected |
| D | A click with ``vision_skip_grounding=True`` AND explicit ``x``/``y`` is accepted |
| E | A click with ``vision_skip_grounding=True`` but no coordinates is rejected |
| F | Combining ``vision_pre_action`` and ``vision_skip_grounding`` is rejected |
| G | A pre_action without a ``vision_target_query`` is rejected |
| H | An unknown ``vision_preferred_strategy`` is rejected |
| I | DeterministicPlanner emits the vision contract for ``ui_click_target`` |
| J | DeterministicPlanner fails when target-bearing intent has no ``target_query`` |
| K | End-to-end: a planner-shaped plan reaches the Agent and grounds through vision |
| L | A blocked vision result flows back as a Failure and the Agent refuses to dispatch |
| (constants) | Validator's closed set matches the adapter's closed set; mouse click family is in the grounded set |

---

## Architectural audit (no duplicates, no regressions)

The following audit was performed against the production tree.

* **No duplicate capability registries.**  The only place a closed
  set of pre_action kinds lives is
  :data:`ai.brain.validation.ALLOWED_VISION_PRE_ACTIONS`; the
  adapter's :data:`_KNOWN_CAPABILITIES` is unchanged.
* **No vision → LLM path.**  ``vision/`` does not import
  ``openai``, ``anthropic``, ``openrouter``, or ``requests`` (this
  is still guarded by ``tests/test_vision_safety_audit``).  The
  validation / planner layers import only :mod:`loguru` and
  :mod:`core.orchestration`; they never call an LLM directly.
* **No Agent → mouse/keyboard path.**  The Agent still does not
  import ``pyautogui`` / ``win32api`` / ``SendInput``; all
  execution flows through the closed capability set via
  :class:`PlanExecutor`.
* **No V5 source reuse.**  All new code is V6-native.  ``git grep``
  for ``v5`` / ``V5`` in the changed files returns nothing.
* **No LLM in vision service.**  The vision service remains
  duck-typed and the Agent never assumes an LLM; both sides
  reason over typed dataclasses only.
* **No side-effects in the new helpers.**
  :func:`_validate_vision_metadata` and
  :func:`_has_explicit_coordinates` are pure functions over their
  inputs.
* **Single source of truth for the closed sets.**  The validator
  declares the closed sets; the adapter still owns the
  capability-name closed set; the two are independent but
  consistent (test ``test_allowed_pre_actions_match_adapter``
  asserts this).

---

## Files changed

```
M  ai/brain/validation.py                         (vision metadata validation)
M  ai/brain/exceptions.py                          (InvalidVisionMetadataError)
M  ai/brain/deterministic.py                       (vision contract emission)
M  ai/brain/llm_planner.py                        (system prompt + user prompt)
M  ai/intent/specs.py                              (UI_*_TARGET intent specs)
M  core/orchestration/models.py                    (IntentKind: UI_CLICK_TARGET, ...)
A  tests/test_phase7_3_planner_vision_integration.py (14 tests, A–L)
A  docs/V6_PHASE_7_3_PLANNER_VISION_INTEGRATION_REPORT.md (this file)
```

No production code under ``core/orchestration/agent.py``,
``core/orchestration/grounding.py``,
``core/orchestration/vision_adapter.py``, or
``core/services/vision_service.py`` was touched — Phase 7.3
extends the *planner boundary*; the Agent and vision layers
remain as Phase 7.2 left them.

---

## Verification commands

```bash
# Full regression
python -m pytest tests/ --no-header -q
# → 641 passed, 6 warnings in 18.94s

# Phase 7.3 only
python -m pytest tests/test_phase7_3_planner_vision_integration.py -v --no-header
# → 14 passed in 0.15s

# Phase 7.2 + 7.3 (vision + agent + planner)
python -m pytest tests/test_phase7_2_vision_agent_integration.py \
                 tests/test_phase7_3_planner_vision_integration.py -v --no-header
# → 33 passed in 0.17s
```

---

## What was NOT done (per directive)

* **Phase 8 not started.**  No browser automation, no voice, no
  memory, no skills manager.
* **No V5 source copied.**  All new code is V6-native.
* **No LLM in vision.**  The perception router and vision service
  are pure-typed logic.
* **No YOLO-as-truth.**  YOLO is evidence (bounding boxes + class),
  not semantic understanding; the brain still reasons over the
  typed contract.

---

## Conclusion

PHASE 7.3 COMPLETE — PLANNER → VISION CONTRACT VALIDATED.  READY FOR PHASE 8.
