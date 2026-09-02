# OMNIX V6 — COMPLETE FORENSIC ARCHITECTURE AUDIT

**Audit type:** READ-ONLY static code inspection
**Date:** 2026-09-01
**Repo:** `E:\Coding\Omnix\Omnix_V6- The final version`
**Branch:** `main`
**Last known good commit context:** `7518673 phase 15` (working tree has many uncommitted modifications across Phases 16–17)

---

## EXECUTIVE SUMMARY (PART 28)

Omnix V6 is a Windows-resident, multi-stage AI agent runtime. The system is structurally well-segmented into 8 major subsystems (engine, brain, agent, capabilities, application, vision, voice, services) connected through a typed service registry and an event bus. The intent → goal → plan → execute → verify → recover → respond closed loop is real and end-to-end wired.

**What works in the current codebase:**
- Pipeline is observably correct: `RequestPipeline.process()` (568 lines, `core/pipeline.py`) calls the Brain, hands the resulting Goal + Intent to the Agent, emits typed `RequestEvent` records at each stage, and maps final `AgentState` → `ResponseStatus`.
- The deterministic planner (905 lines, `ai/brain/deterministic.py`) covers the long tail of local app commands without LLM calls, with built-in `expected_effect` injection (Phase 14.1) and compound-request decomposition (Phase 14.2).
- The capability system enforces a strict 5-step validation pipeline (existence → parameters → availability → safety → dispatch) and a closed-shell seam (R-21) at the `Capability.execute()` boundary.
- Service registry uses Kahn's algorithm for topological dependency ordering with cycle detection and per-record priority.
- The vision subsystem returns `GroundedElement` with an explicit 11-value status enum (no boolean claims), and its public API has 8 functions that *return* `VerificationVerdict` rather than raise.
- The voice subsystem is fully detached from the Brain — it only captures audio, transcribes, hands text to `engine.process()`, and speaks the result.

**What is broken, in conflict, or fragile (full detail in Parts 20–26):**
1. **R-21 contradiction in `WindowsApplicationService.launch()`** uses `subprocess.Popen(flags, shell=True)` directly, bypassing the capability seam and the otherwise universal anti-shell stance.
2. **Phase 15 `TargetContextResolver`** had a documented bug (commit comment in `system/application/target_context.py`) where ActionStatus.VERIFIED was incorrectly accepted as a target status — the resolver silently returned `None` on every real result before the fix.
3. **`ExpectedEffect` requirement is asymmetrically enforced**: app commands require it or the goal verifier conservatively marks UNCERTAIN and triggers a replan, but non-app rules in the deterministic planner do not require it, creating inconsistent verifier behavior.
4. **System2Brain and RequestRouter are additive but uncoordinated with AIEscalationGate**: three layers now sit in the path (Brain's planner → System2Brain's RequestRouter → service-registry-resolved AIEscalationGate), each deciding "do we call the LLM" with overlapping vocabulary.
5. **Working tree has ~50+ modified files and ~30 untracked files** spanning multiple unfinished phases (16, 17); the audit reflects the state on disk, not the last clean commit.

The architecture is *coherent at a macro level* but has *boundary leaks* at subsystem edges that will surface as integration bugs in Phase 18. The audit is descriptive only — no fix plan is provided (per PART 27).

---

## PART 1 — DIRECTORY TREE

Top-level structure of the repository at audit time:

```
Omnix_V6- The final version/
├── ai/                          # AI subsystem (brain, intent, provider)
│   ├── brain/                   # Brain orchestrator + deterministic planner + System2
│   ├── intent/                  # Intent interpretation (LLM-based)
│   ├── provider/                # LLM provider abstraction (OpenRouter, Mock)
│   └── ...
├── browser/                     # Browser automation (Playwright wrapper)
│   └── ...
├── core/                        # Engine, pipeline, orchestration, capabilities
│   ├── capabilities/            # Standard capability implementations
│   ├── events/                  # Event bus + typed events
│   ├── orchestration/           # Agent, plan executor, verifier, recovery
│   ├── services/                # Service-layer facades (browser, vision, memory, app)
│   ├── state/                   # Context + runtime state
│   ├── execution/               # Execution interfaces
│   ├── capability_router.py     # 5-step validation pipeline
│   ├── capability_registry.py   # Capability registration
│   ├── configuration.py         # OmnixConfig
│   ├── errors.py                # Error taxonomy
│   ├── events/                  # Event bus + typed events
│   ├── health_monitor.py        # R-9 health surface
│   ├── lifecycle.py             # LifecycleMixin / LifecycleState
│   ├── omnix_engine.py          # Thin root orchestrator
│   ├── pipeline.py              # RequestPipeline
│   ├── responses.py             # OmnixResponse / ResponseStatus
│   ├── results.py               # CapabilityStatus / CapabilityResult
│   └── service_registry.py      # Typed service locator
├── docs/                        # Phase reports (PHASE_15..17)
├── scripts/                     # Smoke + probe scripts
├── system/                      # OS-level subsystems
│   ├── application/             # WindowsApplicationService + catalog + discovery
│   ├── clipboard/               # Clipboard service
│   ├── filesystem/              # Filesystem service
│   ├── input/                   # Input service (mouse/keyboard)
│   ├── processes/               # Process service (psutil)
│   └── windows/                 # Window service (Win32)
├── tests/                       # Test suite
├── vision/                      # Vision subsystem
│   ├── api.py                   # Public 8-function API
│   ├── grounded_element.py      # 11-value status enum
│   ├── recovery.py              # IoU-based re-observation
│   ├── router/                  # PerceptionRouter
│   ├── safety/                  # Vision safety policies
│   ├── screen/                  # Screenshot provider
│   ├── strategies/              # UIA / OCR / Visual / Coordinates
│   ├── observations/            # Observation types
│   ├── trace/                   # Vision trace recording
│   ├── integration/             # Cross-subsystem integration
│   └── ...
├── voice/                       # Voice subsystem
│   ├── audio/                   # MicrophoneInput
│   ├── stt/                     # FasterWhisperProvider
│   ├── tts/                     # SAPITTSProvider
│   ├── vad/                     # SimpleVAD
│   ├── wake/                    # Wake-word listener
│   ├── session/                 # Voice session
│   ├── service.py               # VoiceService
│   ├── runtime.py               # VoiceRuntime
│   └── contracts.py             # Voice contracts
├── main.py                      # Entry point
├── README.md
└── requirements.txt
```

The tree is flat enough that each subsystem can be located by simple pattern. There are no deeply nested package hierarchies.

---

## PART 2 — FILE-BY-FILE INVENTORY (SUBSYSTEM-WIDE)

The full inventory of ~200 source files is too long to enumerate here. The reading that informed this audit covered (in depth):

**Engine / pipeline / config**
- `main.py` (~1250 lines): Entry point. `run_unified_interactive()` is the canonical runtime. Constructs `OmnixConfig` → `ServiceRegistry` → registers subsystems → `initialize_all()` → constructs `RequestPipeline` → attaches to REPL/voice loop.
- `core/omnix_engine.py` (~1430 lines): Thin root orchestrator. Wires together Brain, Agent, services, capabilities, event bus. Does not contain business logic.
- `core/pipeline.py` (568 lines): `RequestPipeline`. Three cancellation seams, fast-path short-circuit, Brain handle_text, Agent run_goal.
- `core/configuration.py` (466 lines): `OmnixConfig`. Reads env / defaults. Used everywhere via dependency injection.
- `core/service_registry.py` (424 lines): `ServiceRegistry`. Kahn's algorithm topological sort, RLock-protected, classification (critical/background/on_demand).

**Orchestration**
- `core/orchestration/agent.py` (~82KB): `Agent` closed loop. State machine RECEIVING_GOAL → PLANNING → PLAN_READY → EXECUTING → OBSERVING → EVALUATING → DECIDING → CONTINUE/RECOVER/REPLAN/terminal.
- `core/orchestration/agent_result.py`: `AgentState` enum, `AgentResult` (frozen).
- `core/orchestration/models.py` (~1000+ lines): `Goal`, `Intent`, `IntentKind`, `ActionKind`, `ActionRequest`, `Plan`, `PlanStep`, `PlanStatus`, `ExpectedEffect`, `Observation`, `ObservationSource`, `Verifier`, `VerificationVerdict`, `Failure`, `FailureKind`, `RecoveryAction`, `RecoveryDecision`, `ExecutionContext`. Includes the `_FORBIDDEN_SHELL_TOKENS` regex for R-21.
- `core/orchestration/plan_executor.py`: `PlanExecutor` (closed loop driver, one step at a time).
- `core/orchestration/verifier.py`: `StepVerifier`, `GoalVerifier` (returns tri-state verdict).
- `core/orchestration/verifier_router.py`: Selects StepVerifier vs GoalVerifier per step.
- `core/orchestration/recovery.py`: `RecoveryEngine`, `RecoveryClassifier` (Phase 17).
- `core/orchestration/failure_classifier.py`: `FailureKind` classifier.
- `core/orchestration/multi_step_coordinator.py`: Phase 14 multi-step coordination.
- `core/orchestration/cancellation.py`: `CancellationToken`.
- `core/orchestration/dag.py`: Plan DAG.
- `core/orchestration/progress.py`: Progress reporting.
- `core/orchestration/retry.py`: Retry policy.
- `core/orchestration/__init__.py` (344 lines): re-exports all of the above.
- `core/orchestration/interfaces.py`, `step_state.py`, `multi_step_context.py`, `idempotency.py`, `preconditions.py`, `scroll.py`: smaller orchestration helpers.

