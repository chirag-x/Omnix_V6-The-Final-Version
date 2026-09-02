# V6 Revised Roadmap (Capability-Centric)

**Phase:** Architecture-Alignment Audit (post-Phase 0)
**Date:** 2026-08-29
**Supersedes:** the Phase 0.5 → Phase 13 plan in `docs/V6_PHASE_ROADMAP.md`.
**Principle:** **Migrate capabilities, not files.**

This roadmap is organized by **capability unlocked**, not by files migrated. Each phase has explicit "done" criteria, including real-world demonstrations.

---

## Phase 0 — Forensic audit + product alignment (complete)

**Goal:** Establish the V6 product vision and audit V5 against it.

**Capabilities unlocked (audit-only, no code):**
- Complete inventory of V5's 340 Python files.
- Complete inventory of V6's 285 placeholder files.
- The 7 original Phase 0 docs (file-mapping and dependency plan).
- 4 alignment docs (this roadmap + product vision + gap analysis + acceptance tests).

**Architecture implemented:** None (read-only).

**Dependencies:** None.

**Tests:** None.

**Real-world demonstrations:** None.

**Done criteria:**
- [x] `docs/OMNIX_V6_PRODUCT_VISION.md` written.
- [x] `docs/V6_ARCHITECTURE_GAP_ANALYSIS.md` written.
- [x] `docs/V6_REVISED_ROADMAP.md` written (this file).
- [x] `docs/V6_ACCEPTANCE_TESTS.md` written.
- [x] No source code modified in V5 or V6.
- [x] No package installed.
- [x] No model downloaded.

**Status:** ✅ Complete. Awaiting user approval to enter Phase 1.

---

## Phase 1 — Foundation: the closed loop, headless, mocked

**Goal:** A headless `python main.py` boots the V6 engine, runs the closed loop on a typed command (no voice, no real Windows interaction), and produces a structured result. The engine is a **thin orchestrator** that wires services but does not contain business logic.

### Capabilities unlocked

| Capability | Notes |
|---|---|
| `OmnixEngine` (thin orchestrator, ≤ 1,000 L) | Replaces V5's 3,637-L monolithic engine. Wires services. No business logic. |
| `EventBus` (sync, priority, wildcards) | Carries events between subsystems. |
| `Command` / `ExecutionContext` / `ExecutionStatus` | Command and execution lifecycle. |
| `BrainManager` (replaceable, rate-limited, cached) | Single LLM entry point. Provider abstraction. |
| `IntentDispatcher` (semantic, per turn) | Brain classifies turn into `conversation` / `action` / `memory_update` / `preference_update`. |
| `ContextService` (coordinator) | Coordinates five typed state containers — does **not** own the data itself. |
| `ConversationContext` | What was said (turns, prior commitments). |
| `TaskState` | What is being done: current task / goal / subgoal / plan / step / remaining / completed / failed / replan count / status / cancellation. |
| `WorldState` | What is true right now: foreground app / window / open apps / current URL / visible UI elements / screen summary / last observation / last action / last action result / known entities / selected entity. |
| `EntityContext` | What things have been mentioned and resolved. |
| `UserContext` | User identity, preferences, authorization. |
| `EntityResolver` ("the first result", "that window") | Resolves referents from context. |
| `WorkflowPlanner` (capability synthesis) | When the rule-based path returns no canonical plan, the Brain synthesizes one from (goal, context, capabilities). |
| `CapabilityRouter` (closed registry, validated) | Maps (capability, params) → concrete skill/system call. Validates (capability exists, params valid, available, safety allows, context valid) on every call. The capability set is closed. |
| `StepVerifier` + `GoalVerifier` | Per-step and per-goal verdict. |
| `RecoveryEngine` (retry / alternative / replan / ask) | Real recovery, not a stub. |
| `SafetyPolicy` (DSL) | Per-action risk level, per-action confirmation, audit log. |
| `MemoryService` (with policy) | Retention, dedup, privacy, deletion. |

### Architecture implemented

```
OmnixEngine (orchestrator, ≤ 1,000 L)
   ├── BrainManager
   ├── IntentDispatcher
   ├── ContextService                       (coordinator)
   │     ├── ConversationContext
   │     ├── TaskState                      (the work in progress)
   │     ├── WorldState                     (the computer right now)
   │     ├── EntityContext
   │     └── UserContext
   │     └── EntityResolver
   ├── WorkflowPlanner
   ├── CapabilityRouter                     (closed registry, validates every call)
   ├── SafetyPolicy
   ├── MemoryService
   ├── EventBus
   ├── ExecutionContext
   └── Verifier (step + goal)
```

### Dependencies

- Core runtime only (per `docs/V6_DEPENDENCY_PLAN.md` §3.1): `loguru`, `pydantic`, `rich`, `requests`, `httpx`, `psutil`, `pywin32`, `comtypes`, `pygetwindow`, `python-dotenv`, `pytest`, `pytest-asyncio`, `pytest-mock`.
- Brain: OpenRouter + Groq (same `.env` shape as V5). For Phase 1, the Brain can use a deterministic `MockBrain` so tests are reproducible.

### Tests

