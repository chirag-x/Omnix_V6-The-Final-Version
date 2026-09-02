# V6 Architectural Decisions — To Lock Before Implementation

*(Initial manifest: 20 decisions. Current revision: 21 decisions, with AD-3, AD-10, AD-13 amended and AD-21 added.)*

**Phase:** Architecture-Alignment Audit (post-Phase 0)
**Date:** 2026-08-29
**Status:** Decisions to lock before Phase 0.5 begins
**Audience:** Every contributor to Omnix V6

This document is the **decision manifest**. Every entry below is a binding choice that, once made, must be honored by every subsequent phase. The product vision, gap analysis, and revised roadmap are derived from these decisions.

**Rule:** No implementation code is written until each AD is either **LOCKED** (with a short rationale) or explicitly **DEFERRED** to a later phase. Items marked **OPEN** must be resolved by the user before Phase 0.5 begins.

---

## The 20 decisions

### AD-1. The engine is a thin orchestrator, not a monolith

**Decision:** `core/omnix_engine.py` (or its V6 equivalent) is a **wiring layer** that constructs services and routes events. It contains no business logic. Maximum 600 lines. Anything longer is a code smell.

**Rationale:** V5's 3,637-line `omnix_engine.py` is the single largest architectural risk in the migration. Reproducing it in V6 violates the product vision §22 and the gap analysis row #24.

**Locked.** (V6_ARCHITECTURE_RULES.md R-1.)

---

### AD-2. Conversation and action are distinguished per turn, not per session

**Decision:** A semantic dispatch layer (the Brain, not a regex) classifies **every** user turn as `conversation`, `action`, `memory_update`, or `preference_update`. The conversation path **never** enters the agent loop. The action path never returns text without verification.

**Rationale:** Product vision §4, §5, §6, §25. The product vision distinguishes the two modes fundamentally. The V5 `IntentClassifier` is regex-first and is the largest single weakness of the existing implementation.