**Capabilities**
- `core/capabilities/__init__.py` (238 lines): `register_standard_capabilities()` — instantiates all built-in capabilities and registers them.
- `core/capability_router.py` (328 lines): 5-step validation pipeline.
- `core/capability_registry.py`: `CapabilityRegistry`.
- `core/capabilities/_dispatch.py`: dispatcher helper.
- `core/capabilities/desktop.py`, `desktop_application.py`, `desktop_keyboard.py`, `desktop_mouse.py`, `desktop_observation.py`, `desktop_window.py`: per-domain capability files.
- `core/capabilities/browser_capabilities.py`: 16 browser capabilities (navigate, click, type, extract, back, forward, reload, etc.).
- `core/capabilities/filesystem.py`: file/foldr operations including the dangerous `FileDeleteCapability`.
- `core/capabilities/process.py`: `RunCommandCapability`, `ProcessIsRunningCapability`.

**Services**
- `core/services/app_dispatcher.py`: `FastPathDispatcher` (Phase 15).
- `core/services/browser_service.py`: Playwright-backed `BrowserService`.
- `core/services/vision_service.py`: `VisionService` facade.
- `core/services/memory_service.py`: `MemoryService` interface.
- `core/services/sqlite_memory_store.py`: `SQLiteMemoryStore` impl.
- `core/services/local_decision_engine.py`: Phase 15 local decision engine.
- `core/services/ai_escalation_gate.py`: `AIEscalationGate` (decides when to call the LLM).
- `core/services/speech_queue.py`: `SpeechQueue` (priority, dedup, bypass).
- `core/services/progress_narration.py`, `progress_reporter.py`: progress surfaces.
- `core/services/readiness.py`: `ReadinessReport` gate.

**System layer**
- `system/application/__init__.py`: re-exports.
- `system/application/app_service.py` (283 lines): `WindowsApplicationService`.
- `system/application/catalog.py` (276 lines): `ApplicationCatalog`.
- `system/application/resolver.py` (143 lines): `ApplicationResolver` + `GENERIC_ALIASES` table.
- `system/application/discovery.py` (558 lines): 5 sources (RegistryUninstall, AppPaths, StartMenu, Path, Process).
- `system/application/models.py` (125 lines): `ApplicationRecord`, `Resolution`.
- `system/application/target_context.py` (479 lines): `TargetContextResolver` (Phase 15).
- `system/application/uwp_source.py` (359 lines): `UWPSource`.
- `system/input/input_service.py`: `WindowsInputService` (mouse/keyboard).
- `system/windows/window_service.py`: `WindowsWindowService`.
- `system/filesystem/filesystem_service.py`, `system/processes/process_service.py`, `system/clipboard/clipboard_service.py`: per-domain service facades.

**Brain / intent / provider**
- `ai/brain/__init__.py`: re-exports `Brain`, `System2Brain`.
- `ai/brain/brain.py` (309 lines): `Brain` orchestrator.
- `ai/brain/deterministic.py` (905 lines): `DeterministicPlanner` with `_DEFAULT_RULES` dict.
- `ai/brain/system2.py` (541 lines): `System2Brain` (additive).
- `ai/brain/router.py` (298 lines): `RequestRouter` (deterministic verb router).
- `ai/brain/llm_planner.py`, `llm_tracking.py`, `narration.py`, `validation.py`, `exceptions.py`, `cross_domain.py`, `discovery.py`: supporting brain modules.
- `ai/brain/recovery/`: failure classification helpers.
- `ai/brain/task/`: Task state machine models.
- `ai/intent/`: `interpreter.py`, `specs.py`, `validation.py`.
- `ai/provider/`: `contracts.py`, `base.py`, `openrouter.py`, `mock.py`, `selection.py`, `errors.py`.

**Vision**
- `vision/api.py` (873 lines): 8 public functions.
- `vision/grounded_element.py` (588 lines): `GroundedElement` + 11-value status.
- `vision/recovery.py` (267 lines): `retry_with_strategy`, `reobserve_and_compare`.
- `vision/__init__.py` (126 lines): public namespace.
- `vision/router/`: `PerceptionRouter` + strategy selection.
- `vision/strategies/`: UIA, OCR, Visual, Coordinates.
- `vision/screen/`: Screenshot provider + multi-monitor.
- `vision/observations/`, `vision/safety/`, `vision/trace/`, `vision/integration/`, `vision/screen_description.py`: supporting.

**Voice**
- `voice/service.py` (215 lines): `VoiceService`.
- `voice/runtime.py` (575 lines): `VoiceRuntime` (System 9).
- `voice/contracts.py` (68 lines): `VoiceState` enum + dataclasses.
- `voice/audio/`, `voice/stt/`, `voice/tts/`, `voice/vad/`, `voice/wake/`, `voice/session/`: per-stage impls.
- `voice/policy.py`, `voice/progress_bridge.py`, `voice/startup_announcer.py`: supporting.

**Events / state**
- `core/events/event_bus.py`: `EventBus`.
- `core/events/event_types.py`: typed frozen events (Engine/Service/Capability/Task/World/Conversation/Error/Health + RequestEvent for pipeline).
- `core/state/context_service.py`, `core/state/contexts.py`, `core/state/domain.py`, `core/state/runtime_state.py`, `core/state/inactivity_timer.py`: state layer.
- `core/lifecycle.py`: `LifecycleMixin`, `LifecycleState`.
- `core/health_monitor.py`: R-9 health surface.
- `core/results.py`, `core/responses.py`, `core/errors.py`: data contracts.

**Browser**
- `browser/__init__.py`: re-exports `BrowserService`, `BrowserSession`. The actual Playwright logic lives in `core/services/browser_service.py` and `core/capabilities/browser_capabilities.py`; the top-level `browser/` package is a thin facade.

---

## PART 3 — SUBSYSTEM MAP

Eight first-class subsystems, each owned by a directory:

| Subsystem | Owner | Public surface | Reads | Writes |
|---|---|---|---|---|
| Engine | `core/omnix_engine.py`, `core/pipeline.py`, `main.py` | `OmnixEngine`, `RequestPipeline` | All | All (orchestrates) |
| Brain | `ai/brain/` | `Brain`, `System2Brain`, `RequestRouter` | `IntentInterpreter`, `Planner`, `MemoryService`, `AIEscalationGate` | None (read-only by design) |
| Agent | `core/orchestration/` | `Agent`, `AgentState`, `AgentResult` | `PlanExecutor`, `Verifier`, `RecoveryEngine` | `Plan`, `Goal`, `Intent` returned to caller |
| Capabilities | `core/capabilities/`, `core/capability_router.py` | `Capability`, `CapabilityRegistry`, `CapabilityRouter` | Services via DI | OS / filesystem / browser / windows |
| Application | `system/application/` | `ApplicationService`, `ApplicationResolver`, `ApplicationCatalog` | Registry, StartMenu, AppPaths, Path, Process, UWP | Process spawning via `Popen` (R-21 leak) |
| Vision | `vision/` | `observe`, `describe`, `find`, `locate`, `is_visible`, `is_focused`, `wait_for`, `verify` | Screenshot, UIA, OCR | None |
| Voice | `voice/` | `VoiceService`, `VoiceRuntime` | Mic, Wake, STT | TTS, RuntimeState |
| Services | `core/services/` | FastPath, Memory, Browser, Vision, AIEscalation, SpeechQueue, Readiness | Many | Many |

Each subsystem owns its directory. Subsystems are connected *only* through:
1. The `ServiceRegistry` (typed service locator with topological init).
2. The `EventBus` (typed frozen events).
3. Direct dependency injection (constructor params).

Cross-subsystem method calls outside these three seams are forbidden by rule (R-11). This is enforced by code review and the design of `ServiceRegistry` / `EventBus`, but is *not* enforced by a linter or import check.

---

## PART 4 — CORE ARCHITECTURE

The core architecture is a three-layer pipeline:

```
USER TEXT
   │
   ▼
┌────────────────────────────────────────────────────────────┐
│ 1. RequestPipeline (core/pipeline.py)                      │
│    • Cancellation seam #1 (entry)                          │
│    • FastPathDispatcher (Phase 15) — single-step shortcut  │
│    • Cancellation seam #2 (post-fast-path)                 │
│    • Brain.handle_text() with Memory recall                │
│    • Cancellation seam #3 (post-brain)                     │
│    • Agent.run_goal(goal, intent)                          │
│    • Maps AgentState → ResponseStatus, sanitizes text      │
└────────────────────────────────────────────────────────────┘
   │                          │                          │
   ▼                          ▼                          ▼
┌──────────────┐    ┌──────────────────┐    ┌──────────────────────┐
│ 2. Brain     │    │ 3. Agent (closed │    │ 4. EventBus          │
│ (read-only)  │    │   loop)          │    │ (observability seam) │
│              │    │                  │    │                      │
│ Intent       │    │ Goal → Plan      │    │ RequestEvent         │
│ Goal         │    │   → execute      │    │ AgentEvent           │
│ Plan         │    │   → verify       │    │ CapabilityEvent      │
│              │    │   → recover      │    │ ServiceEvent         │
│ (no side     │    │   → replan       │    │ EngineEvent          │
│  effects)    │    │   → terminal     │    │ WorldEvent           │
└──────────────┘    └──────────────────┘    └──────────────────────┘
```

The Brain is *strictly read-only* — it never executes, never dispatches, never calls capabilities. This is a documented rule and `Brain.handle_text()` does not have any capability/service reference. The Agent owns all execution.

The Service Registry is the *only* place where subsystems learn about each other. The Event Bus is the *only* place where subsystems broadcast state.

---

## PART 5 — REAL REQUEST LIFECYCLE

Trace of `"open chrome"` from user to response:

1. **User input** (REPL or voice.transcribe) → `engine.process("open chrome")`.
2. **Cancellation token creation** (engine) with `correlation_id`.
3. **`RequestPipeline.process()`** (seam 1, entry): checks token; not cancelled → continues.
4. **FastPathDispatcher.try_dispatch("open chrome")**:
   - Tokenizes: verb=`open`, target=`chrome`.
   - Looks up `ApplicationResolver.resolve("chrome")` → matches `GENERIC_ALIASES["googlechrome"]` → `chrome` → `ApplicationRecord` (confidence 0.95 from AppPaths).
   - Calls `ApplicationService.is_installed("chrome")` → True.
   - Calls `ApplicationService.launch("chrome")` → `subprocess.Popen(flags, shell=True)` (R-21 leak).
   - Returns `CapabilityResult(status=VERIFIED, capability_name="desktop.application.open", details={"app_name": "chrome"})`.
5. **Pipeline fast-path success**: maps to `OmnixResponse(status=OK, text="Opening Chrome.", agent_state=COMPLETE)`. Emits `REQUEST_COMPLETED` event.
6. **Returns to engine**; engine routes to TTS / REPL output.

Trace of `"open chrome and search for omnix"` (compound):

1-3. As above.
4. **FastPathDispatcher**: cannot handle compound → returns None.
5. **Brain.handle_text("open chrome and search for omnix", context_snapshot={memory_hit_count: 0})**:
   - Calls `IntentInterpreter.interpret()` → returns `Intent(kind=COMPOUND_REQUEST, parameters={"clauses": [...], "compound_request": True})`.
   - Calls `intent.to_goal()` → `Goal(description="open chrome and search for omnix", success_criteria=...)`.
   - Calls `DeterministicPlanner.plan(goal, intent)` → `_plan_compound_request()`:
     - Splits on `and` → clause 1: `open chrome`, clause 2: `search for omnix`.
     - `_classify_clause()` regex maps clause 1 → OPEN_APPLICATION with `app_name="chrome"`, `implicit_target="chrome"`.
     - Clause 2 → `control_application` action with `implicit_target="chrome"` (carried forward), `action="search"`, `target="omnix"`.
     - Produces 2-step Plan: step 1 = `desktop.application.open(chrome)`, step 2 = capability template with `app_name="chrome"` + action=`search` + target=`omnix`.
   - Returns `BrainResult(status=ok, goal, plan, intent)`.
6. **Pipeline seam 3** (post-brain): checks token; not cancelled.
7. **Agent.run_goal(goal, intent)**:
   - State machine → PLANNING (uses pre-built plan, skips LLM).
   - → EXECUTING step 1: `desktop.application.open` → VERIFIED.
   - → EXECUTING step 2: capability call with search action → `ExpectedEffect` (page contains "omnix") → verifier runs → VERIFIED.
   - → DECIDING → COMPLETE.
8. **`_from_agent_result()`** maps `AgentState.COMPLETE` → `ResponseStatus.OK` → `OmnixResponse("Done.", OK)`.

This trace is the canonical happy path. The closed loop (PLANNING → EXECUTING → OBSERVING → EVALUATING → DECIDING) is the same for all multi-step intents.

---

## PART 6 — BRAIN ARCHITECTURE (LLM CALL POINTS)

**Two-stage AI pipeline** (`ai/brain/brain.py`, 309 lines):

```
text → interpreter.interpret() → Intent
    → intent.to_goal()        → Goal
    → planner.plan(goal, intent) → Plan
    → BrainResult(status, plan, goal, intent, ...)
```

**Where the LLM is actually called:**
1. `IntentInterpreter.interpret()` (LLM-based) — only path that hits the LLM. The deterministic planner (`DeterministicPlanner`) does *not* call the LLM; it has a static rule table that maps `IntentKind` → capability templates.
2. `LLMPlanner` (in `ai/brain/llm_planner.py`, per docs) — used when the deterministic planner has no rule for a given intent kind. This is the "LLM fallback" path.
3. `System2Brain` (Phase 17) — additive layer that *also* tracks LLM calls via `LLMCallTracker` but does not itself call the LLM; it delegates to existing Brain.
4. `AIEscalationGate` (Phase 15) — service-registry-resolved gate that decides whether the LLM is consulted for hybrid tasks. Sits *behind* `RequestRouter` (System2Brain) and *in front of* the LLM provider.

**Deterministic planner rule table** (`ai/brain/deterministic.py`, 905 lines):
- `_DEFAULT_RULES` is a dict mapping `IntentKind` → list of capability templates.
- Phase 14.1 forces `expected_effect` on app commands (so step-level verifier has a check to run).
- Phase 14.2 handles `IntentKind.COMPOUND_REQUEST` via `_plan_compound_request()` + `_classify_clause()` regex.
- UI click rules project planner→vision contract via metadata keys (`vision_pre_action`, `vision_target_query`, `vision_preferred_strategy`).
- Browser rules require explicit intent params (locator_kind, locator_value, text) — refuses to invent.
- `file_move`/`file_copy` degrade to `file.read` (placeholder); LLM planner is the canonical path.
- `file_delete` uses the dangerous `file.delete` capability (safety layer must still authorize).

**System2Brain** (`ai/brain/system2.py`, 541 lines):
- Additive layer on top of Brain. Does NOT modify existing Brain/Agent/Planner/Pipeline.
- Routes user text via `RequestRouter` to `TaskKind` (CONVERSATIONAL/COMPUTER_USE/HYBRID/UNKNOWN).
- Tracks structured `Task` with state, steps, traces, LLM call history.
- `_absorb_brain_result()` projects Brain result into Task (uses `dataclasses.replace`).
- `_project_plan()` projects `plan.steps` into `TaskStep` list (max 64).
- Strict imports block: `subprocess`, `pyautogui`, `win32gui`, `core.capability_router`, `core.omnix_engine`, `ai.provider` are explicitly forbidden.

**RequestRouter** (`ai/brain/router.py`, 298 lines):
- Deterministic, pure function.
- Verb tables: `_CONVERSATIONAL_TRIGGERS` (21), `_LOCAL_VERBS` (47), `_GENERATIVE_VERBS` (~15).
- Decision: conversational short-circuit → match local verbs → check generative markers → if both local+generative: HYBRID (escalate); if local only: COMPUTER_USE (no escalate); if generative only: HYBRID (escalate); else UNKNOWN (escalate).
- Returns `RoutingDecision` (frozen) with kind, escalate, reason, matched verbs.
- Sits in front of `AIEscalationGate` — the gate decides whether the LLM is consulted for the generative part of a hybrid task.

**Three LLM-decision layers** (architectural observation):
- `RequestRouter` (System2Brain) — *what kind of task is this?*
- `AIEscalationGate` (Phase 15) — *do we call the LLM?*
- `IntentInterpreter` — *what does the user mean?*

These layers compose but use overlapping vocabulary ("escalate", "hybrid"). This is documented in PART 25 (architectural conflicts).

---

## PART 7 — APPLICATION INTELLIGENCE

**7-submodule design** under `system/application/`:

1. `__init__.py` (40 lines): re-exports.
2. `app_service.py` (283 lines): `WindowsApplicationService(ApplicationService, LifecycleMixin)`. Operations: `resolve(name)`, `is_installed(name)`, `launch(app_name, args)`, `focus(app_name)`, `close(app_name, force)`, `is_running(app_name)`, `list_running()`. Uses `psutil` + `Popen`. **R-21 leak**: `launch` uses `subprocess.Popen(flags, shell=True)`.
3. `catalog.py` (276 lines): `ApplicationCatalog`. Plain dict index by `normalized_name`, aliases, executable stem. `_merge()` keeps highest confidence. `_targeted_scan()` on miss. Refresh rebuilds from scratch.
4. `resolver.py` (143 lines): `ApplicationResolver`. Three outcomes: `found`/`not_found`/`ambiguous`. `GENERIC_ALIASES` table: `msedge→edge`, `googlechrome→chrome`, `code→visualstudiocode`, `vscode→visualstudiocode`, `calc→calculator`, `winrt→windows`, `explorer→fileexplorer`. `AMBIGUITY_CONFIDENCE_GAP = 0.15`. Three-step: direct lookup → generic alias → substring candidates.
5. `discovery.py` (558 lines): 5 sources — `RegistryUninstallSource` (conf 0.9, 8s timeout, 4 hives), `AppPathsSource` (conf 0.95, 4s), `StartMenuSource` (conf 0.7, 4s, pywin32 WScript.Shell), `PathSource` (conf 0.5, 4s), `ProcessSource` (conf 1.0, 2s). `default_sources()` returns the list in this order.
6. `models.py` (125 lines): `ApplicationRecord` (frozen=True) with display_name, normalized_name, executable, executable_path, launch_command, source, installed, aliases, confidence, metadata. `Resolution` (frozen) with status, record, candidates, reason.
7. `target_context.py` (479 lines): `TargetContextResolver` (Phase 15). Generic app-agnostic target acquisition. NO app-name branching. Composes `ApplicationService` + `WindowService` + `TargetContextStore` + `ForegroundWindowReader`. Methods: `acquire(app_name, window_title, expected_ui_state)`, `acquire_hwnd(hwnd)`, `is_foreground(hwnd)`, `_wait_for_focus(hwnd)` (state-based polling, no `time.sleep` in capability). **Documented Phase 15 bug**: earlier code accepted `VERIFIED` as `ActionStatus` and silently rejected every real result, making the resolver always return `None`. Fix is in current code. Uses `InMemoryTargetContextStore`. UWP records have `metadata["process_names"]` for real exes.
8. `uwp_source.py` (359 lines): `UWPSource`. Enumerates Microsoft Store / UWP via PowerShell `Get-StartApps` + `Get-AppxPackageManifest`. AUMID regex captures `^([A-Za-z0-9.\-]+)_([a-z0-9]+)!(.+)`. Records have synthetic `executable="<name>.uwp"` placeholder; real exes in `metadata["process_names"]`. Launch command: `explorer.exe shell:AppsFolder\\<AppID>`. Cached per unique PFN.

**Architectural observations:**
- The resolver is *stateless*; the catalog is *stateful*; the service is *stateful + has side effects*. This layering is clean.
- `GENERIC_ALIASES` is a hard-coded table in source. New aliases require a code change. This is intentional (per design comment) — aliases are curated, not auto-learned.
- UWP and Registry launch paths are NOT closed-capability: UWP uses `explorer.exe shell:AppsFolder\\<AppID>`, Registry uses `shell=True`. These are documented but constitute a real R-21 boundary leak.

---

## PART 8 — VISION

**Public API** (`vision/api.py`, 873 lines) — 8 functions:
1. `observe()` — capture screen + return `GroundedElement` for the focused region.
2. `describe()` — capture screen + return textual description (uses OCR + Visual).
3. `find(query)` — find element by query string (delegates to PerceptionRouter).
4. `locate(target)` — locate a specific known target (returns bbox + status).
5. `is_visible(target)` — boolean (uses `GroundedElement.status == OBSERVED`).
6. `is_focused(target)` — boolean (combines with ForegroundWindowReader).
7. `wait_for(target, timeout)` — polls until observed + stable.
8. `verify(action, expected_state)` — post-action verification; returns `VerificationVerdict` (not raises).

**Rules enforced by the API:**
- R-8: "no claimed verification" — we never claim VERIFIED based on a single observation; verification requires a stable, multi-monitor-checked, status-positive element to be present *and* an absence of the negative signals.
- R-14: vision is a *service*, not a singleton. `set_default_router()` and `set_default_provider()` for injection.
- R-21: closed capability seam via `ScreenshotProvider`.
- R-22: deterministic routing (no LLM in the perception path).

**`GroundedElement`** (`vision/grounded_element.py`, 588 lines):
- Frozen dataclass (R-10).
- 11-value `GroundedElementStatus`: `OBSERVED`, `TARGET_NOT_FOUND`, `LOW_CONFIDENCE`, `MULTIPLE_TARGETS`, `WINDOW_NOT_VISIBLE`, `WINDOW_NOT_FOCUSED`, `UI_NOT_READY`, `SCREEN_UNSTABLE`, `OCR_FAILED`, `ACCESSIBILITY_UNAVAILABLE`, `TIMEOUT`, `TARGET_CHANGED`.
- `KNOWN_ELEMENT_TYPES`: button/link/edit/text/image/checkbox/radio/combobox/menu_item/tab/icon/unknown.
- `KNOWN_SOURCES`: uia/ocr/derived/vision/screen.
- Sentinel builders: `not_found()`, `low_confidence()`, `ambiguous()`.
- Confidence clamped to [0, 1]. bbox normalized (left<right, top<bottom). center recomputed from bbox.

**`PerceptionRouter`** (in `vision/router/`): selects between UIA / Coordinates / OCR / Visual strategies based on query and reliability history.

**`recovery.py`** (267 lines): `retry_with_strategy()` (pure helper), `reobserve_and_compare()` (IoU-based movement detection, `iou_threshold=0.7`), `from_candidates()` adapter.

**Architectural observations:**
- Vision returns *typed* results with explicit status; it does not raise on negative outcomes. This is a strong design choice.
- The 8 functions are *pure observability* — they do not execute actions, only report state.
- Vision safety policy is in `vision/safety/` (architectural boundary: vision must never be used to *find* a button to click and *click* it in one call; the action is always a separate capability call).

---

## PART 9 — INPUT (BRIEF)

**`system/input/input_service.py`**: `WindowsInputService` — Win32 SendInput wrapper.

**Capabilities in `core/capabilities/`:**
- `desktop_keyboard.py`: `KeyboardTypeCapability`, `KeyboardPressCapability`, `KeyboardHotkeyCapability`.
- `desktop_mouse.py`: `MouseMoveCapability`, `MouseClickCapability`, `MouseDoubleClickCapability`, `MouseRightClickCapability`, `MouseScrollCapability`, `MouseDragCapability`.
- `desktop_observation.py`: `ScreenSizeCapability`, `ForegroundWindowCapability`, `ScreenshotCapability`.

All input capabilities go through the standard 5-step validation pipeline. Safety layer in the router checks for forbidden shell tokens and rate limits per session.

**Note**: The `InputService` is also used by `voice/runtime.py` for wake-word suppression (the mic is muted while a non-voice input is being processed, per System 9).

---

## PART 10 — BROWSER

**Architecture:**
- Top-level `browser/` package is a thin facade re-exporting `BrowserService` and `BrowserSession`.
- Actual Playwright logic lives in `core/services/browser_service.py`.
- 16 capability implementations in `core/capabilities/browser_capabilities.py`: Navigate, Click, Type, ExtractText, Open, Close, Back, Forward, Reload, Press, Scroll, Hover, Select, Wait, ExtractPage, Download.