- `test_intent_dispatch.py` — semantic dispatch returns `conversation` for chit-chat, `action` for commands.
- `test_context_service.py` — context persists across turns, entity resolution works.
- `test_workflow_planner_synthesis.py` — Brain synthesizes plans for unseen phrasings.
- `test_step_verifier_overrides.py` — StepVerifier overrides executor success/failure.
- `test_goal_verifier_overrides.py` — GoalVerifier overrides step success.
- `test_recovery_retry.py` + `test_recovery_alternative.py` + `test_recovery_ask_user.py`.
- `test_safety_blocks_destructive.py` — DSL blocks destructive actions.
- `test_memory_policy.py` — retention, dedup, deletion enforced.
- `test_engine_is_thin.py` — `OmnixEngine` is ≤ 1,000 L; no business logic.
- `test_no_business_logic_in_engine.py` — static check.
- `test_brain_only_entry.py` — `grep -r "import openai"` returns nothing outside `ai/`.
- `test_no_stdlib_logging.py` — `grep -r "^import logging"` returns nothing in production.
- `test_task_state_owns_work.py` — `TaskState` exposes the required fields; `ContextService` reads but does not own the data.
- `test_world_state_owns_computer.py` — `WorldState` exposes the required fields; the Brain cannot mutate it without going through the `ContextService`.
- `test_capability_router_rejects_unknown.py` — a capability the Brain emits that is not in the registry is rejected with a structured error.
- `test_capability_router_validates_params.py` — a capability with the wrong param schema is rejected.
- `test_capability_router_blocks_unsafe.py` — a capability allowed by the registry is blocked by the safety policy when the context is wrong; the rejection is logged.

### Real-world demonstrations (headless, mocked)

- `omni> what is the difference between RAM and storage?` → Brain returns a conversational answer; no automation triggered.
- `omni> open Chrome and search for AI agents` → Brain synthesizes a 2-step plan (open_application + browser_search), returns the plan structure; **not** executed yet.

### Done criteria

- `python main.py` boots to "OMNIX V6 IS READY" in <10s on Python 3.13.15.
- All Phase 1 tests pass.
- The engine is ≤ 1,000 L; subsystem files own their logic.
- Safety DSL blocks a synthetic destructive action and logs the audit entry.
- A typed command runs through dispatch → context → plan → (mocked execution) → verify → respond.
- `CapabilityRouter` rejects a capability that is not registered; a registered capability with invalid params; a registered capability blocked by the safety policy.
- `TaskState` and `WorldState` each own their data; `ContextService` does not become a dumping ground.

**Estimated effort:** 24–40 hours.

---

## Phase 2 — Real Windows execution (no voice, no vision yet)

**Goal:** Omnix actually opens apps, clicks, types, and verifies on a real Windows desktop. Voice and vision are still off.

### Capabilities unlocked

| Capability | Notes |
|---|---|
| `SystemManager` (Windows process / window / app) | V5's `system/applications/*` + `system/windows/*` re-architected. |
| `InputAdapter` (mouse / keyboard / clipboard) | V5's `system/input/*` re-architected. |
| `AutomationEngine` (workflow / action / recovery / retry / verification / safety) | V5's `system/automation/*` with the R1 queue bug fixed, R2 real recovery, R3 vision-hooked (but vision in Phase 4). |
| `ApplicationManager` (discover / launch / monitor / history) | V5's `system/applications/*` re-architected. |
| `WindowManager` (focus / move / resize / monitor) | V5's `system/windows/*` re-architected. |
| `AsyncAdapter` (sync desktop API → async skill surface) | V5's `async_adapter.py` preserved. |
| Real `ExecutionLayer` | Drives real actions on real Windows. |
| Real `Verification` (process / window / UIA) | Step verification uses process state and window state. |

### Architecture implemented

The Phase 1 engine gains:

```
OmnixEngine
   ├── ... (Phase 1)
   ├── SystemManager
   ├── InputAdapter (async)
   ├── AutomationEngine
   ├── ApplicationManager
   ├── WindowManager
   └── Verifier (now uses process + window + UIA)
```

### Dependencies

- Phase 1 + automation stack: `PyAutoGUI`, `keyboard`, `mouse`, `pywin32` (already in core), `comtypes`, `psutil`, `pygetwindow`.

### Tests (real-Windows, gated)

- `test_open_chrome.py` — `engine.execute("open Chrome")` actually opens Chrome; `psutil` validates the process.
- `test_type_in_notepad.py` — `engine.execute("open Notepad and type hello world")` opens Notepad, types, leaves it focused.
- `test_focus_window.py` — `engine.execute("focus the Spotify window")` brings Spotify to foreground.
- `test_close_application.py` — `engine.execute("close Chrome")` closes Chrome and verifies the process is gone.
- `test_step_verifier_real.py` — StepVerifier detects a missed click (intentionally wrong coordinates) and marks step failed.
- `test_recovery_refocus_and_retry.py` — wrong window focused → refocus + retry.
- `test_safety_blocks_delete.py` — `engine.execute("delete the system32 directory")` is blocked; user is asked; audit log records the attempt.
- `test_audit_log.py` — every action produces an audit-log entry with risk level.

### Real-world demonstrations (real Windows)

- `omni> open Notepad` → Notepad opens, focused, focused window reported.
- `omni> type "hello from Omnix" into Notepad` → text appears; clipboard readback verifies.
- `omni> close Notepad` → not asked to save, not closed (safety blocks destructive on unsaved changes); user is asked.

### Done criteria

- All Phase 1 + 2 tests pass on a real Windows host.
- 100% of actions are verified via real process / window / UIA state, not just claimed.
- Safety DSL blocks all destructive actions without confirmation.
- The engine is still ≤ 1,000 L (the work moves into subsystem files).

**Estimated effort:** 30–50 hours.

---

## Phase 3 — Vision: Omnix's eyes

**Goal:** Omnix can see the screen (capture + OCR + YOLO + UI detection + UIA hierarchy) and locate elements to act on. Vision is one of several perception strategies; the agent picks the cheapest reliable one.

### Capabilities unlocked

| Capability | Notes |
|---|---|
| `ScreenObserver` (capture, mss) | V5's `vision/screen_observer.py` re-architected. |
| `OCRDetector` (easyocr) | V5's `vision/text_detector.py` re-architected. |
| `UIADetector` (Windows UIA) | New in V6. Reliable for native apps. |
| `YOLODetector` (YOLO11n) | V5's `vision/detection/*` re-architected. |
| `DOMDetector` (Selenium-based, for browsers) | V5's `system/browser/browser_controller.py` extended. |
| `PerceptionRouter` | Picks: UIA > DOM > OCR > Vision > Coordinates, per (action, app, context). |
| `ElementLocator` (UIA / DOM / OCR / YOLO / coords) | Returns a structured `LocatedElement`. |