**Locked.** (V6_ARCHITECTURE_RULES.md R-22; gap analysis P0 #1.)

---

### AD-3. Natural language is the only user-facing interface; internal interface is structured capability calls

**Decision:** This decision has two layers, and confusing them produces bad architecture in both directions.

**User-facing interface — natural language only.**

The user must never need to know internal command names. They speak or type naturally:

> "Open Chrome and search for AI agents."

> "Can you find my Python assignment PDF and put it on the Desktop?"

The agent accepts free-form utterances from voice or text. There are no "command templates," no "slash commands," no skill names exposed to the user. The user never types `open_chrome` and never sees `play_believer` as a callable thing.

**Internal interface — structured capability calls are REQUIRED.**

Inside the engine, the natural-language input is interpreted, not echoed. The pipeline is:

```text
User language
    ↓
Semantic understanding (Brain / NLU)
    ↓
Goal
    ↓
Structured plan
    ↓
Capability calls
```

For example:

```text
User:  "Open Chrome and search for AI agents."

Goal:  Search for "AI agents" in Chrome

Plan:
  1. open_application(app="Chrome")
  2. focus_window(window_match="Chrome")
  3. search_browser(query="AI agents")
```

These structured calls are **internal implementation objects** — dataclasses with typed parameters, validated by the `CapabilityRouter`, never serialized to the user, never documented as a user-facing API. The skill set is not a CLI; it is the type system the Brain emits into and the router validates against.

**What this forbids:**

- The user typing or seeing `open_chrome` as a command.
- Documentation that lists skill names as "things the user can say."
- Slash-command interfaces (`/open`, `/search`).
- The LLM emitting free-form text that the system then re-parses as a command.
- Hard-coded phrase → skill tables exposed to the user.

**What this does NOT forbid:**

- The Brain emitting `open_application(app="Chrome")` internally.
- The `CapabilityRouter` accepting `(capability_name, parameters)` from the Brain.
- A developer-facing introspection view of available capabilities (debug panel only).
- The user saying "open Chrome" and the Brain mapping that to `open_application(app="Chrome")` — that is correct.

**Rationale:** Product vision §5, §17, §25. V5's skill set reads as `open_chrome`, `play_believer` — that is a hard-coded command list, not a general agent. But the alternative is not "let the LLM do everything"; the alternative is "let the LLM do **understanding and planning**, and let the deterministic system do **execution**." The seam between the two is the structured capability call.

**Locked.** (V6_ARCHITECTURE_RULES.md R-2; gap analysis P0 #3, #4; AD-9; AD-11.)

---

### AD-4. The agent loop is closed: observe → plan → act → observe → verify → recover

**Decision:** No execution path may "plan all then execute blind." Every step has: a pre-state observation, an action, a post-state observation, a per-step verification, a goal verification. Recovery is a first-class stage, not an exception path.

**Rationale:** Product vision §9, §10, §11, §25. This is the non-negotiable property. V5's `WorkflowPlanner` + `GoalExecutor` + `StepVerifier` + `RecoveryEngine` already implement this loop; V6 must not weaken it.

**Locked.** (V6_ARCHITECTURE_RULES.md R-5, R-6, R-7; gap analysis P0 #14.)

---

### AD-5. Action success is not goal success; verification is mandatory

**Decision:** `StepVerifier` and `GoalVerifier` are the only authorities on success. An executor returning `success=True` is **evidence, not a verdict**. The verifier may override it. The system never reports "Task completed" based on executor output alone.

**Rationale:** Product vision §10, §25. The V5 verifier pattern is preserved; the enforcement must be hard, not optional.

**Locked.** (V6_ARCHITECTURE_RULES.md R-6, R-7; gap analysis P0 #14.)

---

### AD-6. Safety policy is below the agent, not above it

**Decision:** A safety DSL is the source of truth for what actions are allowed, what requires confirmation, and what is blocked. The LLM cannot bypass it. Every action produces an audit-log entry. Direct LLM instructions to skip safety are rejected and logged.

**Rationale:** Product vision §20, §25. The LLM is a user of the safety layer, not its author. The user controls the DSL, not the model.

**Locked.** (V6_ARCHITECTURE_RULES.md R-8; gap analysis P0 #21.)

---

### AD-7. The Brain is the only LLM caller; it is replaceable

**Decision:** No code outside `ai/` may import `openai`, `anthropic`, or any provider SDK. The Brain exposes a single `ask(prompt, role, context) -> AIResult` interface. Provider switching is a configuration concern, not a code change.

**Rationale:** Product vision §16, §25. The Brain seam is the most important abstraction in V6. A Brain that hard-codes OpenRouter is not a Brain, it's a wrapper.

**Locked.** (V6_ARCHITECTURE_RULES.md R-9; gap analysis P0 #19.)

---

### AD-8. V5 is reference, not specification; migrate capabilities, not files

**Decision:** No V5 file is migrated because it exists in V5. Every V6 file exists because a capability requires it. The V5 file map is a **reference** for finding implementations of capabilities, not a list of files to copy.

**Rationale:** Product vision §21, §27. V5 has weak verification, a regex-first classifier, a 3,637-line engine, and obsolete `frozen/` modules. Copying those files reproduces the flaws.

**Locked.** (V6_ARCHITECTURE_RULES.md R-3; gap analysis P0 #28.)

---

### AD-9. Capabilities are small, composable, and deterministic

**Decision:** A capability is a single, testable operation: `OpenApplication`, `ClickElement`, `TypeText`, `SearchBrowser`, `ReadScreen`, `FindFile`, `PlayMedia`, `FocusWindow`. No capability contains the intelligence to determine the entire task. The agent composes them.

**Rationale:** Product vision §17, §25. V5's `open_chrome_and_search` skill is the anti-pattern. The agent must be able to handle unseen task shapes by composition.

**Locked.** (V6_ARCHITECTURE_RULES.md R-10; gap analysis P1 #17.)

---

### AD-10. Context is essential, and is split into TaskState, WorldState, and a coordinating ContextService

**Decision:** `ContextService` is **not** an unstructured dumping ground. It is a thin coordinator over five well-typed state containers, each with clear ownership:

```text
ContextService  (coordinator / facade)
    ├── ConversationContext   — what was said (turns, prior commitments)
    ├── TaskState             — what is being done (goal, plan, step, status)
    ├── WorldState            — what is true right now (apps, windows, screen, entities)
    ├── EntityContext         — what things have been mentioned and resolved
    └── UserContext           — user identity, preferences, authorization
```

**TaskState owns the agent's understanding of the work in progress.** It must be capable of representing at minimum:

```text
current task          — the top-level task the user requested
current goal          — the active sub-goal being executed
current subgoal       — finer-grained objective within the current plan
current plan          — the structured plan (list of capability calls)
current step          — index / pointer into the plan
remaining steps       — steps not yet executed
completed steps       — steps that have been verified
failed steps          — steps that failed (with reason and recovery attempts)
replan count          — how many times the plan has been replanned
task status           — pending | running | verifying | succeeded | failed | cancelled
cancellation state    — explicit cancel signal from the user
```

**WorldState owns the agent's understanding of the computer right now.** It must be capable of representing at minimum:

```text
foreground application
foreground window
open applications
open windows
current webpage
current URL
visible UI elements
screen state / summary
last observation
last action
last action result
known entities         — entities observed on the screen or in the world
selected entity        — the entity the user is currently referring to
```

**Responsibilities:**

- `ContextService` coordinates retrieval, persistence, and event emission. It does **not** own the data — it routes reads and writes to the right container.
- `TaskState`, `WorldState`, `ConversationContext`, `EntityContext`, `UserContext` each own their data and expose read/write methods with validation.
- The Brain and capabilities consume the `ContextService`; nothing bypasses it.
- "The first result" is resolved by `EntityContext` against the `WorldState.last_observation`. "That window" is resolved against `WorldState.foreground_window`. "The blue button" is resolved against `WorldState.visible_ui_elements`.

**What this forbids:**

- A single mega-dataclass that holds all state in one blob.
- Capabilities reading or writing state directly without going through the owning container.
- The Brain carrying task or world state in its prompt that is not also in the typed containers.
- "Context" used as a synonym for "any data the agent might need."

**Rationale:** Product vision §7, §25. "The first result", "that window", "the blue button" — none of these are resolvable without a context layer. V5 has `ConversationManager` (short-term messages only); V6 must have a real context service. But a context service that owns everything becomes the new monolith. The split into `TaskState` and `WorldState` (and the three sibling contexts) gives each kind of state a clear owner and a clear contract.

**Locked.** (V6_ARCHITECTURE_RULES.md R-11; gap analysis P0 #5, #25; new in this amendment.)

---

### AD-11. Plan synthesis for unseen tasks is Brain-driven, not rule-bound

**Decision:** When a user request does not match a known capability composition, the **Brain** produces a plan: a sequence of capability calls given the goal, context, and available capabilities. The rule-based planner is a fast-path optimization, not the contract.

**Rationale:** Product vision §8, §25. "Open Chrome, search for the best free Python courses, open the first result, find the decorators section" — no rule-based template produces this. The Brain must reason about decomposition.

**Locked.** (V6_ARCHITECTURE_RULES.md R-4; gap analysis P0 #8.)

---

### AD-12. Recovery is real, not a stub

**Decision:** Recovery strategies are explicit and tested: wait, re-observe, refocus, re-locate, alternative locator, keyboard navigation, browser automation instead of coordinates, replan, ask the user. The `RecoveryEngine` is exercised by tests, not a placeholder.

**Rationale:** Product vision §11, §25. V5's `RecoveryManager.recover` is a stub (V5 audit R2). V6 must not inherit that.

**Locked.** (V6_ARCHITECTURE_RULES.md R-12; gap analysis P1 #15.)

---

### AD-13. PerceptionRouter is adaptive, not universally ordered

**Decision:** There is **no universal ordering** of perception strategies. `DOM > UIA > OCR > Vision > Coordinates` is one example for one class of applications; it is not a fixed rule. The same applies to any other ordering.

The architectural rule is:

> The `PerceptionRouter` selects the **most reliable available** perception strategy for the **current** application, target, action, context, and evidence.

The router is a function of:

```text
application          — Chrome, Notepad, Spotify, custom app, …
target type          — web element, native control, text, image, coordinate
available APIs       — which strategies are usable right now (Selenium, UIA, OCR runtime, vision model loaded)
historical reliability — past success rate of each strategy for this (app, target_type) pair
confidence           — current strategy's reported confidence, or absence of it
current world state  — WorldState.foreground_application, focused window, screen summary
task requirements    — what the step actually needs (a click target, a text read, a region)
latency / cost       — vision models are slow; DOM lookups are fast
```

The router may produce a plan such as:

```text
Browser app + clickable target  → DOM or accessibility tree
Native Windows app + control    → UIA first
Text-heavy UI (PDF, terminal)   → OCR
Unknown / custom / game UI      → Vision
Last resort                     → Coordinate (with explicit fallback reason)
```

…or a completely different plan for an unfamiliar application. The router **records the choice and the reason**, so that a failure in the chosen strategy can fall back intelligently.

**What this forbids:**

- A single hard-coded `if/else` chain that always tries DOM, then UIA, then OCR, then vision, then coordinates — regardless of app or context.
- Treating `DOM > UIA > OCR > Vision > Coordinates` as a product rule rather than an example.
- Locking the strategy in advance of observing the world state.

**What this permits:**

- Sensible defaults per application class (e.g. "for browser targets, prefer DOM when Selenium is connected").
- A strategy that fails and triggers the router to pick a different one, with the failure fed back as evidence.
- A vision model being the first choice for a custom Win32 game UI that exposes no accessibility tree.
- The router choosing OCR for a terminal where neither DOM nor UIA exists.

**Every strategy is a plug-in implementing a `PerceptionStrategy` protocol.** Adding a new strategy (e.g. a new accessibility backend, a different OCR engine) is a registration, not a router rewrite.

**Rationale:** Product vision §12, §18, §25. Vision is the eyes, not the only sense. A universally ordered hierarchy is brittle: it makes wrong choices for unfamiliar applications, costs more than it needs to, and obscures the reason for a strategy failure. The router that picks contextually is more reliable, more debuggable, and more honest about why it acted the way it did. V5 leans vision-heavy in some flows; V6 must be explicit and adaptive.

**Locked.** (V6_ARCHITECTURE_RULES.md R-13; gap analysis P1 #11, #27; amended in this revision to drop the universal ordering.)

---

### AD-14. Voice is first-class; the user does not switch modes for it

**Decision:** There is no "voice mode toggle." The voice loop is: wake word → STT → NLU → Brain → response (TTS). The voice and text paths converge at the Brain. STT confidence < 0.6 triggers a "Did you mean…?" confirmation; the agent never acts on a guess.

**Rationale:** Product vision §14, §25. V5 has a wake listener, recognizer, and TTS; V6 must not regress and must not require a "voice mode" toggle.

**Locked.** (V6_ARCHITECTURE_RULES.md R-14; gap analysis P0 #2.)

---

### AD-15. Memory has policy: retention, dedup, privacy, deletion, confidence

**Decision:** Every memory write is governed by a `MemoryPolicy` (retention window, deduplication rule, privacy flag, confidence score). The user can inspect, edit, and delete every memory. The agent can never store a fact the user has not authorized.

**Rationale:** Product vision §15, §25. V5's `MemoryCoordinator` works but has no policy enforcement. Uncontrolled memory growth is forbidden (product vision §15).

**Locked.** (V6_ARCHITECTURE_RULES.md R-15; gap analysis P1 #6.)

---

### AD-16. The dependency model is tiered and lazy

**Decision:** V6 has three dependency tiers: **boot** (Python stdlib + minimal third-party for `OMNIX_HEADLESS=1` boot), **agent** (everything the closed loop needs), and **capability** (vision, voice, browser, ML). Capabilities load their dependencies on first use, not at import. A missing optional dependency degrades the corresponding capability gracefully, not the whole engine.

**Rationale:** V6_DEPENDENCY_PLAN.md. This is the only way to keep boot under 5 seconds and to make V6 runnable on hosts without a GPU or microphone.

**Locked.** (V6_DEPENDENCY_PLAN.md §3; V6_MODEL_PLAN.md §4.1.)

---

### AD-17. Tests are acceptance tests, not unit tests of components

**Decision:** The contract for V6 is the acceptance test suite (`docs/V6_ACCEPTANCE_TESTS.md`). Each test describes a real user request, expected behavior, expected verification, failure behavior, and success criteria. Component unit tests exist to support the acceptance tests; they do not replace them. CI gates on three markers: `real_windows`, `voice`, `vision`.

**Rationale:** Product vision §25, §26. Unit tests passing does not mean the product works. The acceptance tests are the definition of done.

**Locked.** (V6_TEST_MIGRATION_PLAN.md §1; V6_ACCEPTANCE_TESTS.md.)

---

### AD-18. The architecture enables future capabilities; nothing is impossible

**Decision:** Every subsystem exposes a protocol, not a concrete class: `BrainProvider`, `PerceptionStrategy`, `LocatorStrategy`, `SafetyPolicy`, `MemoryBackend`, `Capability`. Adding a new strategy, provider, or backend is a registration, not a rewrite.

**Rationale:** Product vision §19, §25. Multi-modal, local LLM, document understanding — none of these are "in V6", but none of them are "blocked by V6" either.

**Locked.** (V6_ARCHITECTURE_RULES.md R-16; gap analysis P2 #22.)

---

### AD-19. Every phase moves toward the integrated loop; no isolated features

**Decision:** No phase ships a feature that cannot be reached from the natural-language → understand → plan → act → observe → verify → recover → respond loop. A phase that produces "voice works but cannot trigger actions" is not a phase — it's a demo.

**Rationale:** Product vision §24, §25. The Phase 0 file-centric roadmap had this risk; the revised roadmap (V6_REVISED_ROADMAP.md) is structured to avoid it.

**Locked.** (V6_REVISED_ROADMAP.md Phase 1 entry.)

---

### AD-20. Anti-patterns are enforced, not just documented

**Decision:** CI rejects: `import logging` in production code (use a structured logger); `import openai` outside `ai/`; any `frozen/` directory; any engine file > 1,000 lines; any module named with the V5 service-suffix pattern (`*Service.py`) **without** a corresponding `*Result` dataclass; any verifier returning `success=True` without evidence.

**Rationale:** Product vision §25, §26. Documentation without enforcement is not architecture. The V5 audit identified 12 latent risks; V6 must close the door on them in CI, not in code review.

**Locked.** (V6_ARCHITECTURE_RULES.md R-17, R-18, R-19, R-20.)

---

### AD-21. The Brain may reason but can only invoke registered capabilities

**Decision:** The Brain may interpret, plan, and choose — but it must **never** invent arbitrary executable operations. The set of operations the agent can perform is closed: it is the set of capabilities registered with the `CapabilityRouter`. The Brain composes plans from that set; it cannot escape it.

Conceptually:

```text
Available capabilities  (registered with CapabilityRouter)
        ↓
Brain                  (reasons, plans, selects)
        ↓
Structured plan        (list of capability calls)
        ↓
CapabilityRouter       (validates, routes)
        ↓
Registered capability  (executes)
        ↓
Execution result
```

**Example of a permitted plan:**

```text
AVAILABLE:
  open_application
  focus_window
  search_browser
  click_element
  type_text
  scroll
  read_screen
  find_file
  open_file
  play_media

PLAN (Brain-emitted):
  1. open_application(app="Chrome")
  2. focus_window(window_match="Chrome")
  3. search_browser(query="AI agents")
  4. read_screen()                    ← verify results loaded
```

**Example of what the Brain MUST NOT emit:**

```text
execute_python_code(...)
run_arbitrary_command(...)
invented_capability(...)
shell_exec(...)
os.system(...)
eval(...)
```

unless that capability is **explicitly registered** with the `CapabilityRouter` and **explicitly allowed by the safety policy**. The default position is: **the capability does not exist.**

**CapabilityRouter validation — required on every call:**

The router MUST validate, before any execution, that:

1. **Capability exists** — the name is in the registry.
2. **Parameters are valid** — the parameter schema matches the capability's declared signature.
3. **Capability is currently available** — its dependencies are loaded, and no failure has disabled it.
4. **Safety policy allows it** — the policy permits this capability in this context.
5. **Execution context is valid** — the call fits the current `TaskState` and `WorldState` (e.g. you cannot `focus_window` on a non-existent window without first opening the app).

If any check fails, the call is rejected. The plan is either replanned, the user is asked, or the task is marked failed. The rejection is logged.

**What this forbids:**

- The Brain emitting any string that the system then evaluates.
- A "code execution" capability that the LLM can drive without explicit registration.
- New capabilities being added at runtime by the Brain itself.
- A `general_purpose_tool` capability that wraps arbitrary code.

**What this permits (carefully):**

- A `run_powershell_script` capability that is registered, has a strict parameter schema, is gated by the safety policy, and is logged in full.
- A `browser_navigate` capability that internally uses Selenium — that is one registered capability, not many invented ones.
- The Brain asking the user "I don't have a capability to do X; can I register `do_x` with these parameters?" — but the registration is a user-driven config change, not an autonomous Brain act.

**Rationale:** Product vision §16, §17, §20, §25. A general-purpose agent is not the same as a free-form code-executing agent. The user trusts Omnix with their computer; that trust depends on the agent being **confined** to a known, validated set of operations. An LLM that can execute arbitrary code is a different product, with a different threat model and a different regulatory profile. The capability registry is the boundary.

**Locked.** (New in this amendment; complements AD-3, AD-6, AD-9, AD-11.)

---

## How to use this document

Before Phase 0.5 begins, the user reviews each AD and either:

- **Locks** it as written.
- **Amends** it (the rationale is updated; the decision may change).
- **Defers** it to a later phase (the AD is moved to a "deferred decisions" appendix).
- **Opens** it (the user marks the AD as needing a decision before Phase 0.5; the assistant surfaces the open question).

No code is written until every AD in this document has one of those four states.

---

## Locked vs. deferred vs. open

| AD | Title | State |
|---|---|---|
| AD-1 | Engine is a thin orchestrator | Locked |
| AD-2 | Conversation vs. action per turn | Locked |
| AD-3 | NL is the only user-facing interface; internal interface is structured capability calls | Locked (amended) |
| AD-4 | Closed agent loop | Locked |
| AD-5 | Verification is mandatory | Locked |
| AD-6 | Safety below the agent | Locked |
| AD-7 | Brain is the only LLM caller | Locked |
| AD-8 | Migrate capabilities, not files | Locked |
| AD-9 | Capabilities are small and composable | Locked |
| AD-10 | Context is split into TaskState, WorldState, ConversationContext, EntityContext, UserContext | Locked (amended) |
| AD-11 | Plan synthesis is Brain-driven | Locked |
| AD-12 | Recovery is real, not a stub | Locked |
| AD-13 | PerceptionRouter is adaptive, not universally ordered | Locked (amended) |
| AD-14 | Voice is first-class | Locked |
| AD-15 | Memory has policy | Locked |
| AD-16 | Dependencies are tiered and lazy | Locked |
| AD-17 | Tests are acceptance tests | Locked |
| AD-18 | Architecture enables future capabilities | Locked |
| AD-19 | Every phase is integrated | Locked |
| AD-20 | Anti-patterns are enforced in CI | Locked |
| AD-21 | Brain may reason but can only invoke registered capabilities | Locked (new) |

All 21 decisions are **locked** in this document. The user may amend any of them before Phase 0.5 begins.

---

**ARCHITECTURE DECISIONS LOCKED. NO SOURCE CODE MODIFIED. NO DEPENDENCIES INSTALLED. WAITING FOR USER APPROVAL BEFORE PHASE 0.5.**
