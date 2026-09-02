# V6 Architecture Rules

**Phase:** 0 — Forensic Audit
**Status:** Complete
**Date:** 2026-08-29
**Source of truth:** `My Goal for Omnix.md` + V5 `core/omnix_engine.py` (3637 L, 49 methods).

---

## 1. Purpose

This document is the **architectural rulebook** for Omnix V6. It captures the invariants that the V6 codebase must honor so that every new feature moves the system toward the goal in `My Goal for Omnix.md`, not away from it.

These rules are **binding for Phase 0.5+**. Any change that violates them requires an addendum to `V5_V6_MIGRATION_AUDIT.md` and explicit user approval.

---

## 2. The One Diagram (V6)

```
                 USER
                   │
                   ▼
             "HEY OMNIX"
                   │
                   ▼
                LISTEN                ← voice/
                   │
                   ▼
              UNDERSTAND              ← ai/ (BrainManager) + core/planning/intent_classifier
                   │
        ┌──────────┴──────────┐
        ▼                     ▼
     MEMORY                PERCEPTION
   (memory/)               (vision/)
        │                     │
        └──────────┬──────────┘
                   ▼
                REASON                  ← core/agent/agent_controller
                   │
                   ▼
                  PLAN                  ← core/agent/workflow_planner + core/planning/task_planner
                   │
                   ▼
                   ACT                  ← core/agent/goal_executor + skills/ + system/automation + system/input
                   │
                   ▼
               OBSERVE                 ← core/agent/observation_loop + vision/screen_observer
                   │
                   ▼
             VERIFY / ADAPT            ← core/agent/step_verifier + goal_verifier + recovery_engine + retry_manager
                   │
                   ▼
                  SPEAK                 ← voice/tts_engine
                   │
                   ▼
                  USER
```

This diagram is the **target loop**. Any subsystem addition or modification must fit into one of these boxes.

---

## 3. Architectural invariants (the "rules")

### R-1. Single boot path

**Rule:** There is exactly one boot path: `main.py` → `OmnixEngine(...)` → walk subsystems → `engine.start()`. No subsystem may be constructed or started by another subsystem; all wiring flows through the engine.

**Why:** Prevents the "two boot sequences" failure mode. Guarantees that `engine.start()` can shut down everything.

**Enforcement:** Linter rule (Phase 0.5) — `from core.omnix_engine import OmnixEngine` is the only authorized orchestrator import; subsystem classes are not allowed to import the engine.

### R-2. Service wrapper contract

**Rule:** Every subsystem that returns a "result of an operation" must go through a `core/services/*Service.py` wrapper. The wrapper exposes methods that return a `*Result` dataclass with at least:

```python
@dataclass
class XResult:
    success: bool
    value: Any = None
    provider: Optional[str] = None
    operation: Optional[str] = None
    error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
```

**Why:** Callers depend on this shape. Any new return path that bypasses the wrapper breaks every downstream consumer.

**Enforcement:** Type-check + unit test in Phase 1.

### R-3. Result normalization

**Rule:** All skill/executor return values are normalized through `core.execution.execution_status.normalize_result` into an `ExecutionStatus` enum value. No inline mapping of skill return shapes to status anywhere else in the codebase.

**Why:** Centralizes the contract. The agent loop and verifiers both consume `ExecutionStatus`; if normalization diverges, the loop breaks.

**Enforcement:** Static grep in Phase 0.5: `grep -r "ExecutionStatus" core/` must show **one** producer.

### R-4. Async adapter pattern

**Rule:** Synchronous desktop APIs (pyautogui, pyperclip, win32api, subprocess) remain **sync**. The async path is a thin facade (`system/input/async_adapter.py`, `system/browser/browser_controller.py`). The facade probes for subcomponents, then for top-level convenience methods, then raises.

**Why:** Sync desktop APIs are reliable on Windows; running them under `asyncio.run` is brittle. The facade is the bridge.