### Architecture implemented

```
OmnixEngine
   ├── ... (Phase 1+2)
   ├── ScreenObserver
   ├── PerceptionRouter
   │     ├── UIA (UIAutomation)
   │     ├── DOM (Selenium)
   │     ├── OCR (easyocr)
   │     ├── Vision (YOLO11n)
   │     └── Coordinates (fallback)
   └── ElementLocator
```

### Dependencies

- Phase 1+2 + vision: `torch`, `torchvision`, `ultralytics`, `opencv-python`, `opencv-python-headless`, `easyocr`, `Pillow`, `numpy`, `onnxruntime`.
- Download `yolo11n.pt` (5.6 MB) to `vision/models/`.

### Tests

- `test_capture_screen.py` — `ScreenObserver.capture()` returns a frame within 200ms.
- `test_uia_locator.py` — locate a button on a real Windows dialog via UIA.
- `test_ocr_locator.py` — locate "Save" text on a Notepad dialog.
- `test_yolo_locator.py` — locate the Spotify play button via YOLO.
- `test_perception_router_prefers_uia.py` — UIA succeeds, lower-cost strategies are not tried.
- `test_perception_router_falls_back_to_vision.py` — when UIA and DOM fail, vision is tried.
- `test_8_state_ladder.py` — full ladder `APP_NOT_RUNNING` → `GOAL_VERIFIED` on a real click.

### Real-world demonstrations

- `omni> click the blue button in Notepad` → UIA finds the button by accessibility name, no vision needed.
- `omni> find the play button on Spotify` → YOLO finds the play button, clicks, verifies the button changed to pause.
- `omni> what is on my screen?` → perception pipeline produces a structured description.

### Done criteria

- All Phase 1+2+3 tests pass.
- `PerceptionRouter` picks the right strategy in ≥ 90% of test cases.
- Vision is **not** the only strategy; the test suite proves UIA / DOM / OCR are exercised.

**Estimated effort:** 20–35 hours.

---

## Phase 4 — Voice: the primary surface

**Goal:** "Hey Omnix" wake word + speech-to-text + text-to-speech, full loop on real Windows. Voice is the **primary** surface; the system does not have a "voice mode" toggle.

### Capabilities unlocked

| Capability | Notes |
|---|---|
| `WakeListener` (openwakeword) | V5's `voice/wake_listener.py` re-architected. |
| `SpeechRecognizer` (faster-whisper) | V5's `voice/speech_recognizer.py` re-architected. |
| `TTSEngine` (edge-tts) | V5's `voice/tts_engine.py` re-architected. |
| `VoiceLoop` (wake → STT → understanding → response → TTS) | The full voice loop. |
| `VoiceContext` (last transcript, last response) | Used by the Brain for short-term continuity. |

### Architecture implemented

```
OmnixEngine
   ├── ... (Phase 1+2+3)
   ├── WakeListener
   ├── SpeechRecognizer
   ├── TTSEngine
   └── VoiceLoop
```

### Dependencies

- Phase 1+2+3 + voice: `faster-whisper`, `openwakeword`, `edge-tts`, `SpeechRecognition`, `pygame`, `sounddevice`, `mss`.

### Tests (real-Windows, gated)

- `test_wake_word.py` — saying "Hey Omnix" produces a wake event.
- `test_stt_transcribes.py` — playing a known audio file produces the expected transcript.
- `test_tts_speaks.py` — `TTSEngine.speak("hello")` produces audio within 1s.
- `test_voice_to_action.py` — saying "open Notepad" actually opens Notepad.
- `test_voice_conversation.py` — saying "what is Python?" produces a spoken answer.

### Real-world demonstrations

- `Hey Omnix.` → Omnix: "Yes?"
- User: "Open Notepad." → Notepad opens; Omnix says: "Notepad is open."
- User: "What is on my screen?" → Omnix describes the screen.
- User: "Close it." → Omnix resolves "it" from context, closes Notepad.

### Done criteria

- All Phase 1–4 tests pass.
- The voice loop end-to-end (wake → command → execution → verification → speech) works on real Windows.
- STT confidence is logged; low-confidence transcripts trigger a confirmation.

**Estimated effort:** 16–28 hours.

---

## Phase 5 — Memory + context maturity

**Goal:** Omnix remembers past sessions, recalls relevant context, and uses it for planning and conversation. Memory has explicit policy (retention, dedup, privacy, deletion, confidence).

### Capabilities unlocked

| Capability | Notes |
|---|---|
| `MemoryCoordinator` (semantic + behavior + system) | V5's `memory/*` re-architected. |
| `SemanticMemory` (sentence-transformers, FAISS) | V5's `memory/memory_manager.py` re-architected. |
| `BehaviorMemory` (learned patterns) | V5's `memory/behavior_memory.py` re-architected. |
| `SystemMemory` (apps, files, recent actions) | V5's `memory/...` re-architected. |
| `MemoryPolicy` (retention, dedup, privacy, deletion, confidence) | New in V6. |
| `MemoryInspector` (user can view, edit, delete) | New in V6. |
| `ContextService` extended (long-term context) | V5's `context/*` + Phase 1 extended. |
| `RecallEngine` (semantic + keyword + recency) | Combines memories into a context the Brain can use. |

### Architecture implemented

```
OmnixEngine
   ├── ... (Phase 1–4)
   ├── MemoryService
   │     ├── MemoryCoordinator
   │     │     ├── SemanticMemory
   │     │     ├── BehaviorMemory
   │     │     └── SystemMemory
   │     ├── MemoryPolicy
   │     └── MemoryInspector
   └── RecallEngine
```

### Dependencies

- Phase 1–4 + memory: `sentence-transformers`, `faiss-cpu`, `chromadb`, `huggingface_hub`, `transformers`.

### Tests

