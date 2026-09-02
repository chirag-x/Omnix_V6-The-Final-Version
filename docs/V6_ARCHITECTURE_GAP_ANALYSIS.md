# V6 Architecture Gap Analysis

**Phase:** Architecture-Alignment Audit (post-Phase 0)
**Date:** 2026-08-29
**Compares against:** `docs/OMNIX_V6_PRODUCT_VISION.md` and the previous Phase 0 docs.

This document audits every major product requirement against (a) the V6 architecture as documented in the Phase 0 set, (b) V5's actual implementation, and (c) the gap between them.

| # | Product Requirement | Current V6 Support | V5 Support | Gap | Required Architecture Change | Priority |
|---|---|---|---|---|---|---|
| 1 | **Conversation mode (per turn)** | Mentioned in `V6_ARCHITECTURE_RULES.md` R-22 but no explicit dispatch design. V5 has `intent_classifier` that returns `conversation` intent. | Partial. `IntentClassifier` distinguishes `conversation` vs `action` via regex+AI fallback. But the dispatch is rule-heavy, not semantic. | High. The classifier is regex-first; natural-language variations will miss. | **Replace regex-first classification with a semantic dispatch layer**: Brain classifies turn intent (`conversation` / `action` / `memory_update` / `preference_update`) before any rule runs. Conversation path does **not** touch automation. | **P0** |
| 2 | **Voice** | `voice/` exists in V6 skeleton. Plan in `V6_MODEL_PLAN.md` defers to Phase 2. | Implemented in V5 (WakeListener, SpeechRecognizer, TTS). V5 has the loop, but the conversation path bypasses the agent loop in some flows. | Medium. V5 voice works; V6 must not regress. | **Add voice→understanding→response loop** as the primary surface. Wake → STT → NLU → Brain → response → TTS. No "voice mode" toggle. | **P0** |
| 3 | **Natural-language understanding** | `core/planning/intent_classifier.py` planned; no semantic NLU designed. | Regex + `BrainManager.ask` fallback. Works for ~70% of cases; brittle on rephrasings. | High. The user explicitly said "do NOT design V6 around exact command templates." | **Add a semantic NLU layer** (Brain-driven) that resolves intent, entities, referents, and slots. Regex rules remain only as a fast-path optimization, not as the contract. | **P0** |
| 4 | **Intent** | `core/planning/intent_classifier.py` planned. | `IntentClassifier` with `INTENT_PATTERNS` (regex). | High (same as #3). | Same as #3. | **P0** |
| 5 | **Context (conversation, task, app, screen, action, entity)** | `core/state/conversation_manager.py` planned; no entity-resolution layer designed. | V5 has `ConversationManager` (short-term messages only). No entity resolution, no referent tracking, no app/screen/action context integrated. | **Critical**. V5 does not track "the first result" or "that window" across turns. | **Add a `ContextService` that coordinates five typed state containers — `ConversationContext`, `TaskState`, `WorldState`, `EntityContext`, `UserContext` — with clear ownership**. `TaskState` owns the work (goal, plan, step, status, replan count, cancellation). `WorldState` owns the world (foreground app, window, open apps, current URL, visible elements, last observation, last action, known entities, selected entity). The Brain and capabilities consume the `ContextService`; nothing bypasses it. | **P0** |
| 6 | **Memory (with policy)** | `core/services/memory_service.py` planned. V5 design uses `MemoryCoordinator` over 3 backends. | V5 has `MemoryCoordinator` (1068 L) with semantic + behavior + system. No retention/dedup/privacy policy enforcement. | Medium-High. Functional but ungoverned. | **Add a `MemoryPolicy`** that enforces retention, dedup, privacy, deletion. User can inspect and edit memory. | **P1** |
| 7 | **Planning (multi-step, dynamic)** | `core/agent/workflow_planner.py` planned. V5 has 9 agent files totaling ~9,800 L. | V5 has `WorkflowPlanner`, `GoalExecutor`, `StepVerifier`, `GoalVerifier`, `RecoveryEngine`, `RetryManager`, `WaitEngine`, `ObservationLoop`, `AgentController`. | Low. V5's planning is solid; the gap is in NLU and capability composition. | **Refactor for clarity, not scope.** Keep V5's planner/executor/verifier/recovery; split into smaller classes with clear seams. Do not rewrite the loop. | **P1** |
| 8 | **Dynamic task execution (unseen tasks)** | Not explicitly designed. | V5's regex classifier cannot construct plans for unseen phrasings; relies on the Brain to extract steps. | High. The user's "open Chrome, search for the best free Python courses, open the first result, find the decorators section" example requires the Brain to **synthesize** a plan. | **Add a plan-synthesis step**: when the rule-based path returns no canonical plan, the Brain takes the goal + context + available capabilities and produces a plan (steps as capability calls). | **P0** |
| 9 | **Windows automation (real)** | `system/` skeleton. V5 has 100+ files. | V5 has real automation: `system/automation/*`, `system/input/*`, `system/windows/*`, `system/applications/*`. | Low. Functional. | **Preserve** V5's system/ layer; fix the latent `WorkflowExecutor` queue bug (R1); design real recovery (R2); hook vision into verification (R3). | **P1** |
| 10 | **Browser automation (real, hybrid)** | `system/browser/` skeleton. V5 has `BrowserController` (async facade). | V5 has Selenium wired into `BrowserController` (async). Hybrid vision+DOM was partial. | Medium. V5 leans vision-heavy in some flows. | **Add an adaptive `PerceptionRouter`** that picks the most reliable available strategy per (app, target, action, context, evidence). Sensible defaults per app class; the router records the choice and reason; failures feed back as evidence. **No universal ordering is locked in.** | **P1** |
| 11 | **Vision (YOLO, OCR, UI detection)** | `vision/` skeleton. V5 has ~25 files + `yolo11n.pt`. | V5 has `VisionManager`, `VisionPipeline`, `ScreenObserver`, `TextDetector`, `UIDetector`, `ElementLocator`, `ScreenIntelligence`, `ScreenSummary`, plus `detection/`, `hierarchy/`, `summary/`, `discovery/`. | Low. Functional. | **Treat vision as one perception strategy among many.** Add an adaptive `PerceptionRouter` — selection is a function of (app, target, available APIs, historical reliability, confidence, world state, task requirements, latency/cost). The router records the choice and reason. | **P1** |
| 12 | **OCR** | Planned in `vision/text_detector.py`. | V5 has `easyocr` integration. | Low. | Preserve. | **P2** |
| 13 | **Observation (per step)** | `core/agent/observation_loop.py` planned. | V5 has `ObservationLoop` (per-step observation). | Low. | Preserve. | **P1** |
| 14 | **Verification (step + goal)** | `core/agent/step_verifier.py` + `goal_verifier.py` planned. | V5 has both. V5 tests cover override semantics. | Low. | **Hard-enforce** R-5: every step runs through `StepVerifier`; every goal through `GoalVerifier`. | **P0** |
| 15 | **Recovery (per step)** | `core/agent/recovery_engine.py` + `retry_manager.py` + `wait_engine.py` planned. | V5 has all three, but `RecoveryManager.recover` is a stub (R2) and `Verification.verify` is naive (R3). | Medium. Functional but limited. | **Design real recovery**: retry with backoff, alternative strategy, replan, ask-user. Hook vision into verification. | **P1** |
| 16 | **Replanning (multi-step)** | `core/agent/agent_controller.py` has `max_replan_attempts`. | V5 has replan budget in `AgentController`. Tests cover termination. | Low. | Preserve. | **P1** |
| 17 | **Skills / capabilities** | `skills/built_in/*` skeleton. V5 has 35+ skill files. | V5 has functional skills. Some are too large ("open Chrome and search"). | Medium. | **Refactor to small composable capabilities** per product vision §17. Do not preserve "one skill = one command" if it conflicts. | **P1** |
| 18 | **Capability routing** | `core/capability_router.py` planned. | V5 has implicit routing in `IntentClassifier` + `WorkflowPlanner`. | Medium. Routing is ad-hoc. | **Add a `CapabilityRouter`** that maps (capability name, parameters) → concrete skill/system call. Centralized. Testable. | **P1** |
| 19 | **AI Brain (replaceable)** | `ai/brain_manager.py` skeleton. V5 has 232 L. | V5's `BrainManager` works but provider selection is minimal. | Medium. | **Add explicit provider selection, rate limiting, retry, prompt caching.** Brain remains the **only** entry point for LLM calls. | **P0** |
| 20 | **Voice response (TTS)** | `voice/tts_engine.py` planned. | V5 has `edge-tts`. | Low. | Preserve. | **P1** |
| 21 | **Safety (low-risk vs destructive)** | `system/automation/safety_manager.py` planned; V5 has a blocked-actions set (R4). | V5 has `SafetyManager` with a `validate(action) -> bool` and a set of blocked action types. No DSL. | Medium. | **Define a safety policy DSL**: per-action risk level, per-action confirmation requirement, per-action audit log entry. DSL is the source of truth; the LLM cannot bypass it. | **P0** |
| 22 | **Extensibility (multi-modal, local LLM, docs)** | V6 architecture does not preclude, but doesn't actively enable. | V5 has minimal multi-modal hooks. | Medium. | **Add capability extension points**: a `Provider` protocol for the Brain, a `PerceptionStrategy` protocol for vision, a `LocatorStrategy` protocol for finding elements. New strategies plug in without rewriting the loop. | **P2** |
| 23 | **Testing (real-world acceptance)** | No tests in V6. V5 has 9 tests. | V5 tests cover planner, executor, verifier, recovery, real-Windows, but **do not** cover the full conversation→action loop end-to-end with natural language. | **Critical**. The user's success criteria are real-world scenarios; V5's tests are largely unit/integration. | **Add acceptance tests per `docs/V6_ACCEPTANCE_TESTS.md`**: per category (conversation, voice, app, mouse, keyboard, windows, browser, files, media, vision, context, memory, multi-step, verification, recovery, safety). Each test: user request → expected behavior → expected verification → failure behavior → success criteria. | **P0** |
| 24 | **Engine: orchestrator, not monolith** | V6 plan inherits V5's 3,637-L `omnix_engine.py`. | V5's engine is monolithic. | **High**. The user explicitly said "do NOT reproduce a 3637-line monolithic engine." | **Redesign `OmnixEngine` as a thin orchestrator** that wires services but does not contain business logic. Each subsystem owns its logic. | **P0** |
| 25 | **Conversation state with referents ("the first result", "that window")** | Not designed. | V5 has `ConversationManager` (short-term messages) but no entity resolution. | **Critical** for the "context" requirement. | **Add an `EntityResolver` / `ReferentResolver`** that maps pronouns and demonstratives to entities from the context. | **P0** |
| 26 | **Anti-patterns (silent fallback, fake success, monolithic engine)** | Documented in `V6_ARCHITECTURE_RULES.md`. | V5 has the latent bugs R1-R12. | High if not enforced. | **Enforce** via linter rules, code review, and tests. Add CI checks for: no `import logging` in production code, no `import openai` outside `ai/`, no `frozen/` directory, no engine file > 1,000 lines. | **P0** |
| 27 | **Hybrid browser (adaptive perception, not universal ordering)** | Not explicitly designed. | V5 leans on `BrowserController` + vision. DOM and UIA partially wired. | Medium. | **Make the `PerceptionRouter` adaptive**: per (app, target, available APIs, historical reliability, confidence, world state, task requirements, latency/cost), pick the highest-confidence available strategy. Record the choice and reason. **No universal ordering is locked in.** | **P1** |
| 28 | **Migrate capabilities, not files** | Phase 0 docs were file-centric (340 V5 files vs 285 V6 placeholders). | n/a | High. The user has explicitly course-corrected. | **Rewrite all subsequent docs to be capability-centric.** Phase plans describe capabilities unlocked, not files migrated. | **P0** |
| 29 | **User-facing NL vs. internal structured capability calls** | The seam was implicit; the user could confuse the two. | V5 conflated them — `open_chrome` is a skill name visible to a casual reader. | Medium. If the seam is not explicit, a future contributor will leak the internal interface. | **Make the two layers explicit**: user input is NL only; the Brain emits typed capability calls; the `CapabilityRouter` validates them. The capability set is a type system, not a CLI. | **P0** |
| 30 | **Brain can only invoke registered capabilities** | Not in V6 plan; assumed the Brain could emit any string. | V5 has no closed capability set; the executor accepts whatever the classifier hands it. | **High**. Without this, the agent is a free-form code-executing LLM, which is a different product. | **Close the capability set**: the `CapabilityRouter` validates (capability exists, parameters valid, capability available, safety policy allows, execution context valid) on every call. The Brain may reason, but it may not invent operations outside the registry. | **P0** |
| 31 | **Open-ended agent benchmarks (generalization gate)** | `V6_ACCEPTANCE_TESTS.md` is deterministic; no open-ended tests. | V5 has 9 tests, all unit/integration, none open-ended. | **High**. A green acceptance suite does not prove the agent generalizes. | **Add `V6_OPEN_ENDED_AGENT_BENCHMARKS.md`** with at least 10 benchmarks whose exact steps are not predefined. The benchmarks gate V6 release, not every commit. | **P0** |

---

## Summary of gaps by priority

### P0 — must fix before Phase 0.5
- **Conversation/action dispatch (semantic, per turn)** — #1
- **Natural-language understanding (Brain-driven, not regex-first)** — #3, #4
- **Context split (TaskState, WorldState, EntityContext, ConversationContext, UserContext) with `ContextService` as coordinator** — #5, #25
- **Plan synthesis for unseen tasks** — #8
- **Verification (StepVerifier + GoalVerifier, hard-enforced)** — #14
- **AI Brain (replaceable, rate-limited, cached)** — #19
- **Safety DSL** — #21
- **Acceptance tests aligned with product vision** — #23
- **Engine as thin orchestrator, not monolith** — #24
- **Anti-pattern enforcement (CI + linter)** — #26
- **Capability-centric docs (revised roadmap)** — #28
- **Brain can only invoke registered capabilities (capability set is closed; `CapabilityRouter` validates every call)** — new in this revision
- **Open-ended agent benchmarks (generalization gate, separate from the deterministic acceptance tests)** — new in this revision

### P1 — needed for V6 to fulfill the product vision
- Memory with policy — #6
- Plan/executor/verifier/recovery (preserve V5, refactor for clarity) — #7
- Windows automation (preserve, fix R1-R3) — #9
- Browser automation (adaptive router, not universal ordering) — #10, #27
- Vision as one of several perception strategies — #11
- Observation loop (preserve) — #13
- Recovery (real, not stub) — #15
- Replanning (preserve) — #16
- Skills as small composable capabilities (refactor) — #17
- Capability router — #18
- Voice response (preserve) — #20

### P2 — nice-to-have, can be deferred
- OCR (preserve) — #12
- Multi-modal / extensibility hooks — #22

---

## Critical questions — answered

### Q1 (§26 of the product vision): Can the current V6 architecture evolve into a general-purpose Windows AI agent capable of performing tasks it was not explicitly programmed as one giant predefined command?

**Yes — but only with the changes listed above.** Specifically:

- V5 already has the **closed loop** (observe → plan → act → observe → verify → recover) in `core/agent/*`. The loop is sound.
- V5's **verification** (StepVerifier + GoalVerifier) is sound and tested. It must be hard-enforced, not optional.
- V5's **execution layer** (`system/automation/*`, `system/input/*`, `system/windows/*`, `system/applications/*`, `system/browser/*`) is real and runs on actual Windows. It must be preserved and patched (R1-R3), not rewritten.
- V5's **Brain** (`ai/brain_manager.py`) is the right seam. It must be expanded with provider selection, rate limiting, and prompt caching — but the seam exists.

What V5 **cannot** do today:

- It cannot reliably handle natural-language rephrasings because the `IntentClassifier` is regex-first.
- It cannot resolve "the first result" or "that window" across turns because there is no `EntityResolver`.
- It cannot synthesize a plan for an unseen task because the planner is rule-bound; the Brain is a fallback, not the primary plan-synthesizer.
- The engine is monolithic (3,637 L), which makes the above changes harder to make safely.

**Conclusion:** V6 is achievable from the V5 foundation **iff** the P0 changes are made. The P0 changes are not file-level; they are **architectural seams** (semantic NLU, context+entities split into TaskState/WorldState, plan synthesis, thin engine, safety DSL, capability-centric phases, acceptance tests, closed capability registry, open-ended benchmarks).

### Q2 (§27 of the product vision): Is the current V6 design too dependent on migrating V5's existing architecture?

**Yes, the Phase 0 docs were too file-centric.** The audit's `V5_V6_FILE_MAP.md` lists 340 V5 files and their V6 destinations. That framing biases the migration toward "preserve V5's structure." The user has explicitly said this is wrong.

**Proposed migration philosophy:**

```
MIGRATE CAPABILITIES, NOT FILES.
```

Concretely:

- For each **capability** in the product vision (e.g. "click element", "verify step", "resolve entity", "synthesize plan"), decide whether V5 has a working implementation, a partial one, or none.
- **Working**: preserve the pattern, refactor for clarity and the new seams.
- **Partial**: keep the working parts, redesign the broken parts.
- **Missing**: design from scratch in V6.

The V5 file map is a **reference**, not a **target**. The V6 file layout is determined by the product vision and the new architectural seams, not by what V5 happened to put where.

A V6 implementation may use **fewer files** than V5 (if many small V5 modules collapse into one well-named V6 module) or **more files** (if V5's monolithic engine is split into clear responsibilities). Either is fine. The judgment is capability-coverage, not file-count.

---

**OMNIX V6 PRODUCT VISION ALIGNED. NO SOURCE CODE MODIFIED. NO DEPENDENCIES INSTALLED. WAITING FOR USER APPROVAL BEFORE PHASE 0.5.**