**Enforcement:** Linter rule — `async def` functions in `system/` are allowed **only** in `*_async.py` and `*_adapter.py` files.

### R-5. StepVerifier + GoalVerifier are mandatory

**Rule:** Every step execution is followed by `StepVerifier.verify(step, observation, claim)`. Every goal completion is followed by `GoalVerifier.verify(goal, observations, claim)`. The agent loop will not mark a step "passed" or a goal "achieved" without a verifier verdict.

**Why:** The user explicitly said in `My Goal for Omnix.md` §7: "I do not want Omnix to fake success." The verifiers are the only honest-answer mechanism.

**Enforcement:** The agent controller must call the verifier — `test_verification_recovery.py` is the regression test.

### R-6. Frozen pattern by absence

**Rule:** V6 has no `frozen/` directory. If a module is deprecated, **delete it** and record the rationale in `V5_V6_MIGRATION_AUDIT.md` addenda. Do not move-aside; do not rename.

**Why:** V5's `frozen/` was a soft-enforcement pattern (a directory name with comments). V6 makes it a hard rule.

**Enforcement:** `find . -type d -name frozen` returns nothing in V6.

### R-7. Env-var gates are sacred

**Rule:** `OMNIX_HEADLESS=1` and `OMNIX_QUIET_BOOT=1` are the only env vars that affect boot behavior. `OMNIX_HEADLESS=1` suppresses real microphone/camera/screen access (used by tests). `OMNIX_QUIET_BOOT=1` suppresses the branded console bookends.

**Why:** Tests need to boot the engine without triggering real hardware. Adding new env vars for the same purpose creates confusion.

**Enforcement:** Code review; docstring on every `os.environ.get("OMNIX_*")` call.

### R-8. No silent fallback for action success

**Rule:** If an automation action cannot be verified, the agent loop must return `success=False, status=ExecutionStatus.UNCERTAIN` — not `success=True`. The user has explicitly asked for honest reporting.

**Why:** `My Goal for Omnix.md` §7: "ACTION ATTEMPTED ≠ ACTION COMPLETED ≠ USER'S GOAL ACHIEVED."

**Enforcement:** `Verification.verify` must return a tri-state (passed/failed/uncertain), not a bool. The verifier's verdict overrides the executor's claim.

**Exception:** Inference degradation (CUDA unavailable → CPU) is allowed **with explicit loguru warning**. This is the only allowed silent fallback.

### R-9. Subsystem lifecycle is uniform

**Rule:** Every subsystem has:

- `initialize() -> bool` (idempotent)
- `shutdown() -> None` (idempotent, safe to call twice)
- `initialized: bool` property
- `statistics() -> Dict[str, Any]` (counters, errors, last event)
- `__repr__` (debuggable string)

**Why:** The engine walks subsystems uniformly. Variations break the walk.

**Enforcement:** Phase 0.5 unit test on every subsystem class.

### R-10. Thread safety: `RLock` or ownership

**Rule:** Subsystems that are accessed from the event loop **and** worker threads must use `threading.RLock`. Subsystems accessed from a single owner thread need not lock, but must document the owner.

**Why:** V5's locking is inconsistent (R5). V6 standardizes.

**Enforcement:** Code review; `threading.Lock` without a clear reason is a refactor request.

### R-11. Event bus is the integration point

**Rule:** Cross-subsystem communication goes through `core/events/event_bus.py`. Direct method calls between subsystems (e.g. `vision_manager` calling `memory_service`) are forbidden except in the `OmnixEngine` itself.

**Why:** Direct calls create hidden dependencies that the engine cannot shut down. The event bus is the only integration seam.

**Enforcement:** Phase 0.5 linter: imports of `core.services.*` outside `core/` are flagged.

**Exception:** Within a single subsystem (e.g. `vision_manager` calling `screen_observer`) direct calls are fine.

### R-12. AI Brain is replaceable

**Rule:** The `ai/brain_manager.py` is the only authorized entry point for LLM calls. No subsystem may call `openai`, `httpx`, or any other LLM library directly.