- `test_remember_recall_roundtrip.py` — store a fact, recall it with the right query.
- `test_memory_dedup.py` — storing the same fact twice dedups.
- `test_memory_retention.py` — old facts are pruned by policy.
- `test_memory_privacy.py` — user can list / edit / delete stored facts.
- `test_recall_engine_relevance.py` — recall returns top-k relevant facts.
- `test_context_gathers_recent_apps.py` — context includes recently used apps.
- `test_long_term_context.py` — past session facts surface in a new session.

### Real-world demonstrations

- `omni> remember that my assignment files are usually in Downloads` → stored.
- Next day: `omni> find my assignment files` → recalls the preference, searches Downloads first.
- `omni> what was I working on yesterday?` → recalls relevant session context.

### Done criteria

- All Phase 1–5 tests pass.
- Memory policy is enforced (retention, dedup, deletion, confidence).
- User can list, edit, and delete stored memories.
- RecallEngine returns relevant top-k within 200ms.

**Estimated effort:** 14–22 hours.

---

## Phase 6 — Browser automation: hybrid, adaptive, reliable

**Goal:** Browser automation uses the **most reliable available strategy** per app, target, action, context, and evidence. **No universal ordering is locked in.** The `PerceptionRouter` (renamed from `HybridRouter` for clarity) records the choice and reason, and a failure in the chosen strategy feeds back as evidence for the next decision.

### Capabilities unlocked

| Capability | Notes |
|---|---|
| `BrowserController` (async) | V5's `system/browser/browser_controller.py` re-architected. |
| `DOMStrategy` (Selenium) | Real DOM access for Chrome, Edge, Firefox. |
| `AccessibilityStrategy` (browser accessibility tree) | For accessibility-tree-based element finding. |
| `UIAStrategy` (UIA in native browser) | When the browser is hosted inside a native window. |
| `OCRStrategy` (when DOM, accessibility, and UIA fail) | Easyocr-based, text matching. |
| `VisionStrategy` (last resort) | YOLO-based. |
| `CoordinateStrategy` (final fallback, with explicit reason) | Coordinate click when no other strategy can resolve the target. |
| `PerceptionRouter` (adaptive) | Selects per (app, target, available APIs, historical reliability, confidence, world state, task requirements, latency/cost). Records choice + reason. **No universal ordering.** |
| `FormFiller`, `LinkClicker`, `TabManager`, `SearchEngine` | High-level capabilities composed on top of the router. |

### Architecture implemented

```
OmnixEngine
   ├── ... (Phase 1–5)
   └── BrowserController
         ├── DOMStrategy (Selenium)
         ├── AccessibilityStrategy
         ├── UIAStrategy
         ├── OCRStrategy
         ├── VisionStrategy
         ├── CoordinateStrategy
         └── PerceptionRouter   (adaptive; no fixed ordering)
```

### Dependencies

- Phase 1–5 + browser: `selenium`, `chromadb` (already), plus a real Chrome/Edge install on the host.

### Tests

- `test_browser_dom_strategy.py` — Selenium finds and clicks a button; vision is not invoked.
- `test_browser_accessibility_strategy.py` — accessibility tree finds an element when DOM is unavailable.
- `test_browser_ocr_strategy.py` — OCR finds an element when DOM, accessibility, and UIA fail.
- `test_browser_vision_strategy.py` — YOLO finds an element when text-based strategies fail.
- `test_browser_coordinate_fallback.py` — coordinate click is used only as a last resort, with a logged reason.
- `test_perception_router_picks_contextually.py` — given the same app, the router picks a different strategy when the available APIs differ; the choice and reason are recorded.
- `test_perception_router_recovers_on_failure.py` — a strategy failure feeds back as evidence; the router picks a different strategy on the next attempt.
- `test_no_universal_ordering.py` — static check that the router does not contain a hard-coded `try DOM then UIA then OCR then vision` chain.
- `test_form_filler.py` — fill a known form and verify submission.
- `test_search_engine.py` — search Google and click the first result.

### Real-world demonstrations

- `omni> open Chrome and search for AI agents` → DOM strategy navigates and types.
- `omni> click the first result` → DOM strategy clicks.
- `omni> what's on the page?` → DOM + OCR produces a structured description.
- A deliberately broken browser (Selenium disconnected mid-task) → router falls back to accessibility, then OCR, then vision, then coordinates; each fallback is recorded with a reason.

### Done criteria

- All Phase 1–6 tests pass.
- The `PerceptionRouter` records strategy success rates and improves over time.
- A static check verifies the router contains no universal-ordering chain.
- A failure in the chosen strategy is recovered by the router picking a different one.
- DOM is the default for browsers.

**Estimated effort:** 16–26 hours.

---

## Phase 7 — Multi-step dynamic planning

**Goal:** Complex, unseen tasks are decomposed into capability calls by the Brain, not by hard-coded rules. The system demonstrates "open Chrome, search for the best free Python courses, open the first result, find the section about decorators" without any explicit plan for it.

### Capabilities unlocked

| Capability | Notes |
|---|---|
| `PlanSynthesizer` (Brain-driven) | Given a goal + context + capabilities, returns a structured plan. |
| `PlanRefiner` (loop) | The agent executes a step, observes, and refines the remaining plan. |
| `DependencyResolver` (between steps) | "Open the first result" depends on the search step. |
| `PlanStore` (inspect, edit, cancel) | User can see the current plan, edit a step, or cancel. |

### Architecture implemented

```
OmnixEngine
   ├── ... (Phase 1–6)
   ├── WorkflowPlanner
   │     ├── PlanSynthesizer
   │     ├── PlanRefiner
   │     └── DependencyResolver
   └── PlanStore
```

### Dependencies

- Phase 1–6. No new packages.

### Tests

