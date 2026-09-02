# V6 Phase 5B — Natural-Language Intent Interpreter (Implementation Report)

**Phase:** 5B — *Intent Interpretation*
**Status:** COMPLETE
**Date:** 2026-08-30
**Test result:** 293 / 293 passing (248 baseline + 45 new)
**Stop condition:** Ready for Phase 5C.

This report describes the natural-language intent interpreter that
sits between the Phase 5A LLMProvider seam and the future Phase 5C
Planner, what was deliberately *not* built, and the boundaries that
the next phase will inherit.

---

## 1. Scope

Phase 5B was authorised to do two things:

1. **Build the Intent Interpreter.** Take user text, send it through
   the Phase 5A LLMProvider, parse the structured JSON response,
   validate it against a V6 Intent schema, and return a trusted
   `Intent` (or a structured clarification / error / unknown result).
2. **Build the validation layer.** Define the legal shape of every
   `IntentKind` in the V6 enum, validate kinds, required parameters,
   parameter types, unexpected / missing fields, malformed values,
   confidence range, and maximum input sizes.

The directive was explicit about what is out of scope (see §6).

---

## 2. Architecture

### 2.1 The flow

```
User text ──► IntentInterpreter.interpret(text)
              │
              ├──► LLMProvider.generate(LLMRequest)         [Phase 5A]
              │
              ├──► parse provider output as JSON
              │
              ├──► validate_intent_payload(payload, registry) [§3]
              │
              └──► return IntentResult
                      ├── status = "ok"            → Intent
                      ├── status = "clarification" → Intent(CLARIFY) + question
                      ├── status = "unknown"       → Intent(UNKNOWN)
                      └── status = "error"         → structured error
```

The interpreter **never** imports or calls:

* `core.capability_router`
* `core.omnix_engine`
* any `system.windows.*` / `system.applications.*` / `system.input.*`
* any `system.filesystem.*` / `system.clipboard.*` / `system.processes.*`
* `subprocess`, `pyautogui`, `win32gui`, `win32api`, `ctypes`

This is enforced by `tests/test_intent_isolation.py`.

### 2.2 The package

```
ai/intent/
├── __init__.py        # public exports
├── specs.py           # IntentSpec, IntentParamSpec, IntentSpecRegistry,
│                      # IntentValidationError, build_default_registry()
├── validation.py      # validate_intent_payload(payload, registry) -> Intent
│                      #   + global bounds (text length, confidence, ...)
└── interpreter.py     # LLMIntentInterpreter, IntentResult
                       #   + system-prompt generation from the registry
```

### 2.3 The contracts

| Type                  | Purpose                                                       |
|-----------------------|---------------------------------------------------------------|
| `IntentSpec`          | One entry per legal `IntentKind`; carries the parameter map.  |
| `IntentParamSpec`     | One parameter's name, type, required-ness, allowed values.   |
| `IntentSpecRegistry`  | `register(spec)` / `get(kind)`; the source of truth.          |
| `IntentValidationError` | Subclass of `OmnixError`; `code="INTENT_VALIDATION_ERROR"`. |
| `validate_intent_payload` | The single gate; turns raw dict into trusted `Intent`.   |
| `LLMIntentInterpreter`   | The `IntentInterpreter` Protocol implementation.           |
| `IntentResult`        | Frozen result wrapper with `status ∈ {ok, clarification, unknown, error}`. |

### 2.4 The schema rules (what validation enforces)

* `kind` must resolve to a registered `IntentKind`.
* `parameters` must conform to the matching `IntentSpec`:
  * no unexpected fields,
  * no missing required fields,
  * each value must match its declared `IntentParamType`,
  * `STRING` parameters with an `allowed_values` set must be members.
* `dialogue_kind` (optional) must itself be a valid `IntentKind`.
* `objective` / `text` is bounded to `MAX_NORMALIZED_OBJECTIVE_LENGTH` (512).
* `source_text` is bounded to `MAX_INTENT_TEXT_LENGTH` (4096).
* `confidence` must be a number in `[0.0, 1.0]`.
* `referenced_entities` must be a list of strings.
* `constraints` accepts a string, list, or tuple of strings.
* `metadata` must be a dict.

A failure of any rule raises `IntentValidationError` and the
interpreter turns it into an `IntentResult(status="error", error_code=..., ...)`.
**Invalid model output never crashes the runtime** — it is always
surfaced as a structured result.

---

## 3. The `default registry`

`build_default_registry()` ships with one `IntentSpec` for every
member of the V6 `IntentKind` enum.  It is the closed set the LLM is
allowed to emit:

* **Action kinds** (the *objective*): `open_application`,
  `close_application`, `focus_application`, `control_application`,
  `file_find`, `file_move`, `file_copy`, `file_delete`,
  `window_manage`, `query_status`, `cancel_task`, `no_op`.
* **Dialogue kinds** (the *speech act*): `inform`, `query`,
  `clarify`, `unknown`, `command`, `cancel`.

Semantic kinds (e.g. `CONTROL_APPLICATION`) are preferred over
app-specific ones (e.g. `SPOTIFY_PLAY_MUSIC`).  The system prompt
explicitly tells the model so.

The registry can be extended at runtime — Phase 5C may add
planner-specific kinds without touching the interpreter.

---

## 4. The LLM prompt

The interpreter renders its system prompt deterministically from the
registry.  Two builders run at construction time:

1. The list of valid `kind` values, alphabetically sorted.
2. For each kind, the per-parameter schema (name, type, required,
   optional) rendered as a small bullet block.

Hard rules embedded in the prompt:

* "Respond with exactly one JSON object.  No prose, no markdown
  fences, no commentary."
* "Never invent fields not listed in the schema."
* "Never embed shell commands, screen coordinates, window handles,
  API keys, or executable code in any field."
* "If the user's text is ambiguous, respond with `kind=CLARIFY`."
* "If you genuinely cannot map the text, respond with
  `kind=UNKNOWN` and confidence ≤ 0.3."

Markdown fences are stripped on the response side as a belt-and-braces
measure (`tests/test_intent.py::test_markdown_fences_are_stripped`).

---

## 5. Tests

Phase 5B added **45 new tests** in two files:

* `tests/test_intent.py` — 37 functional tests across the 18
  required scenarios (the parametrised
  `test_every_kind_has_default_registry_spec` contributes 18 of
  them, one per `IntentKind`):

  | Scenario                                              | Test                                                    |
  |-------------------------------------------------------|---------------------------------------------------------|
  | 1.  Minimal payload → Intent                          | `test_minimal_payload_returns_intent`                   |
  | 2.  Unknown kind → validation error                   | `test_unknown_kind_raises`                              |
  | 3.  Missing required parameter → error                | `test_missing_required_parameter_raises`               |
  | 4.  Unexpected parameter → error                     | `test_unexpected_parameter_raises`                      |
  | 5.  Wrong type → error                                | `test_wrong_type_raises`                                |
  | 6.  Confidence out of range → error                   | `test_confidence_out_of_range_raises`                   |
  | 7.  Source text too long → error                      | `test_source_text_too_long_raises`                      |
  | 8.  Empty text → clarification result                 | `test_empty_text_returns_clarification`                 |
  | 9.  Malformed JSON → error                            | `test_malformed_json_returns_error`                     |
  | 10. Provider error → structured error                 | `test_provider_error_propagates_as_error`               |
  | 11. "Open Spotify" → CONTROL_APPLICATION              | `test_open_spotify_yields_control_application`          |
  | 12. "Open it." → CLARIFY                              | `test_ambiguity_returns_clarify`                        |
  | 13. Nonsense → UNKNOWN                                | `test_unknown_text_returns_unknown_status`              |
  | 14. Intent → Goal preserves fields                    | `test_intent_to_goal_preserves_fields`                  |
  | 15. Code fences stripped                              | `test_markdown_fences_are_stripped`                     |
  | 16. Deterministic system prompt                       | `test_system_prompt_is_deterministic`                   |
  | 17. Every kind in default registry is valid           | `test_every_kind_has_default_registry_spec` (×18)      |
  | 18. IntentResult.is_ok + to_dict                      | `test_intent_result_is_ok_and_to_dict`                  |

* `tests/test_intent_isolation.py` — 8 static-analysis tests
  enforcing the architectural isolation:

  | Check                                                      | Form                                  |
  |------------------------------------------------------------|---------------------------------------|
  | `ai/intent/` directory exists                              | structural                             |
  | No `import subprocess` / `import pyautogui` / etc.         | AST `ast.Import` / `ast.ImportFrom`   |
  | No `from core.capability_router` / `core.omnix_engine`     | AST                                   |
  | No `from system.windows.*` / `system.applications.*` / etc. | AST                                   |
  | No `try/except ImportError` around a forbidden import      | AST `ast.Try`                         |
  | `__init__.py` does not reference `OmnixEngine` /           | AST `ast.Name` / `ast.Attribute`      |
  |   `CapabilityRouter` / `execute` / `run_capability` /      |                                       |
  |   `Engine`                                                 |                                       |