**Why:** Brain swap, prompt caching, rate limiting, model selection — all live in the Brain. Bypassing it breaks every one of those features.

**Enforcement:** `grep -r "import openai" core/ skills/ vision/ voice/ system/` returns no matches outside `ai/`.

### R-13. Memory is a service, not a singleton

**Rule:** Memory is accessed through `core/services/memory_service.py`. Direct access to `memory_coordinator`, `memory_manager`, `behavior_memory`, `ui_pattern_memory` outside the service is forbidden.

**Why:** The service layer is the seam for future memory backends, retention policies, encryption.

**Enforcement:** Same grep pattern as R-12.

### R-14. Vision is a service, not a singleton

**Rule:** Vision is accessed through `core/services/vision_service.py`. Direct access to `vision_manager`, `vision_pipeline`, `screen_observer` outside the service is forbidden.

**Why:** Same as R-13.

**Exception:** `skills/built_in/vision/*` is allowed to call `vision_service` because skills are *clients* of the service. They are not subsystems.

### R-15. Skills are declarative

**Rule:** A skill is a `Skill` dataclass with `name`, `description`, `parameters_schema`, `required_capabilities`, `execute(ctx, **params) -> SkillResult`. Skill bodies should contain no `import openai`, no direct hardware access, no direct file I/O outside declared capabilities.

**Why:** Skills are the unit of audit. Declarative skills are auditable; imperative ones are not.

**Enforcement:** Phase 0.5 lint rule: skill body must be ≤ 80 lines; otherwise flagged for review.

### R-16. Configuration is JSON, secrets are not

**Rule:** `config/*.json` holds non-secret configuration. `.env` (or keyring, in Phase 0.5+) holds secrets. JSON files **never** contain API keys.

**Why:** JSON is often committed; `.env` should not be.

**Enforcement:** Pre-commit hook (Phase 0.5) scanning `config/*.json` for patterns matching `sk-*`, `gsk_*`, etc.

### R-17. Logging uses loguru only

**Rule:** All logging goes through `from loguru import logger`. No `import logging`. No `print` for status. (Branded `console_loading/console_success/console_error` helpers are the only allowed console output during boot.)