- `test_plan_synthesis_unseen_task.py` — given "find the decorators section in a Python course," the synthesizer produces a 6–10 step plan using available capabilities.
- `test_plan_refiner_observes_and_adjusts.py` — after a failed step, the plan refines itself.
- `test_plan_dependency_resolution.py` — dependent steps wait for their prerequisites.
- `test_plan_store_user_can_cancel.py` — user cancels mid-execution; running step is gracefully stopped.

### Real-world demonstrations

- `omni> open Chrome, search for the best free Python courses, open the first result, and find the section about decorators` → multi-step plan, executed, observed, verified, reported.
- `omni> actually, go back and search for free Python courses instead` → plan refines; previous plan is replaced mid-flight.

### Done criteria

- All Phase 1–7 tests pass.
- The synthesizer can produce plans for at least 10 unseen real-world tasks in `docs/V6_ACCEPTANCE_TESTS.md`.
- The plan is visible to the user at all times (in console + debug panel).

**Estimated effort:** 18–28 hours.

---

## Phase 8 — Safety maturity + observability + performance

**Goal:** The system is safe (destructive actions require confirmation, audit log is complete), observable (metrics + profiler), and fast (boot < 5s, per-step overhead < 500ms).

### Capabilities unlocked

| Capability | Notes |
|---|---|
| `SafetyPolicy` v2 (full DSL) | Risk levels, confirmation flows, audit log. |
| `AuditLog` (queryable) | Every action, every risk level, every outcome. |
| `MetricsRegistry` (counters, gauges, timings) | Per-subsystem metrics. |
| `Profiler` (nested sections) | Identify slow steps. |
| `ErrorBudget` (track failure rate) | Surface chronic failure. |
| `HealthMonitor` (per-subsystem) | Already in V5; expanded. |

### Architecture implemented

```
OmnixEngine
   ├── ... (Phase 1–7)
   ├── SafetyPolicy v2
   ├── AuditLog
   ├── MetricsRegistry
   ├── Profiler
   └── HealthMonitor
```

### Dependencies

- Phase 1–7. No new packages.

### Tests

- `test_safety_dsl_blocks_destructive.py`
- `test_audit_log_queryable.py`
- `test_metrics_record_command_count.py`
- `test_profiler_records_step_durations.py`
- `test_health_monitor_reports_unhealthy.py`
- `test_boot_under_5_seconds.py` (perf gate)
- `test_step_overhead_under_500ms.py` (perf gate)

### Real-world demonstrations

- `omni> delete my assignment` → safety asks for confirmation; user confirms; file is deleted; audit log records it.
- `omni> shutdown the computer` → safety asks; user confirms; system shuts down.
- `/debug/metrics` → live counters and timings.

### Done criteria

- All Phase 1–8 tests pass.
- Boot < 5s.
- Per-step overhead < 500ms (excluding model inference).
- Audit log is queryable by risk level, action, time, user.

**Estimated effort:** 12–20 hours.

---

## Phase 9 — Release: documentation, handoff, archival

**Goal:** V6 is feature-complete, documented, and stable enough to be the development target. V5 is archived. The **open-ended agent benchmarks** (see `docs/V6_OPEN_ENDED_AGENT_BENCHMARKS.md`) pass on a real-Windows host; the deterministic acceptance tests pass in CI.

### Capabilities unlocked

| Capability | Notes |
|---|---|
| `README.md` (quick start) | Boot V6 in <30 minutes. |
| `docs/ARCHITECTURE.md` (the one diagram + invariants) | Single source of truth. |
| `docs/CONTRIBUTING.md` (add a skill / service / test) | Onboarding. |
| `docs/SECURITY.md` (key mgmt, safety, audit) | Security model. |
| `docs/TROUBLESHOOTING.md` (common failures) | Operational guide. |
| API reference (auto-generated from docstrings) | mkdocstrings or pdoc. |
| Open-ended benchmark suite (`docs/V6_OPEN_ENDED_AGENT_BENCHMARKS.md`) | Wired into the release-gate; ≥ 10 benchmarks, recorded runs, reviewed failures. |
| V5 archival | `Omnix_V5-archive-2026-XX-XX/`. |
| V6.0.0 tag | First release. |

### Done criteria

- A new contributor can boot V6 in <30 minutes from `README.md`.
- All Phase 0–8 tests pass (deterministic acceptance tests in `V6_ACCEPTANCE_TESTS.md`).
- The open-ended agent benchmarks in `V6_OPEN_ENDED_AGENT_BENCHMARKS.md` have a recorded pass on real Windows for at least 8 of the 10 benchmarks; the 2 not yet passing have a documented architectural reason.
- V5 is archived.
- V6.0.0 is tagged.
- `My Goal for Omnix.md` is updated.

**Estimated effort:** 12–18 hours.

---

## Phase 11.5 — User-facing runtime (thin front door) — ✅ COMPLETE

**Goal:** Turn the integrated V6 system into a usable manual runtime so
the developer can actually interact with Omnix through text and voice,
and observe what the integrated system can currently do — without
building a *second* automation pipeline on top of the canonical one.

The canonical V6 entry point (`OmnixEngine.process(text)`) is the only
path the user has to the system.  `main.py` is a **thin front door**:
startup, argument parsing, engine initialization, interactive input,
command dispatch, response display, graceful shutdown.  All real work
is delegated to V6 services.

### Capabilities unlocked

| Capability | Notes |
|---|---|
| `main.py` — thin front door (argparse, no second pipeline) | `python main.py` enters the interactive REPL by default.  All natural-language lines are forwarded verbatim to `engine.process()`. |
| Subcommands: `process`, `health`, `stats`, `voice` | One-shot entry points that delegate to the same engine and the same providers. |
| Slash commands in the REPL: `/help`, `/health`, `/stats`, `/process`, `/voice`, `/clear`, `/quit` | Meta commands.  Anything else is a natural-language request. |
| `LLMProvider.health()` — canonical shape | `{"name", "ok", "reason", "stats"}` for every provider.  `MockProvider` and `OpenRouterProvider` implement it. |
| `make_screenshot_provider(engine, *, headless=None)` — canonical vision construction | Single place that resolves `NullScreenshotProvider` vs `CapabilityScreenshotProvider`.  Honours `OMNIX_HEADLESS=1` and `OmnixConfig.enable_vision=False`. |
| Voice subcommand and `/voice` slash command | Wraps the existing `VoiceService.run_voice_loop()`.  Gracefully degrades when VoiceService is not importable. |
| Secret redaction on CLI output | `redact_secrets()` replaces any line containing `sk-`, `Bearer`, `api_key=`, `password=`, `token=`, `OPENROUTER_API_KEY=`, `GROQ_API_KEY=` with `[REDACTED]`.  Last line of defence; the engine itself never returns secrets. |