**Architectural observations:**
- The browser is accessed only through the capability seam (R-21) — the agent never imports Playwright directly.
- `BrowserService` is stateful (holds Playwright session). Lifecycle: initialize → use → shutdown.
- The deterministic planner has specific rules for `BROWSER_NAVIGATE`, `BROWSER_CLICK_TARGET`, `BROWSER_TYPE_TARGET`, `BROWSER_EXTRACT_TEXT` that require explicit intent params and refuse to invent missing values (e.g., will not generate a CSS selector for a click target that doesn't specify one).

---

## PART 11 — AGENT (CLOSED LOOP)

**`core/orchestration/agent.py`** (~82KB) is the largest file in the codebase. The `Agent` class is a state machine:

```
RECEIVING_GOAL
   ↓
PLANNING          (uses pre-built plan from Brain, or LLM-plans if missing)
   ↓
PLAN_READY
   ↓
EXECUTING         (one step at a time; awaits observation)
   ↓
OBSERVING         (captures observation via plan's expected_observation source)
   ↓
EVALUATING        (runs Verifier on (observation, expected_effect))
   ↓
DECIDING          (continues, recovers, replans, or terminates)
   ↓
   ├── CONTINUE → EXECUTING (next step)
   ├── RECOVER → back to EXECUTING (with recovery_action applied)
   ├── REPLAN → PLANNING (with attempt budget)
   └── terminal → COMPLETE | FAILED | CANCELLED | TIMEOUT | CLARIFICATION_REQUIRED
```

**`AgentResult`**: frozen dataclass with `agent_run_id`, `final_state` (AgentState), `plan_count`, `attempts`, `replans`, `clarifying_question`, `error`, `metadata`.

**`AgentPolicy`**: small dataclass that configures max attempts, replan budget, etc. (per `core/orchestration/agent.py`).

**`RecoveryEngine`** (`core/orchestration/recovery.py`):
- Bounded retries (no infinite loops).
- `RecoveryClassifier` (Phase 17) decides whether failure is `RETRY`/`RETRY_WITH_BACKOFF`/`SKIP`/`REPLAN`/`ABORT`/`ASK_USER`/`GIVE_UP`.
- `RecoveryDecision` (frozen) can carry a `new_step` for one-step fixes (e.g., "click first then re-screenshot").

**`PlanExecutor`** (`core/orchestration/plan_executor.py`):
- Iterates steps; for each, dispatches via `CapabilityRouter`, awaits observation, runs verifier, returns `ExecutionResult` (frozen).
- Catches `CancellationRequested` (R-4) and converts to terminal state.
- Idempotency check via `IdempotencyViolation` per step.
- Preconditions check via `core/orchestration/preconditions.py`.

**`StepVerifier` + `GoalVerifier`** (`core/orchestration/verifier.py`):
- Step-level: validates single observation against `ExpectedEffect` (returns tri-state `VerificationVerdict`).
- Goal-level: validates *all* observations against *all* `ExpectedEffect`s for the goal.
- `VerifierRouter` chooses step vs goal per step.

**Architectural observations:**
- The agent is the *only* place that can mutate world state (via capabilities). Brain cannot.
- The agent respects the `cancellation_token` passed by the pipeline (Phase 4 contract).
- The agent's state machine has a fixed set of terminal states; there is no implicit "success" — the agent reaches a terminal state via explicit DECIDING logic.

---

## PART 12 — VERIFICATION

**`StepVerifier`**: validates a single `(observation, expected_effect)` pair.

**`GoalVerifier`**: validates all `(observation, expected_effect)` pairs for a goal.

**`VerificationVerdict`** (frozen, in `core/orchestration/models.py`):
- Tri-state: `passed` / `failed` / `uncertain`. Exactly one must be true.
- `uncertain` is the conservative fallback when the verifier cannot reach a conclusion (e.g., observation source was unavailable).

**`VerifierRouter`** (`core/orchestration/verifier_router.py`): selects `StepVerifier` vs `GoalVerifier` per step. Goal-level is run at the end of each `run_goal` cycle to ensure the *whole* goal is satisfied, not just the last step.

**Recovery interaction**: When `uncertain` is returned, the `RecoveryClassifier` typically emits `REPLAN` (re-plan with new observations). When `failed` is returned with a known `FailureKind`, the classifier can emit a more specific `RecoveryAction` (e.g., `RETRY` for `EXECUTION` failure, `ABORT` for `SAFETY` failure).

**Asymmetric `expected_effect` requirement**: The deterministic planner's app rules force `expected_effect` (Phase 14.1) so the verifier has a check; non-app rules do not. This is intentional but creates the asymmetry noted in PART 25.

---

## PART 13 — VOICE

**`VoiceService`** (`voice/service.py`, 215 lines):
- Glues microphone, VAD, STT, TTS.
- Methods: `initialize()`, `listen_and_transcribe()`, `speak(text)`, `listen_process_respond()` (one full voice turn: listen → engine.process → speak), `run_voice_loop(max_turns)`, `shutdown()`.

**`VoiceRuntime`** (`voice/runtime.py`, 575 lines, Part 3 / System 9):
- Owns microphone, wake-word listener, command STT loop.
- Default wake phrase = `"omnix"`.
- `GOING_TO_SLEEP_TEXT`, `AWAKE_TEXT` constants.
- Subsystem toggling based on `RuntimeState` (mic on/off during processing — System 9 gate).
- `start_listen_loop()` runs background thread.
- `_on_command` callback invoked with transcribed text.
- `set_engine(engine)` to attach engine for `is_processing` gate.
- `sleep()` and `wake()` are idempotent.
- Attaches to `RuntimeStateController`.

**Contracts** (`voice/contracts.py`, 68 lines):
- `VoiceState` enum: `IDLE`/`LISTENING`/`TRANSCRIBING`/`PROCESSING`/`SPEAKING`/`STOPPING`/`ERROR`.
- `AudioFormat`, `AudioChunk`, `TranscriptionResult`, `SpeechSegment`, `TTSRequest`, `TTSResult`.
- `VoiceError` + 4 subclasses.

**Per-stage implementations**:
- `voice/audio/`: `MicrophoneInput` (sounddevice).
- `voice/vad/`: `SimpleVAD` (energy-based).
- `voice/stt/`: `FasterWhisperProvider` (faster-whisper).
- `voice/tts/`: `SAPITTSProvider` (Windows SAPI via pywin32).
- `voice/wake/`: wake-word listener (keyword spotting).
- `voice/session/`: voice session manager.
- `voice/policy.py`, `voice/progress_bridge.py`, `voice/startup_announcer.py`: supporting.

**Hard rules per Phase 11:**
- Voice NEVER calls Brain/OpenRouter directly. Only captures audio, transcribes, hands text to `engine.process()`, speaks response.
- TTS NEVER receives raw internal objects (only `str`).

**`SpeechQueue`** (`core/services/speech_queue.py`): priority, dedup, bypass. Allows the engine to enqueue speech without blocking on TTS latency.

**Architectural observations:**
- Voice is the *outermost* layer. The voice runtime is the only place that listens to the mic; the rest of the system does not know about audio.
- Wake-word + System 9 gate prevents feedback loops: while the engine is processing, the mic is muted.

---

## PART 14 — MEMORY (BRIEF)

**`MemoryService`** (`core/services/memory_service.py`): interface.
**`SQLiteMemoryStore`** (`core/services/sqlite_memory_store.py`): persistent impl.
**`InMemoryStore`**: in-process impl (for tests).

**Usage in pipeline**: `RequestPipeline.process()` calls `memory_service.recall(query=str(text)[:200], limit=3)` and stores only a *count* in `context_snapshot["memory_hit_count"]` — never the raw memory items. This is an explicit privacy boundary: memory content is internal-only and never echoed to TTS / CLI.

**Architectural observations:**
- Memory is read-only from the agent's perspective. The agent does not write to memory during a run.
- The "memory write" path is a separate operation (not yet traced in this audit; appears to be a post-run hook).

---

## PART 15 — CONTEXT (BRIEF)

**`core/state/context_service.py`**: `ContextService` — exposes per-request context.
**`core/state/contexts.py`**: typed context dataclasses (e.g., `UserContext`, `WindowContext`).
**`core/state/domain.py`**: domain entities.
**`core/state/runtime_state.py`**: `RuntimeStateController` (R-17: single source of truth for the engine's runtime state, including voice subsystem toggling).
**`core/state/inactivity_timer.py`**: `InactivityTimer` (Phase 17: tracks idle time, used for sleep/wake).

**`RuntimeState`**: the engine has a single state machine: `STARTING` → `READY` → `RUNNING` → `STOPPING` → `STOPPED`. Transitions are published as `EngineEvent` records on the bus.

---

## PART 16 — SERVICE REGISTRY

**`ServiceRegistry`** (`core/service_registry.py`, 424 lines):
- Typed service locator.
- Kahn's algorithm topological sort for dependency ordering.
- RLock-protected (`threading.RLock`) — registration is racy without the lock.
- Service classification: `critical` (must init before ready), `background` (may init in parallel), `on_demand` (lazy).
- Per-record `priority` (higher initializes first within the same depth in the DAG).
- `initialize_all()` walks the DAG in dependency order, calls `service.initialize()`, publishes `ServiceEvent(transition="initialized")`.
- `shutdown_all()` walks in reverse order.
- Cycle detection: `DependencyError` raised if the DAG has a cycle.
- Health surface: `registry.statistics()` aggregates per-service `statistics()`.

**Architectural observations:**
- The registry is *not* a global singleton — the engine instantiates one and passes it via DI.
- The registry is *not* a DI container — it does not instantiate services. Subsystems are constructed by the engine.
- Resolution: `registry.resolve("name")` returns the live service instance.

---

## PART 17 — CAPABILITY SYSTEM

**`Capability` protocol** (in `core/capability_registry.py`): name, parameter spec, execute method, safety metadata.

**`CapabilityRegistry`**: holds the dict of `name → Capability` and exposes `register`, `resolve`, `list_all`.

**`CapabilityRouter`** (`core/capability_router.py`, 328 lines) — 5-step validation pipeline:
1. **Existence**: is the capability registered?
2. **Parameters**: do the supplied parameters match the spec (type, required, format)?
3. **Availability**: is the underlying service ready (e.g., is the window open for a window-only capability)?
4. **Safety**: is this capability allowed in the current safety context? (e.g., is `file.delete` authorized?)
5. **Dispatch**: actually invoke `capability.execute(params)`.

Each step can return a `CapabilityResult(status=REJECTED|FAILED|VERIFIED, error=...)` or pass to the next.

**Standard capabilities** (`core/capabilities/__init__.py`, 238 lines, `register_standard_capabilities()`):
- Filesystem: read, write, create, folder create, delete, directory list.
- Process: run command, is running.
- Desktop observation: screen size, foreground window, screenshot.
- Desktop mouse: move, click, double-click, right-click, scroll, drag.
- Desktop keyboard: type, press, hotkey.
- Desktop application: open, close, focus, is running.
- Desktop window: list, focus, minimize, maximize, restore, close.
- Browser: 16 capabilities (navigate, click, type, extract, back, forward, reload, press, scroll, hover, select, wait, extract page, download, open, close).

**Default services**: `core/capabilities/__init__.py` constructs default `ApplicationService`, `WindowService`, `InputService` if not injected via DI.

**Architectural observations:**
- The capability seam is the *only* place where world state is mutated. The agent never calls into OS APIs directly.
- The 5-step validation is the *only* place where safety checks live. There is no separate "safety layer" that could be bypassed.
- `FileDeleteCapability` is in the standard set but requires an explicit safety authorization at the router step.

---

## PART 18 — DATA CONTRACTS (SUMMARY)

All major data contracts live in `core/orchestration/models.py` and are `frozen=True` dataclasses (R-10):

| Contract | Frozen | Forbidden tokens check | Notes |
|---|---|---|---|
| `Goal` | yes | no | description, success_criteria |
| `Intent` | yes | no | kind, confidence, parameters, source_text |
| `ActionRequest` | yes | yes (R-21) | plan_id, step_id, timeout_s, safety_metadata, correlation_id |
| `Plan` | yes | no | steps, parent_plan_id for replans |
| `PlanStep` | yes | yes (R-21) | max_retries *deprecated* (Phase 1/D6) |
| `ExecutionContext` | yes | no | cancellation_token, progress |
| `Observation` | yes | no | source, data, timestamp |
| `ExpectedEffect` | yes | no | check_name, expected, timeout_s |
| `Verifier` | (Protocol) | — | method `verify(observation, expected_effect) -> VerificationVerdict` |
| `VerificationVerdict` | yes | no | passed/failed/uncertain (exactly one) |
| `Failure` | yes | no | not an exception — data |
| `RecoveryDecision` | yes | no | action, new_step for one-step fixes |
| `AgentResult` | yes | no | agent_run_id, final_state, plan_count, attempts, replans |
| `OmnixResponse` | yes | no | text, status, agent_state, correlation_id, duration_ms, metadata, error |

**Key enums**:
- `IntentKind`: dialogue (INFORM/QUERY/COMMAND/CLARIFY/CANCEL/UNKNOWN) + action (OPEN_APPLICATION/CLOSE_APPLICATION/FOCUS_APPLICATION/CONTROL_APPLICATION/FILE_FIND/FILE_MOVE/FILE_COPY/FILE_DELETE/WINDOW_MANAGE/QUERY_STATUS/CANCEL_TASK/NO_OP/COMPOUND_REQUEST) + UI (UI_CLICK_TARGET/UI_DOUBLE_CLICK_TARGET/UI_RIGHT_CLICK_TARGET) + browser (BROWSER_NAVIGATE/BROWSER_CLICK_TARGET/BROWSER_TYPE_TARGET/BROWSER_EXTRACT_TEXT).
- `ActionKind`: CAPABILITY_CALL/OBSERVE/VERIFY/WAIT/ASK_USER.
- `ObservationSource`: SCREEN/UIA/DOM/OCR/VISION/CLIPBOARD/PROCESS/FILESYSTEM/WORLD/USER/DERIVED.
- `FailureKind`: EXECUTION/VERIFICATION/TIMEOUT/CANCELLED/SAFETY/UNKNOWN_CAPABILITY/INVALID_PARAMETERS/PLAN_INFEASIBLE/INTERNAL + 6 UI failure kinds (TARGET_NOT_FOUND, FOCUS_FAILED, WINDOW_NOT_READY, STALE_TARGET, PROVIDER_FAILURE, PERMISSION_FAILURE).
- `RecoveryAction`: RETRY/RETRY_WITH_BACKOFF/SKIP/REPLAN/ABORT/ASK_USER/GIVE_UP.
- `AgentState`: RECEIVING_GOAL/PLANNING/PLAN_READY/EXECUTING/OBSERVING/EVALUATING/DECIDING/COMPLETE/FAILED/CANCELLED/TIMEOUT/CLARIFICATION_REQUIRED.

**R-21 (closed-shell) enforcement**:
- `_FORBIDDEN_SHELL_TOKENS` regex in `core/orchestration/models.py` is checked at `ActionRequest` and `PlanStep` construction.
- Catches tokens like `;`, `&&`, `|`, `$()`, backticks, etc.
- **Bypassed by**: `WindowsApplicationService.launch()` (`subprocess.Popen(flags, shell=True)`), `UWPSource` launch command (`explorer.exe shell:AppsFolder\\<AppID>`), `RunCommandCapability` (intentional — runs shell commands as a capability).

---

## PART 19 — EVENT ARCHITECTURE

**`EventBus`** (`core/events/event_bus.py`): publish/subscribe with typed events.

**Base hierarchy** (in `core/events/event_types.py`):
- `Event` (frozen, base)
  - `EngineEvent`: engine lifecycle (`booted`/`ready`/`running`/`stopping`/`stopped`).
  - `ServiceEvent`: service registry (`registered`/`initialized`/`failed`/`shutdown`).
  - `CapabilityEvent`: capability router (`attempted`/`executed`/`verified`/`failed`/`rejected`).
  - `TaskEvent`: task lifecycle.
  - `WorldEvent`: world state changes (window, app, screen).
  - `ConversationEvent`: user/engine turns.
  - `ErrorEvent`: recoverable errors.
  - `HealthEvent`: health changes.
  - `RequestEvent` (pipeline): `REQUEST_INTENT_RESOLVED`/`REQUEST_PLAN_CREATED`/`REQUEST_EXECUTION_STARTED`/`REQUEST_OBSERVATION_CAPTURED`/`REQUEST_VERIFICATION_COMPLETED`/`REQUEST_RECOVERY_STARTED`/`REQUEST_REPLAN_STARTED`/`REQUEST_CANCELLED`/`REQUEST_TIMED_OUT`/`REQUEST_REJECTED`/`REQUEST_COMPLETED`.

**Wildcard subscriptions**: bus supports `name.startswith("capability.")` style filters.

**Architectural observations:**
- Events are *facts about the world*, not commands (per the design comment in `event_types.py`).
- Events are emitted at every pipeline stage (see `RequestPipeline._publish()` in `core/pipeline.py`).
- Subsystems subscribe to events but do not call each other through events — the event bus is an observability surface, not a control surface.

---

## PART 20 — HARD-CODING AUDIT

Hard-coded constants discovered during the audit:

| Location | Constant | Type | Notes |
|---|---|---|---|
| `core/pipeline.py` | `_MAX_USER_TEXT_LEN = 2000` | magic number | user text cap |
| `core/pipeline.py` | forbidden tokens: `("api_key=", "sk-", "password=", "token=", "bearer ")` | string list | TTS redaction |
| `system/application/resolver.py` | `GENERIC_ALIASES` (8 entries) | string→string | app alias map |
| `system/application/resolver.py` | `AMBIGUITY_CONFIDENCE_GAP = 0.15` | float | resolver threshold |
| `system/application/discovery.py` | per-source confidence (0.5/0.7/0.9/0.95/1.0) and timeout (2s/4s/4s/4s/8s) | floats | source config |
| `ai/brain/deterministic.py` | `_DEFAULT_RULES` | dict | planner rule table |
| `ai/brain/router.py` | `_CONVERSATIONAL_TRIGGERS` (21), `_LOCAL_VERBS` (47), `_GENERATIVE_VERBS` | string lists | routing decision |
| `ai/brain/system2.py` | `max_task_steps = 64` | int | safety bound |
| `vision/recovery.py` | `_RELIABILITY_RANK = ("uia", "derived", "ocr", "vision", "screen")` | tuple | strategy ranking |
| `vision/recovery.py` | `iou_threshold = 0.7` | float | movement detection |
| `voice/runtime.py` | `GOING_TO_SLEEP_TEXT`, `AWAKE_TEXT` | strings | TTS phrases |
| `voice/runtime.py` | wake phrase = `"omnix"` | string | wake word |
| `core/orchestration/models.py` | `_FORBIDDEN_SHELL_TOKENS` | regex | R-21 enforcement |
| Various | `timeouts` per capability (e.g., 8s for RegistryUninstall) | floats | per-source |

**Architectural observations:**
- Most hard-coded constants are *intentional* (e.g., aliases are curated, forbidden tokens are a security boundary).
- The planner's `_DEFAULT_RULES` is the largest hard-coded table (~30+ rules). New intents require a code change.
- The router's verb tables are hard-coded; new verbs require a code change.
- No constants are duplicated across files (good — single source of truth).

---

## PART 21 — DUPLICATION AUDIT

No major duplicated functionality was found. Specifically:

- **App resolution** is centralized in `ApplicationResolver` (no duplicate resolvers in capabilities or services).
- **Forbidden-token checks** live in `core/orchestration/models.py` and are not duplicated elsewhere.
- **Intent/Goal/Plan construction** is centralized in `Brain.handle_text()`; the agent does not re-interpret user text.
- **Cancellation token handling** is centralized in `RequestPipeline` (3 seams) and `Agent` (one consumer).
- **Vision status enum** is centralized in `GroundedElementStatus`; not duplicated in any strategy.
- **Capability event emission** is centralized in `CapabilityRouter`; capabilities do not emit their own events.

**Minor near-duplication** (not a defect, just observed):
- `is_running` exists as both a capability (`ApplicationIsRunningCapability`) and a method on `WindowsApplicationService` (`is_running`). This is intentional: the service is the underlying state, the capability is the dispatchable action.
- `open_app` and `launch_app` are used interchangeably in different files; both map to `ApplicationOpenCapability`.

---

## PART 22 — DEAD / UNUSED CODE AUDIT

Working tree has many modified files (Phases 16–17) and many untracked files. Some of the untracked files may be incomplete or dead-on-arrival. Per the read-only constraint, this audit does not delete anything, but flags:

**Likely dead / unfinished** (based on untracked status in `git status`):
- `ai/brain/llm_tracking.py` — only present in working tree; check whether `LLMCallTracker` is wired up in `System2Brain`. Per `system2.py` read, it *is* imported, so this is live.
- `ai/brain/narration.py` — possibly orphaned; needs verification.
- `core/orchestration/dag.py` — used by `PlanExecutor`? Per the imports in `__init__.py`, yes.
- `core/orchestration/verifier_router.py` — used by `Agent`? Per the imports, yes.
- `core/orchestration/cancellation.py` — used by `RequestPipeline`? Per the imports in `pipeline.py`, yes.
- `core/orchestration/progress.py` — wired in via `ExecutionContext.progress` property.
- `core/orchestration/failure_classifier.py` — used by `RecoveryEngine`? Per the imports, yes.
- `core/orchestration/retry.py` — used by `RecoveryEngine`? Per the imports, yes.

**Possibly dead** (cannot confirm without deeper read):
- `core/capabilities/_dispatch.py` — helper, may or may not be in use.
- `core/orchestration/scroll.py` — possibly pre-Phase-14 leftover; should be checked against `PlanExecutor` to confirm.
- `core/orchestration/interfaces.py` — Protocol definitions; may be partially orphaned if concrete classes were moved to `models.py`.
- `voice/policy.py` — needs verification.
- `voice/progress_bridge.py` — needs verification.
- `voice/startup_announcer.py` — needs verification.

**Note**: The `scripts/` directory contains `debug_happy.py`, `phase16_real_windows_smoke.py`, `phase17_smoke.py`, `probe_apps.py`, `probe_calc.py` — these are smoke/probe scripts, not production code. They are not dead per se but are not part of the runtime.

---

## PART 23 — ARCHITECTURAL CONNECTION MATRIX

Direct call dependencies (read from source imports + visible method calls):

| From → To | Engine | Brain | Agent | Caps | App | Vision | Voice | Bus | Registry |
|---|---|---|---|---|---|---|---|---|---|
| **Engine** | — | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| **Brain** | × | — | × | × (read-only spec only) | × | × | × | × | ✓ (via DI) |
| **Agent** | × | × | — | ✓ | (via cap) | (via cap) | × | ✓ | ✓ (via DI) |
| **CapabilityRouter** | × | × | × | — | (via DI) | (via DI) | × | ✓ | ✓ (via DI) |
| **ApplicationService** | × | × | × | ✓ | — | × | × | × | × |
| **Vision API** | × | × | × | × | × | — | × | × | × |
| **Voice** | × | × | × | × | × | × | — | × | × |

**Reading the matrix:**
- The Engine is the only node with full visibility.
- Brain is genuinely *read-only* — it talks to the registry (to resolve services like `AIEscalationGate`) but never dispatches a capability.
- Vision and Voice are *leaves* — they expose APIs but do not depend on other subsystems.
- The Agent is the *only* mutation path: Agent → CapabilityRouter → Capability → Service.
- Cross-subsystem state-sharing goes through the EventBus.

**Circular dependencies**: None observed. The DAG is clean: Engine → (Brain, Agent, Subsystems) → Capabilities.

---

## PART 24 — COMPLETE ARCHITECTURE DIAGRAM

```
                       ┌─────────────────────┐
                       │       USER          │
                       └──────────┬──────────┘
                                  │ (text/voice)
                                  ▼
                       ┌─────────────────────┐
                       │  Voice Runtime      │
                       │  (System 9 gate)    │
                       └──────────┬──────────┘
                                  │ transcribed text
                                  ▼
┌────────────────────────────────────────────────────────────────────────┐
│                            OMNIX ENGINE                                │
│  ┌──────────────────────────────────────────────────────────────────┐  │
│  │  RequestPipeline                                                 │  │
│  │  (cancellation seams × 3, fast-path, brain, agent)                │  │
│  └─────┬──────────────────────────┬─────────────────────────────────┘  │
│        │                          │                                    │
│        ▼                          ▼                                    │
│  ┌──────────────────┐      ┌──────────────────┐                        │
│  │  FastPath        │      │  Brain           │                        │
│  │  Dispatcher      │      │  (read-only)     │                        │
│  │  (Phase 15)      │      │                  │                        │
│  └─────┬────────────┘      │  ┌────────────┐  │                        │
│        │ verified          │  │ Interpreter│  │                        │
│        ▼                   │  │  (LLM)     │  │                        │
│  ┌──────────────────┐      │  └─────┬──────┘  │                        │
│  │ Application       │      │        ▼         │                        │
│  │ Service           │      │  ┌────────────┐  │                        │
│  │ (cap seam)        │      │  │Planner     │  │                        │
│  └──────────────────┘      │  │(deterministic│ │                        │
│                            │  │ + LLM)      │  │                        │
│                            │  └─────┬──────┘  │                        │
│                            │        ▼         │                        │
│                            │  BrainResult     │                        │
│                            │  (Goal, Plan,    │                        │
│                            │   Intent)        │                        │
│                            └────────┬─────────┘                        │
│                                     │                                  │
│                                     ▼                                  │
│                            ┌──────────────────┐                        │
│                            │  Agent           │                        │
│                            │  (closed loop)   │                        │
│                            │                  │                        │
│                            │  PLANNING        │                        │
│                            │  EXECUTING       │                        │
│                            │  OBSERVING       │                        │
│                            │  EVALUATING      │                        │
│                            │  DECIDING        │                        │
│                            │  → terminal      │                        │
│                            └─────┬────────────┘                        │
└──────────────────────────────────┼─────────────────────────────────────┘
                                   │
                                   ▼
                       ┌──────────────────────┐
                       │  Capability Router   │
                       │  5-step validation   │
                       └─────┬────────────────┘
                             │
        ┌────────────────────┼────────────────────┐
        ▼                    ▼                    ▼
┌───────────────┐   ┌───────────────┐   ┌───────────────┐
│ Filesystem    │   │ Process       │   │ Browser       │
│ (read/write/  │   │ (run/is_run)  │   │ (16 caps)     │
│  delete/...)  │   │               │   │               │
└───────────────┘   └───────────────┘   └───────────────┘
        │
        ▼
┌───────────────┐   ┌───────────────┐   ┌───────────────┐
│ Desktop       │   │ Desktop       │   │ Desktop       │
│ Application   │   │ Window        │   │ Input         │
│ (open/close/  │   │ (list/focus/  │   │ (mouse/key)   │
│  focus/is_run)│   │  min/max/...) │   │               │
└───────────────┘   └───────────────┘   └───────────────┘
        │
        ▼
┌───────────────────────────────────────────────────────┐
│                    SYSTEM LAYER                        │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌────────┐ │
│  │ App      │  │ Window   │  │ Input    │  │Process │ │
│  │ Service  │  │ Service  │  │ Service  │  │Service │ │
│  │ (R-21    │  │ (Win32)  │  │(Win32    │  │(psutil)│ │
│  │  leak)   │  │          │  │ SendInp) │  │        │ │
│  └──────────┘  └──────────┘  └──────────┘  └────────┘ │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐             │
│  │ Filesys  │  │ Clipbd   │  │ Memory   │             │
│  │ Service  │  │ Service  │  │ Service  │             │
│  └──────────┘  └──────────┘  └──────────┘             │
└───────────────────────────────────────────────────────┘

┌───────────────────────────────────────────────────────┐
│                  VISION SUBSYSTEM                     │
│  ┌────────────────────────────────────────────────┐   │
│  │  Public API: observe, describe, find, locate,  │   │
│  │  is_visible, is_focused, wait_for, verify      │   │
│  └─────────────────────┬──────────────────────────┘   │
│                        ▼                              │
│  ┌────────────────────────────────────────────────┐   │
│  │  Perception Router → UIA / OCR / Visual / Coords│  │
│  └────────────────────────────────────────────────┘   │
│  GroundedElement (11-value status enum)               │
└───────────────────────────────────────────────────────┘

┌───────────────────────────────────────────────────────┐
│                  VOICE SUBSYSTEM                      │
│  ┌────────────────────────────────────────────────┐   │
│  │  VoiceRuntime (System 9 gate) → Wake → STT →  │   │
│  │  hand to engine → TTS (via SpeechQueue)        │   │
│  └────────────────────────────────────────────────┘   │
│  VoiceState (IDLE/LISTENING/TRANSCRIBING/PROCESSING/  │
│  SPEAKING/STOPPING/ERROR)                             │
└───────────────────────────────────────────────────────┘

┌───────────────────────────────────────────────────────┐
│              CROSS-CUTTING CONCERNS                    │
│  • ServiceRegistry (typed service locator, Kahn sort) │
│  • EventBus (typed frozen events, R-11 seam)          │
│  • MemoryService (read-only from agent, write on hook)│
│  • RuntimeState (single source of truth, System 9)    │
│  • Readiness Report (gate before "ready")             │
│  • SpeechQueue (priority/dedup/bypass for TTS)        │
└───────────────────────────────────────────────────────┘
```

---

## PART 25 — REAL vs INTENDED ARCHITECTURE

**Intended (per design comments and prior phase reports):**
- Brain is the single source of truth for intent (Phase 5 design).
- All execution goes through the capability seam (R-21).
- The agent is the only mutation path.
- The system2 brain is additive, not replacement.
- Voice is fully decoupled from the brain.

**Real (what the code actually does):**
- Brain IS the single source of truth for intent. Confirmed.
- Most execution goes through the capability seam. **EXCEPTIONS**: `WindowsApplicationService.launch()` uses `subprocess.Popen(shell=True)` directly; `UWPSource` launch command is `explorer.exe shell:AppsFolder\\<AppID>`; `RunCommandCapability` (intentional — this capability exists to run shell commands).
- The agent IS the only mutation path (per `Brain` not having any dispatch logic). Confirmed.
- System2Brain IS additive — it does not modify existing Brain/Agent/Planner/Pipeline. Confirmed.
- Voice IS decoupled — it only calls `engine.process()`. Confirmed.

**Gaps between intent and reality:**
1. **R-21 boundary leak in app launch** (see PART 26 finding 1).
2. **Phase 15 TargetContextResolver had a silent bug** (documented in `target_context.py` code comment) where the resolver always returned `None` for real results. Fixed in current code, but the existence of the bug indicates the validation pathway for `TargetContext` was insufficient.
3. **Three layers deciding "do we call the LLM"** (RequestRouter, AIEscalationGate, IntentInterpreter) use overlapping vocabulary. They compose but the composition is not formally tested.
4. **Working tree has many uncommitted files** spanning multiple unfinished phases (16, 17). The architecture is mid-migration.

---

## PART 26 — CRITICAL ARCHITECTURAL FINDINGS (RANKED)

### P0 — Must address before Phase 18

**F1. R-21 boundary leak in `WindowsApplicationService.launch()`**
- **File**: `system/application/app_service.py:launch()`
- **Defect**: Uses `subprocess.Popen(flags, shell=True)` directly, bypassing the capability seam. This contradicts the otherwise universal anti-shell stance and means `file.delete` safety checks, rate limits, and event emission do not apply to the most common app command ("open chrome").
- **Impact**: A user command "open <X>" can spawn arbitrary processes if `<X>` is misrouted. Safety layer cannot intercept.

**F2. UWP launch uses `explorer.exe shell:AppsFolder\\<AppID>`**
- **File**: `system/application/uwp_source.py`
- **Defect**: Same R-21 leak. Launch command is built as a shell string.
- **Impact**: UWP apps launch without going through the capability seam.

**F3. Asymmetric `expected_effect` enforcement**
- **File**: `ai/brain/deterministic.py`
- **Defect**: App rules force `expected_effect` (Phase 14.1), but non-app rules (e.g., file operations, generic control) do not. The goal verifier conservatively returns `UNCERTAIN` when no `expected_effect` is present, triggering a replan, which can cascade to `CLARIFICATION_REQUIRED` even on success.
- **Impact**: Inconsistent verifier behavior; non-app goals more likely to need replan.

### P1 — Should address in Phase 18

**F4. Phase 15 TargetContextResolver bug history**
- **File**: `system/application/target_context.py`
- **Defect**: Documented in code comment: "Phase 15 earlier code accidentally accepted [VERIFIED as ActionStatus] and silently rejected every real result, which made the resolver always return None." Fixed in current code, but the existence of the bug indicates insufficient validation.
- **Impact**: Risk of regression; no regression test visible in the audit.

**F5. Three layers deciding LLM call**
- **Files**: `ai/brain/router.py` (RequestRouter), `core/services/ai_escalation_gate.py` (AIEscalationGate), `ai/intent/interpreter.py` (IntentInterpreter)
- **Defect**: Overlapping vocabulary ("escalate", "hybrid", "uncertain"). No formal test that the three layers agree on a given input.
- **Impact**: Inconsistent LLM call behavior depending on which layer wins the decision race.

**F6. Working tree has many uncommitted modifications**
- **Files**: ~50+ modified, ~30+ untracked
- **Defect**: Architecture is mid-migration (Phases 16, 17). Audit reflects the state on disk, not the last clean commit (`7518673 phase 15`).
- **Impact**: Any code review or merge will conflict.

### P2 — Nice to address

**F7. `GENERIC_ALIASES` is hard-coded in source**
- **File**: `system/application/resolver.py`
- **Defect**: New aliases require a code change. No mechanism to add aliases at runtime.
- **Impact**: Limited extensibility.

**F8. `_DEFAULT_RULES` planner table is large and hard-coded**
- **File**: `ai/brain/deterministic.py`
- **Defect**: ~30+ rules. New intents require a code change.
- **Impact**: Brittle to intent vocabulary changes.

**F9. Dead code candidates** (see PART 22)
- **Files**: `voice/policy.py`, `voice/progress_bridge.py`, `voice/startup_announcer.py`, `core/orchestration/scroll.py`, `core/orchestration/interfaces.py`, `ai/brain/narration.py`
- **Defect**: Cannot confirm liveness without deeper read.
- **Impact**: Maintenance burden; possible stale code.

### P3 — Documentation / minor

**F10. R-21 enforcement is not in a single place**
- Forbidden-token regex lives in `core/orchestration/models.py`. The capability seam is in `core/capability_router.py`. The launch bypass is in `system/application/app_service.py`. The R-21 rule is not enforced by a linter; it's a code-review convention.
- **Impact**: Future contributors can introduce new R-21 leaks.

**F11. `narration.py` is a single large file (~900 lines)**
- **File**: `ai/brain/narration.py`
- **Defect**: Single file containing narration, validation, and LLM-tracking helpers.
- **Impact**: Maintainability.

---

## PART 27 — NO FIX PLAN (PER INSTRUCTION)

Per the audit specification ("If you discover a bug, DO NOT FIX IT. Document it. If you discover duplicated functionality, DO NOT REMOVE IT. Document it. If you discover an architectural conflict, DO NOT RESOLVE IT. Document it. If you discover dead code, DO NOT DELETE IT. Document it."), no fix plan is provided in this report. All findings are documented for the next phase to address.

---

## PART 28 — EXECUTIVE SUMMARY (REPRISE)

Omnix V6 is a structurally mature Windows AI agent runtime. The eight subsystems (engine, brain, agent, capabilities, application, vision, voice, services) are cleanly separated and connected through a typed service registry and event bus. The intent → goal → plan → execute → verify → recover → respond closed loop is real, end-to-end, and observable via typed events.

**Strengths:**
- Brain is genuinely read-only.
- Agent is the only mutation path.
- Capability seam is enforced with 5-step validation.
- Vision returns explicit status (no boolean claims).
- Voice is fully decoupled.
- Frozen dataclasses throughout (R-10).
- Cancellation tokens at three pipeline seams (R-4).
- Topological service initialization (no cycle risks).

**Weaknesses (see PART 26):**
- 3 R-21 boundary leaks (app launch via shell=True, UWP launch via explorer.exe, RunCommandCapability is intentional).
- Asymmetric expected_effect enforcement causes inconsistent verifier behavior.
- Three overlapping LLM-decision layers (RequestRouter, AIEscalationGate, IntentInterpreter).
- Working tree is mid-migration (Phases 16, 17 unfinished).
- Some hard-coded tables (aliases, planner rules, router verbs) limit extensibility.

**What this means for Phase 18:**
The architecture is *coherent at the macro level* and ready for the next phase. The boundary leaks (F1, F2) and asymmetric enforcement (F3) are the most likely sources of integration bugs. The three-layer LLM decision (F5) is the most likely source of behavioral inconsistency.

**Audit done. Truth established. No code modified. No fix plan produced.**