**Why:** V5 has a mix of `logging` and `loguru` (per the audit's cross-cutting observations). V6 standardizes.

**Enforcement:** Linter; `grep -r "^import logging" core/ skills/ system/ vision/ voice/ memory/ ai/ automation/ context/ utils/` returns nothing.

**Exception:** Third-party libraries (`pyautogui`, `transformers`, etc.) are allowed to use `logging`. The library's noise is silenced via env vars (see R-18).

### R-18. Library noise is silenced at the top of `main.py`

**Rule:** `main.py` sets:

```python
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
```

before any heavy import.

**Why:** Reproduces V5's clean boot.

**Enforcement:** Code review; no other file may set these env vars.

### R-19. Tests are pytest, not scripts

**Rule:** All V6 tests are pytest-discoverable: `def test_*()` functions, optional classes for grouping, `if __name__ == "__main__":` only for manual entry points (e.g. real-Windows tests).

**Why:** V5's mix (R6) made CI impossible. V6 standardizes.

**Enforcement:** Phase 0.5 lint rule: files under `tests/` that are not pytest-discoverable must include a `# pragma: no cover` comment + a justification.

### R-20. The user's "My Goal for Omnix.md" is the north star

**Rule:** Every architectural change must move toward one or more of the 11 goals in `My Goal for Omnix.md`. No feature may be added for "completeness" or "marketing."

**Why:** The user explicitly wrote that they do not want a project with many features but no clear connection between them.

**Enforcement:** Phase 0.5+ code review checklist includes "which § in `My Goal for Omnix.md` does this serve?"

---

### R-21. The capability set is closed; the Brain cannot invent operations

**Rule:** The set of operations the agent can perform is the set of capabilities registered with the `CapabilityRouter`. The Brain may reason, plan, and choose, but it **must not** emit any operation outside that set. The `CapabilityRouter` validates every call:

1. The capability exists in the registry.
2. The parameter schema matches the capability's declared signature.
3. The capability is currently available (dependencies loaded, not disabled).
4. The safety policy permits it in this context.
5. The execution context is valid (it fits the current `TaskState` and `WorldState`).

A failed check produces a structured error; the plan is replanned, the user is asked, or the task is marked failed. The rejection is logged.

**Why:** A general-purpose agent is not a free-form code-executing agent. The user trusts Omnix with their computer; that trust depends on the agent being **confined** to a known, validated set of operations. A LLM that can execute arbitrary code is a different product, with a different threat model.

**Enforcement:** CI rejects capability names not in the registry; the `CapabilityRouter` raises on validation failure; the audit log records every rejected call.

**Source:** V6_ARCHITECTURAL_DECISIONS.md AD-21.

---

### R-22. The PerceptionRouter is adaptive, not universally ordered

**Rule:** There is **no universal ordering** of perception strategies. `DOM > UIA > OCR > Vision > Coordinates` is one example for one class of applications; it is not a fixed rule. The `PerceptionRouter` selects per:

```
application
target type
available APIs
historical reliability
confidence
current world state
task requirements
latency / cost
```

The router records the choice and the reason, so a failure in the chosen strategy can fall back intelligently. Every strategy is a plug-in implementing a `PerceptionStrategy` protocol.

**Why:** A universally ordered hierarchy is brittle. It makes wrong choices for unfamiliar applications, costs more than it needs to, and obscures the reason for a strategy failure. The router that picks contextually is more reliable, more debuggable, and more honest about why it acted.

**Enforcement:** CI static check that the router does not contain a hard-coded strategy chain; the router's choice and reason are emitted to the audit log on every selection.

**Source:** V6_ARCHITECTURAL_DECISIONS.md AD-13 (amended).

---

### R-23. Context is owned, not dumped

**Rule:** `ContextService` is a **thin coordinator** over five typed state containers, each with clear ownership:

- `ConversationContext` — what was said (turns, prior commitments)
- `TaskState` — the work in progress (current task, current goal, current subgoal, current plan, current step, remaining steps, completed steps, failed steps, replan count, task status, cancellation state)
- `WorldState` — the computer right now (foreground app, foreground window, open apps, open windows, current webpage, current URL, visible UI elements, screen state, last observation, last action, last action result, known entities, selected entity)
- `EntityContext` — what things have been mentioned and resolved
- `UserContext` — user identity, preferences, authorization

Each container owns its data and exposes read/write methods with validation. The `ContextService` does not become an unstructured dumping ground. The Brain and capabilities consume the `ContextService`; nothing bypasses it.

**Why:** A context service that owns everything becomes the new monolith. Splitting the data into typed containers gives each kind of state a clear owner and a clear contract.

**Enforcement:** Static check that no module outside the five containers reads or writes the corresponding state directly. CI test that mutating `TaskState` goes through the container's write methods, not through a global.

**Source:** V6_ARCHITECTURAL_DECISIONS.md AD-10 (amended).

---

### R-24. Natural language is the user-facing interface; structured calls are the internal interface

**Rule:** Two distinct layers, with no leakage between them.

**User-facing interface — natural language only.** The user never sees a "command template," a "slash command," or a skill name. The agent accepts free-form utterances from voice or text. The user never types `open_chrome` and never sees `play_believer` as a callable thing.

**Internal interface — structured capability calls are required.** Inside the engine, the natural-language input is interpreted, not echoed. The pipeline is:

```
User language
  → Semantic understanding (Brain / NLU)
  → Goal
  → Structured plan
  → Capability calls
```

The capability calls are **internal implementation objects** — typed dataclasses with validated parameters, validated by the `CapabilityRouter`, never serialized to the user, never documented as a user-facing API. The skill set is the type system the Brain emits into; it is not a CLI the user invokes.

**Why:** Confusing the two layers produces bad architecture in both directions. A "natural-language-only" rule that lets the LLM emit free-form text creates a free-form code-executing agent. A "command template" rule turns Omnix into a CLI the user has to learn. The seam is the structured capability call.

**Enforcement:** No documentation lists skill names as "things the user can say." No CLI surface exposes skill names. The `CapabilityRouter` is the only entry point for capability calls.

**Source:** V6_ARCHITECTURAL_DECISIONS.md AD-3 (amended).

---

## 4. Anti-patterns (explicitly forbidden)

| Anti-pattern | Why forbidden |
|---|---|
| Calling `os.system(...)` from a skill | Bypasses capability check. Violates R-21. |
| Importing `core.omnix_engine` from a skill | Creates a cycle. |
| Catching `Exception` and returning `success=True` | Hides failure. |
| Writing API keys in `config/*.json` | Leaks into version control. |
| Adding a new `os.environ.get("OMNIX_*")` for boot behavior | Violates R-7. |
| Mutating global state in a `__init__` | Breaks re-import. |
| Putting business logic in `__init__.py` | Hides it from linters. |
| Calling `print(...)` outside `main.py` boot helpers | Violates R-17. |
| Catching `KeyboardInterrupt` outside `main.py` | Breaks shutdown. |
| `asyncio.run` inside a skill | Sync desktop APIs are sync for a reason. |
| **A hard-coded `try DOM then UIA then OCR then vision` chain in the perception router** | Violates R-22. |
| **A capability name exposed to the user (CLI, doc, slash command)** | Violates R-24. |
| **`ContextService` carrying every kind of state in one mega-dataclass** | Violates R-23. |
| **A capability that wraps `eval`/`exec`/`subprocess` without an explicit registration, parameter schema, safety policy, and full audit log** | Violates R-21. |

---

## 5. What is intentionally NOT in V6

- **PyQt6 UI in production path** — V5 had UI in `frozen/`, which we are not migrating. PyQt6 stays in `requirements.txt` only for debug tooling (Phase 4).
- **System tray icon** — V5 had `frozen/ui/tray_icon.py`. Not migrating. Add only if a Phase-N user-facing feature requires it.
- **Notification popups** — Same.
- **Developer panel** — Same.
- **Settings window** — Same.
- **Character UI** — Same.
- **Skill generator** — V5 had `frozen/skills/generator/`. Not migrating. Skills are hand-authored and reviewed.
- **Generated skills** — V5 had `frozen/skills/generated/`. Not migrating.
- **Environment state** — V5 had `frozen/state/environment_state.py`. Not migrating.
- **System events** — V5 had `frozen/system_events/`. Not migrating.
- **System manager (old)** — V5 had `frozen/system_manager.py`. Not migrating. `core/omnix_engine.py` is the system manager.

If any of these become desirable, they must be re-designed from scratch and added with a recorded rationale in `V5_V6_MIGRATION_AUDIT.md` addenda.

---

## 6. What is in V6 that V5 did not have (additions, not migrations)

These are forward-looking placeholders in the V6 skeleton. They are not implemented in V5; they are future work for Phase N.

- `core/compatibility/` — shims for old skill APIs. Placeholder.
- `core/state/` — finer-grained state machines. V5 had this too, but V6 expands it.
- `core/utils/` — engine-internal utilities (timers, profiler, metrics). V5 had minimal versions.
- `core/events/` — more event types (Phase 5 expanded it).
- `core/execution/` — explicit execution-status pipeline (Phase 6 added this).

---

## 7. Phase 0 sign-off

- [x] No code in V6.
- [x] No code in V5 modified.
- [x] Architecture rules are on disk for Phase 0.5+ to enforce.

**PHASE 0 COMPLETE — NO SOURCE CODE MODIFIED. WAITING FOR APPROVAL TO BEGIN PHASE 0.5.**