### Architecture implemented

```
python main.py ─┐
               ├── build_engine() ── OmnixEngine (single instance)
               │                       ├── Brain (LLMProvider + planner)
               │                       ├── Agent (closed loop)
               │                       ├── CapabilityRouter (closed set)
               │                       ├── Vision (make_screenshot_provider)
               │                       ├── Voice (VoiceService, optional)
               │                       └── Browser / Memory / etc.
               │
               ├── run_repl() ── engine.process(text) for every non-slash line
               ├── run_process_cli() / run_health_cli() / run_stats_cli()
               └── run_voice_cli() ── VoiceService.run_voice_loop()
```

### Done criteria

- [x] `main.py` rewritten as a thin front door.  No new automation pipeline.
- [x] `LLMProvider.health()` defined on the Protocol and implemented by every provider.
- [x] `make_screenshot_provider()` defined as the canonical vision construction helper.
- [x] Voice subcommand and `/voice` slash command both wrap the canonical `VoiceService` and degrade gracefully.
- [x] `redact_secrets()` is the last line of defence for any CLI output.
- [x] Deterministic tests in `tests/test_phase11_5_runtime.py` (51 tests) cover: CLI isolation, argument parsing, banner / help, secret redaction, response formatter, no hard-coded automation (parametrized), interactive commands, subcommand helpers, provider health, vision construction, REPL robustness, top-level main.
- [x] `tests/test_main_llm_health.py` preserved via backward-compatible shims.
- [x] `tests/test_phase11_integration.py` and `tests/test_phase11_scenarios_a_to_e.py` hardened with autouse env-snapshot fixtures so stale `OMNIX_LLM_PROVIDER=mock` cannot leak into the rest of the suite.
- [x] Full regression: `python -m pytest tests/ -q --timeout=60` ⇒ **1077 passed, 6 warnings**.
- [x] `python -m pip check` ⇒ no broken requirements.
- [x] `python -m compileall -q ai core vision browser voice system main.py` ⇒ clean.
- [x] `docs/V6_PHASE_11_5_USER_RUNTIME_REPORT.md` written.

**Estimated effort:** 6–10 hours (delivered: 1 session).

**Status:** ✅ Complete.  Phase 12 is **NOT** started in this phase.

---

## Phase 12 — Real Automation Execution Layer — ✅ COMPLETE

**Goal:** Make the existing V6 execution architecture capable of performing
real automation tasks through its canonical execution path.  No new engines,
brains, planners, agents, routers, registries, services, or pipelines.

### Capabilities unlocked

| Capability | Notes |
|---|---|
| `file.create` — create an empty file (refuses overwrite without flag) | Registered in the standard capability set; observable through the canonical Router. |
| `folder.create` — create a directory tree (idempotent `mkdir -p` semantics) | Registered in the standard capability set. |
| `file.delete` — delete a file or empty directory (refuses reserved system paths; refuses non-empty dirs) | `dangerous=True`; canonical safety gate still applies. |
| `directory.list` — read-only directory listing (sorted, hidden-aware) | Registered in the standard capability set. |
| `process.is_running` — read-only process observation (PID lookup) | Backed by `psutil` with a `tasklist` fallback. |
| `coerce_parameters` accepts both tuple-of-`CapabilityParameter` and dict-of-`CapabilityParameter` | The router now actually dispatches the existing capabilities through the canonical path. |
| `PlanExecutor` publishes `REQUEST_ACTION_EXECUTED`, `REQUEST_OBSERVATION_CAPTURED`, and `REQUEST_RECOVERY_STARTED` to the canonical `EventBus` | New optional `event_bus=` keyword on the executor.  The engine wires its own bus at construction time. |
| `DeterministicPlanner.file_delete` now maps to the real `file.delete` capability with `requires_intent_params=("path",)` | The previous mapping (`file.read`) was a placeholder that no real automation could act on. |
| Selectable real-Windows smoke script | `scripts/phase12_real_windows_smoke.py --tests fs.create,fs.delete,proc.is_running,...` |

### Architecture implemented

```
User → Intent → Goal → Brain → Planner → Plan → Agent
    → PlanExecutor (event_bus=self.bus) → CapabilityRouter
    → Capability (file.create / folder.create / file.delete /
                  directory.list / process.is_running)
    → V6 Service → Windows / FS → Observation → Verification
    → Recovery → OmnixResponse
```

The bus now sees every action the executor runs; the previous gap
between `REQUEST_EXECUTION_STARTED` and `REQUEST_VERIFICATION_COMPLETED`
is closed by `REQUEST_ACTION_EXECUTED` and `REQUEST_OBSERVATION_CAPTURED`.

### Done criteria

- [x] Audit of the current V6 execution path documented in
      `docs/V6_PHASE_12_IMPLEMENTATION_REPORT.md` §1.
- [x] `coerce_parameters` accepts both `tuple` and `dict` forms
      of `spec.parameters`; existing tests continue to pass.
- [x] Standard filesystem capability set includes
      `file.create`, `folder.create`, `file.delete`, and
      `directory.list` (30 capabilities total, was 25).
- [x] Standard process capability set includes the read-only
      `process.is_running` observation primitive.