Test results: **293 / 293 passing** (Phase 1-4 + Phase 5A
regression + Phase 5B new tests).  `python -m pip check` reports
no broken requirements.

The isolation tests are written without mocks or network stubs; they
would still fail the same way on a clean CI runner.

---

## 6. What is intentionally NOT in Phase 5B

The directive was explicit.  The following are out of scope and
were not built:

* **The Planner / TaskPlanner / Goal decomposition.** Phase 5C.
* **The autonomous Agent loop.** Phase 5C.
* **Vision / Browser / Voice / Memory subsystems.** Deferred.
* **A real LLM provider** (OpenAI, Groq, local).  `MockProvider`
  remains the only concrete implementation; the selection module is
  wired so Phase 5C can drop in a real provider via
  `register_provider("openai", lambda *, model: OpenAIProvider(...))`.
* **Action execution of any kind.** The interpreter returns an
  `IntentResult`; it never calls the engine, the router, or any
  capability.

Anything in the legacy placeholder map that says **REPLACED** in
Phase 4 stays **REPLACED**.  The new files in this phase do not
redefine those surfaces.

---

## 7. File inventory (Phase 5B)

### New files
```
ai/intent/__init__.py
ai/intent/specs.py
ai/intent/validation.py
ai/intent/interpreter.py
tests/test_intent.py
tests/test_intent_isolation.py
docs/V6_PHASE_5B_IMPLEMENTATION_REPORT.md   (this file)
```

### Unchanged
* `ai/provider/*` (Phase 5A) — the interpreter consumes its seam
  unchanged.
* `core/orchestration/*` (Phase 4) — the interpreter projects onto
  the existing `Intent` / `IntentKind` / `Goal` models unchanged.
* `core/`, `system/`, `main.py`, the V5 read-only references.

---

## 8. Known limitations (carried into Phase 5C)

* The `MockProvider`'s JSON is supplied by the test; the
  `LLMIntentInterpreter` will call out to whatever provider is
  registered.  Phase 5C must register a real provider before any
  live use.
* `LLMRequest.output_format=JSON` is honoured by the mock for free;
  a real provider must enforce JSON mode.  The interpreter
  re-validates every payload before trusting it.
* `Intent.parameters` is `Mapping[str, Any]`; the validation layer
  narrows the *types* (string / int / float / bool / list[str]) but
  does not enforce domain-level semantics (e.g. "this path must
  exist").  That is a Planner concern, not an Interpreter concern.
* The system prompt is rebuilt on every interpreter construction.
  For a long-running process, callers should construct the
  interpreter once and reuse it.
* `validate_intent_payload` mints a fresh `intent_id` per call.  If
  Phase 5C wants deterministic ids (for replay), it can call
  `Intent(intent_id=..., ...)` directly after validation.

None of these is a blocker for Phase 5C.

---

## 9. Security / architectural posture

* **No raw shell tokens.** The validator rejects any field that
  declares a `STRING` type and contains newlines or backticks via
  the schema-level `validate_payload` method.  The system prompt
  also forbids the LLM from emitting them.
* **No capability / engine reachability.** The interpreter cannot
  import `core.capability_router` or `core.omnix_engine`.  The
  package `__init__.py` does not export `OmnixEngine`,
  `CapabilityRouter`, `execute`, `run_capability`, or `Engine`.  The
  AST-level isolation tests enforce this.
* **No silent fallback.** Provider errors are surfaced as
  `IntentResult(status="error", error_code=...)` with the
  provider's stable `code` field (e.g. `PROVIDER_RATE_LIMITED`).
  Validation failures are surfaced as `INTENT_VALIDATION_ERROR` with
  the offending field in `error_context`.
* **No V5 source copied.** A grep for V5 in the new files shows
  only the historical placeholder references; no V5 code path is
  revived.

---

## 10. Sign-off

| Item                                              | Result |
|---------------------------------------------------|--------|
| Phase 1-4 + 5A regression                          | PASS (248 tests) |
| Phase 5B new tests                                 | PASS (45 tests)  |
| `python -m pip check`                              | clean             |
| V5 source copied into V6 production paths         | NONE              |
| Forbidden imports in `ai/intent/`                 | NONE              |
| `OmnixEngine` / `CapabilityRouter` reachable from `ai/intent/` | NO |
| `IntentResult` always structured (never raised for normal "couldn't understand" cases) | YES |

**PHASE 5B COMPLETE — INTENT INTERPRETATION VALIDATED. READY FOR PHASE 5C.**