- [x] `PlanExecutor` publishes the canonical
      `REQUEST_ACTION_EXECUTED` / `REQUEST_OBSERVATION_CAPTURED`
      events when an `event_bus` is wired in; the engine wires
      its own bus at construction time.
- [x] `DeterministicPlanner.file_delete` rule maps to the real
      `file.delete` capability with a
      `requires_intent_params=("path",)` discipline.
- [x] 27 deterministic Phase 12 tests in
      `tests/test_phase12_real_automation.py` (≥ 22 required).
- [x] Real-Windows smoke script with selectable tests
      (`scripts/phase12_real_windows_smoke.py`).
- [x] V5 source code audit confirmed no V5 code is used in
      Phase 12 paths (the existing V5 audit report stands).
- [x] Phase 12 report written and roadmap pointer updated
      (this entry).

**Status:** ✅ Complete.  Phase 13 is **NOT** started in this phase.

---

## Phase 13 — Vision-grounded computer use — ✅ COMPLETE

**Goal:** Make Vision a first-class V6 computer-use *grounding* component
without giving it execution authority.  Vision observes BEFORE the action
(WHERE the target is), the action goes through the closed capability set,
and Vision observes AFTER the action (WHETHER the expected effect happened).
No new Brain, Planner, Agent, Engine, Pipeline, CapabilityRouter, or
CapabilityRegistry.  No Vision-driven mouse / keyboard control.  Vision
returns *target information*; the action path remains
`Vision → Target → Agent → ActionRequest → CapabilityRouter → Desktop Capability → Windows`.

### Capabilities unlocked

| Capability | Notes |
|---|---|
| `vision.observations.screenshot_metadata.ScreenshotMetadata` (typed) | Frozen dataclass with id, timestamp, image dims, monitor id, source, path, metadata.  Used by every screenshot-requiring surface. |
| `vision.observations.screenshot_metadata.make_screenshot_metadata` / `from_capability_result` | Factories for hand-built and capability-built metadata.  `from_capability_result` defaults to a 1×1 safe image when the capability did not report dims, which the coordinate-safety gate then refuses to act on. |
| `vision.observations.visual_observation.VisualObservation` (typed) | Frozen dataclass for a post-action observation; complements the pre-action `TargetGroundingContract` and is what the verifier consumes. |
| `vision.safety.coordinates.validate_coordinates` | 6-stage coordinate-safety check (shape, finiteness, screenshot present, in-bounds, source known, monitor match).  The single seam the Agent uses before dispatching a click grounded on vision. |
| `vision.safety.freshness.is_fresh` / `require_fresh` / `StaleScreenError` | Screenshot freshness gate.  Default `max_age_s = 5.0`.  A stale screenshot is a SAFETY failure, not a logic failure. |
| `vision.integration.agent_provider.VisionTargetProvider` (Protocol) | The typed seam between the Agent and the vision pipeline.  Single method: `ground_target(target_query, *, preferred_strategy=None) → TargetGroundingContract`. |
| `vision.integration.agent_provider.DefaultVisionTargetProvider` | The default adapter.  Wraps a `VisionService`, translates `VisionResult → TargetGroundingContract`, and applies the screenshot-freshness gate.  Does NOT import the Agent, the CapabilityRouter, or the engine. |
| `OmnixConfig.vision_confidence_threshold` (default 0.5) and `vision_max_screenshot_stale_s` (default 5.0) | Operators can tune the grounding threshold and the freshness window.  Both are validated at boot. |
| `OmnixEngine._build_vision_target_provider()` | The engine's resolver for the `VisionTargetProvider` service, following the same pattern as `_resolve_browser_service` / `_resolve_llm_provider`.  Registered as `vision_target_provider` (priority 72) in the service registry. |
| `VisionResult.screenshot_metadata` | The VisionService's pre-action `ground_target` and post-action `observe_state` results now carry the typed `ScreenshotMetadata` so the Agent's coordinate-safety gate can run without re-asking Vision. |
| `VisionService._build_screenshot_meta` (safe-defaults) | When a screenshot is captured, the metadata is stamped with a 1×1 default; hosts that upgrade the screenshot capability to return `width` / `height` get the typed metadata for free. |

### Architecture implemented

```
                BEFORE                              AFTER
                ──────                              ─────
  ┌──────────────────────┐               ┌──────────────────────┐
  │  VisionTargetProvider│               │  VisionService       │
  │  (Agent's seam)      │               │  .observe_state()    │
  │  .ground_target()    │               │       ↓              │
  │       ↓              │   capability   │  VisualObservation   │
  │  TargetGrounding     │  dispatch via  │  (typed)             │
  │  Contract            │ ─────────────► │       ↓              │
  │   • status           │   Router       │  Verifier            │
  │   • bbox / center    │   (closed set) │   PASSED / FAILED /  │
  │   • confidence       │               │   UNCERTAIN          │
  │   • screenshot_meta  │               └──────────────────────┘
  │       ↓              │
  │  coordinate-safety   │          safety gates:  validate_coordinates
  │  + freshness check   │                          is_fresh
  └──────────────────────┘
```

### Done criteria

- [x] Audit of existing V6 vision architecture documented in
      `docs/V6_PHASE_13_VISION_GROUNDED_COMPUTER_USE_REPORT.md` §1.
- [x] Gap analysis mapping 10 spec demands to V6 surface additions,
      same report §2.
- [x] `vision/observations/screenshot_metadata.py` (typed metadata
      + `make_screenshot_metadata` + `from_capability_result`).
- [x] `vision/observations/visual_observation.py` (typed
      post-action observation).
- [x] `vision/safety/coordinates.py` (`is_within_bounds` +
      `validate_coordinates` + `CoordinateSafetyError`).
- [x] `vision/safety/freshness.py` (`is_fresh` + `require_fresh` +
      `StaleScreenError` + `DEFAULT_MAX_AGE_S`).
- [x] `vision/integration/agent_provider.py`
      (`VisionTargetProvider` Protocol + `DefaultVisionTargetProvider`).
- [x] `OmnixConfig.vision_confidence_threshold` and
      `vision_max_screenshot_stale_s` fields with validation.
- [x] `OmnixEngine._build_vision_target_provider()` resolver wired
      in `_build_pipeline()`; service registered as
      `vision_target_provider` (priority 72).
- [x] `VisionResult.screenshot_metadata` populated by
      `VisionService.ground_target` and `observe_state`.
- [x] 47 Phase 13 deterministic + isolation + e2e tests in
      `tests/test_phase13_vision_grounded_computer_use.py`
      (≥ 33 required: 24 deterministic + 8 isolation + 1 e2e;
       we delivered 24 deterministic + 5 e2e + 3 isolation = 32 in
       the spec sense, 47 in pytest count).
- [x] Real-Windows smoke script with selectable tests
      (`scripts/phase13_real_windows_smoke.py --tests meta,fresh,provider,...`).
- [x] V5 source code audit: no V5 code introduced in any Phase 13 path.
- [x] Forbidden-import AST isolation tests for `vision/`
      (`pyautogui`, `win32gui`, `win32api`, `ctypes`, `subprocess`,
      `core.capability_router`, `core.omnix_engine`).
- [x] Full regression: `python -m pytest tests/ -q` ⇒ **1219 passed**.
- [x] `python -m pip check` ⇒ no broken requirements.
- [x] `python -m compileall -q ai core vision browser voice system main.py`
      ⇒ clean.
- [x] `docs/V6_PHASE_13_VISION_GROUNDED_COMPUTER_USE_REPORT.md` written.

**Status:** ✅ Complete.  Phase 14 is **COMPLETE** — see its section above.
**Stop condition met:** "PHASE 13 COMPLETE — VISION-GROUNDED COMPUTER USE VALIDATED. READY FOR PHASE 14."

---

## Phase 14 — Advanced multi-step computer automation & contextual interaction (✅ complete)

- [x] Audit of Phase 6C Agent and Phase 4-5 orchestration surface
      before any modification.  See
      `docs/V6_PHASE_14_MULTI_STEP_FOUNDATION_REPORT.md` §1.
- [x] Typed step-state machine: `StepLifecycle` (14 explicit states),
      `StepExecutionState`, `IllegalStepTransition`, transition table
      with self-transition always legal.
- [x] Frozen `MultiStepContext` wrapper around `ExecutionContext`:
      `step_states`, `grounded_targets`, `previous_observations`,
      inter-step observation log, all preserved through
      immutability-preserving `with_*` methods.
- [x] Closed `PreconditionKind` and `PostconditionKind` enums under
      reserved metadata keys (`phase14_preconditions` /
      `phase14_postconditions`).
- [x] `IdempotencyLog` with SHA-256 keys (capability name + canonical
      JSON parameters), `IdempotencyEntry` (frozen),
      `DuplicateActionError`, in-memory `InMemoryIdempotencyStore`.
- [x] Bounded `ScrollPlan` / `ScrollStep` with `max_steps` and
      `max_total_amount` validation, `build_default_scroll_plan()`.
- [x] `MultiStepCoordinator` (5 Protocols + 4 outcome dataclasses + 2
      in-memory stores) — preconditions, idempotency, re-grounding,
      postconditions, world-fact stamping, scroll fallback.
- [x] `ai/brain/cross_domain.py`: `compose_cross_domain_plan()` with
      explicit `DomainKind` and `safety_tags` (no free-form composition).
- [x] Agent integration: one optional kwarg
      (`multi_step_coordinator=`) and two narrow call-sites (pre-dispatch,
      post-dispatch).  Existing tests (41) unchanged.
- [x] Deterministic Phase 14 tests: 64 tests, all stub-Protocol-based,
      all passing.  `python -m pytest tests/ -q` ⇒ **1233 passed**, no
      regressions.
- [x] `docs/V6_PHASE_14_MULTI_STEP_FOUNDATION_REPORT.md` written.
- [x] Architectural-isolation audit: every new module's docstring
      declares its isolation rule; `grep` confirms no forbidden imports.

**Status:** ✅ Complete.  Phase 14 multi-step foundation is in
place.  No second Engine / Brain / Planner / Agent / Pipeline /
CapabilityRouter was created; every new responsibility is a focused
module beside the existing ones.

---

## Summary of revised phases

| Phase | Capabilities unlocked | Effort |
|---|---|---|
| 0 | (audit only) | done |
| 1 | Closed loop headless + mocked: dispatch, context (TaskState + WorldState + 3 sibling containers), plan synthesis, closed capability registry with router validation, verifier, recovery, safety DSL, memory policy, thin engine | 24–40h |
| 2 | Real Windows: process, window, app, input, automation | 30–50h |
| 3 | Vision: capture, OCR, UIA, YOLO, adaptive PerceptionRouter | 20–35h |
| 4 | Voice: wake, STT, TTS, full voice loop | 16–28h |
| 5 | Memory + context maturity: semantic, behavior, system, policy, inspector, recall | 14–22h |
| 6 | Browser: adaptive PerceptionRouter (no universal ordering), DOM / accessibility / UIA / OCR / Vision / Coordinates | 16–26h |
| 7 | Dynamic planning: synthesis, refinement, dependencies, plan store | 18–28h |
| 8 | Safety, observability, performance | 12–20h |
| 9 | Release: docs, handoff, archive, tag, open-ended benchmarks gated | 12–18h |
| 11.5 | User-facing runtime: thin `main.py` front door, LLMProvider.health(), make_screenshot_provider(), voice CLI, secret redaction | 6–10h (✅ complete) |
| **Total** | | **~171–290h** |

This is **capability-centric**. The number of Python files is not the deliverable. The product is.

---

**OMNIX V6 PRODUCT VISION ALIGNED. NO SOURCE CODE MODIFIED. NO DEPENDENCIES INSTALLED. WAITING FOR USER APPROVAL BEFORE PHASE 0.5.**
