# V6 Legacy Placeholder Map (Phase 4 â†’ Phase 5 Integration Hardening)

This document classifies every zero-byte / stub file in the V6 tree
that exists for backward-compatibility or migration-tracking purposes
only. **No logic lives in these files.** They are not imported by
V6 production code paths; they are kept on disk so that V5 file
references and external tooling that scan the tree do not break, and
so that the Phase 5 work has an obvious list of files to either
populate, replace, or remove.

> Rule: V6's NEW architecture is authoritative. V5 is READ-ONLY
> REFERENCE ONLY. Anything left over from V5 is either (a) replaced
> by a V6 module in `core/`, `system/`, or `core/orchestration/`, or
> (b) scheduled to be replaced in a later phase.

## Classification

| Class | Meaning | Action |
|-------|---------|--------|
| **REPLACED** | A V6 module in `core/`, `system/`, or `core/orchestration/` already implements the equivalent functionality. The placeholder is purely a "spelled out for V5" copy. | Delete after Phase 5 GA. |
| **DEFERRED** | Functionality that the V6 product vision needs, but has not yet been implemented. Held in reserve. | Populate in Phase 5+ (or remove if the vision is rescoped). |
| **MIGRATION-TRACK** | File exists solely so that V5 docstrings, ticket numbers, or external tests can still resolve the path. | Keep until V5 is fully decommissioned. |
| **DROPPED** | V5 functionality that is **not** part of the V6 product vision at all. | Remove before Phase 5 GA. |

## Per-directory inventory

### `core/agent/` â€” Agent runtime placeholders (10 files, all 0 bytes)

| File | Class | Notes |
|------|-------|-------|
| `core/agent/__init__.py` | MIGRATION-TRACK | V5 imported `core.agent.agent_controller` as the entry point. V6 routes through `core.omnix_engine.OmnixEngine`. Kept so V5 launchers can `import core.agent` without `ModuleNotFoundError`. |
| `core/agent/agent_controller.py` | REPLACED | V6 equivalent: `core.omnix_engine.OmnixEngine` (R-1 thin orchestrator). |
| `core/agent/goal_executor.py` | REPLACED | V6 equivalent: `core.orchestration.interfaces.PlanExecutor` (Phase 4 contract; concrete impl deferred to Phase 5). |
| `core/agent/goal_verifier.py` | REPLACED | V6 equivalent: `core.results.VerificationResult` + `core.orchestration.interfaces.Verifier` (Phase 4). |
| `core/agent/observation_loop.py` | REPLACED | V6 equivalent: `system.windows.window_service.WindowsWindowService` + per-capability `execute()` observation methods. |
| `core/agent/recovery_engine.py` | REPLACED | V6 equivalent: `core.orchestration.interfaces.RecoveryStrategy` (Phase 4 contract; concrete impl deferred). |
| `core/agent/retry_manager.py` | REPLACED | V6 equivalent: `core.utils.timers` + per-call `Engine.execute()` counters. |
| `core/agent/step_verifier.py` | REPLACED | V6 equivalent: `core.results.ActionResult` + `CapabilityResult.verified` flag (R-8 / AD-21). |
| `core/agent/wait_engine.py` | REPLACED | V6 equivalent: `core.utils.timers` (single canonical timer module). |
| `core/agent/workflow_planner.py` | REPLACED | V6 equivalent: `core.orchestration.interfaces.Planner` (Phase 4 contract; concrete impl deferred). |

### `core/planning/` â€” Planning placeholders (7 files, all 0 bytes)

| File | Class | Notes |
|------|-------|-------|
| `core/planning/__init__.py` | MIGRATION-TRACK | V5 imported `core.planning.task_planner`. V6 routing through orchestration interfaces. |
| `core/planning/command_processor.py` | REPLACED | V6 equivalent: `core.orchestration.interfaces.PlanExecutor` (Phase 4). |
| `core/planning/command_schema.py` | REPLACED | V6 equivalent: `core.capability.CapabilitySpec` + `core.orchestration.models.ActionRequest` (R-21 / AD-21). |
| `core/planning/execution_context.py` | REPLACED | V6 equivalent: `core.orchestration.models.ExecutionContext` (Phase 4, R-23). |
| `core/planning/intent_classifier.py` | DEFERRED | V6 has `core.orchestration.models.Intent` (the contract) but no LLM-side classifier. **Target: Phase 5.** |
| `core/planning/target_resolver.py` | DEFERRED | V6 has `core.state.context_service` but no UI-target resolver. **Target: Phase 5+.** |
| `core/planning/task_planner.py` | DEFERRED | V6 has `core.orchestration.interfaces.Planner` (contract) but no concrete LLM-backed planner. **Target: Phase 5.** |

### `core/events/event_dispatcher.py`, `core/events/event_subscriber.py` (2 files, 0 bytes)

| File | Class | Notes |
|------|-------|-------|
| `core/events/event_dispatcher.py` | REPLACED | V6 equivalent: `core.events.event_bus.EventBus.publish()`. The V5 dispatcher used a per-subscriber callback map; V6 uses topic-based pub/sub on the bus. |
| `core/events/event_subscriber.py` | REPLACED | V6 equivalent: `core.events.event_bus.EventBus.subscribe()`. |

### `automation/automation_engine.py` (1 file, 0 bytes)

| File | Class | Notes |
|------|-------|-------|
| `automation/automation_engine.py` | REPLACED | V6's automation concept is the **Brain** layer (Phase 5). For Phase 4, the canonical execution surface is `OmnixEngine.execute()` (R-21). |

### `context/` â€” Context placeholders (2 files, 0 bytes)

| File | Class | Notes |
|------|-------|-------|
| `context/context_manager.py` | REPLACED | V6 equivalent: `core.state.context_service.ContextService` (R-23, the five containers: identity, world, semantic, episodic, working). |
| `context/screen_context.py` | REPLACED | V6 equivalent: `system.windows.window_service.WindowsWindowService` + per-capability `desktop.*` observation. |

### `vision/` â€” Vision placeholders (25 .py files, all 0 bytes; 1 binary model file)

| File / Dir | Class | Notes |
|------------|-------|-------|
| `vision/__init__.py` | MIGRATION-TRACK | Reserved namespace for V6 Phase 5+ vision subsystem. |
| `vision/element_locator.py` | DEFERRED | V6's vision roadmap needs element localization. **Target: Phase 6+ (out of scope for Brain/Orchestration phases).** |
| `vision/screen_intelligence.py` | DEFERRED | Same as above. |
| `vision/screen_observer.py` | REPLACED | V6 equivalent: `system.windows.window_service` + per-capability `desktop.screenshot`. |
| `vision/screen_summary.py` | DEFERRED | **Target: Phase 6+.** |
| `vision/text_detector.py` | DEFERRED | **Target: Phase 6+.** |
| `vision/ui_detector.py` | DEFERRED | **Target: Phase 6+.** |
| `vision/vision_controller.py` | DEFERRED | **Target: Phase 6+.** |
| `vision/vision_manager.py` | DEPLACED | See vision_controller. |
| `vision/vision_pipeline.py` | DEFERRED | **Target: Phase 6+.** |
| `vision/detection/*.py` (6 files) | DEFERRED | YOLO/UIA detection subsystem. **Target: Phase 6+.** The `yolo11n.pt` model file is a real artifact but not yet wired up. |
| `vision/discovery/*.py` (2 files) | DEFERRED | UIA source discovery. **Target: Phase 6+.** |
| `vision/hierarchy/*.py` (2 files) | DEFERRED | UI hierarchy modeling. **Target: Phase 6+.** |
| `vision/models/*.py` (6 .py files) | DEFERRED | Data models for vision. **Target: Phase 6+.** |
| `vision/summary/*.py` (2 files) | DEFERRED | Screen-state summary models. **Target: Phase 6+.** |
| `vision/models/yolo11n.pt` | (binary) | Pre-trained YOLO model file. Will be wired up in Phase 6+ when the vision subsystem is built. |

### `voice/` â€” Voice subsystem (Phase 10 — COMPLETED)

**Status (post-Phase 10):** **POPULATED** — V6-native voice subsystem
implemented. Zero V5 voice code was copied. The 5 zero-byte placeholders
that previously lived in this directory have been **removed and replaced**
by the V6-native `voice/` package described in
`docs/V6_PHASE_10_VOICE_IMPLEMENTATION_REPORT.md`.

The original V6 plan deferred voice to a "final subsystem" phase. Phase 10
brought that forward. Voice is the **transport layer only**: it converts
audio → text and text → audio. It does **not** plan, does **not** access
LLM providers, and does **not** execute actions. All intelligence lives
in the existing `IntentInterpreter` / `Brain` / `Planner` / `Agent`
pipeline. Voice simply hands recognized text into `IntentInterpreter` and
speaks `Brain` responses back to the user.

| V6-Native module | Role |
|------------------|------|
| `voice/contracts.py` | Typed dataclasses: `VoiceState`, `AudioFormat`, `AudioChunk`, `TranscriptionResult`, `TTSRequest`, `TTSResult`, plus the `VoiceError` hierarchy. |
| `voice/audio/microphone.py` | `sounddevice`-backed `InputStream` with a callback → generator bridge. Yields `AudioChunk` objects. No business logic. |
| `voice/vad/detector.py` | `SimpleVAD` — RMS energy-based Voice Activity Detection. No WebRTC dependency. |
| `voice/stt/provider.py` | `SpeechToTextProvider` Protocol. |
| `voice/stt/faster_whisper_provider.py` | `FasterWhisperSTTProvider` — lazy-loaded `faster-whisper` (tiny.en, float16, CUDA) for fully local STT. |
| `voice/tts/provider.py` | `TextToSpeechProvider` Protocol. |
| `voice/tts/sapi_provider.py` | `SapiTTSProvider` — Windows SAPI via `win32com.client.Dispatch("SAPI.SpVoice")`. 100% offline. Uses `threading.RLock()` to avoid the close→stop deadlock. |
| `voice/session/voice_session.py` | `VoiceSession` state machine (IDLE / LISTENING / TRANSCRIBING / PROCESSING / SPEAKING / STOPPING / ERROR) with strict transition validation. |
| `voice/policy.py` | `sanitize_for_tts()` blocks `api_key`, `sk-`, `password`, `token=` from being spoken. `condense_response()` truncates long responses. |
| `voice/service.py` | `VoiceService` — canonical boundary. `listen_and_transcribe()` and `speak()`. No LLM, no planning, no action. |
| `tests/test_voice.py` | 8 deterministic tests (all use mock providers; no real audio hardware). |
| `main.py` patches | New `python main.py voice {test-stt,test-tts,listen}` subcommands. |

> **V5 carryover:** None. The 5 original zero-byte files
> (`audio_utils.py`, `speech_recognizer.py`, `tts_engine.py`,
> `voice_manager.py`, `wake_listener.py`) are gone. A `grep -r "V5"`
> against `voice/` returns nothing. The local-only STT choice is
> `faster-whisper` (not V5's `Vosk`); the local-only TTS choice is
> Windows SAPI (not V5's `edge-tts`/`pyttsx3`).

### Other zero-byte placeholders found

| File | Class | Notes |
|------|-------|-------|
| `ai/brain_manager.py` | DEFERRED | The `ai/` namespace is reserved for the Phase 5 Brain layer. The module shell exists so the package can be imported. |
| `memory/behavior_memory.py` | DEFERRED | The `memory/` namespace is reserved for V6's memory subsystem. **Target: Phase 5+.** |
| `memory/memory_coordinator.py` | DEFERRED | Same. |
| `memory/memory_manager.py` | DEFERRED | Same. |
| `memory/ui_pattern_memory.py` | DEFERRED | Same. |
| `memory/behavior_store.json` | (json) | Empty memory store. Will be populated in Phase 5+. |
| `memory/ui_patterns.json` | (json) | Empty memory store. Will be populated in Phase 5+. |
| `skills/manager/__init__.py` | DEFERRED | Skills manager for Phase 5+. |
| `skills/manager/skill_loader.py` | DEFERRED | Same. |
| `skills/manager/skill_manager.py` | DEFERRED | Same. |
| `skills/manager/skill_registry.py` | DEFERRED | Same. |
| `skills/manager/skill_validator.py` | DEFERRED | Same. |

## Summary statistics

- **Total zero-byte `.py` files**: 19 (legacy dirs) + 5 (voice) + 2 (context) + 1 (automation) + 25 (vision) + 6 (memory/ai/skills) = **58 files**.
- **Class REPLACED**: 18 files â€” already have a V6 equivalent in `core/` or `system/`.
- **Class DEFERRED**: 35 files â€” V6 has the contracts/interfaces, no implementation yet. Most are Phase 5+ vision/AI work.
- **Class DROPPED**: No Dropped.
- **Class MIGRATION-TRACK**: 3 files â€” `__init__.py` shells in the legacy namespaces.

## Phase 7.2 additions (Vision + Agent integration)

Phase 7.2 hardens the boundary between
:class:`core.services.vision_service.VisionService` and
:class:`core.orchestration.agent.Agent`.  Three new modules were
added in production paths, replacing the previously-zero-byte
shells:

| File | Class | Notes |
|------|-------|-------|
| `core/orchestration/grounding.py` | **POPULATED** | The typed contract (:class:`TargetGroundingContract`, :class:`GroundingStatus`) between Vision and the Agent.  Replaces the V5 / Phase 7 stub.  Default confidence threshold is 0.5. |
| `core/orchestration/vision_adapter.py` | **POPULATED** | The only place that turns a :class:`TargetGroundingContract` into an :class:`ActionRequest`.  R-21 enforcement: the adapter is the *only* path from vision into the closed capability set. |
| `core/services/vision_service.py` | **POPULATED** | The :class:`VisionService` (R-14: not a singleton).  Lazy screenshot acquisition: screenshots are only acquired when a screenshot-requiring strategy is actually invoked.  Returns a structured :class:`VisionResult` (R-8: never claims ``verified=True`` from a single screenshot). |
| `vision/router/perception_router.py` | **POPULATED** | :class:`PerceptionRouter`: adaptive but deterministic.  Strategy order is query-aware + reliability-ranked.  Ties are NOT silently broken â€” :class:`AmbiguityError` is raised instead. |
| `tests/test_phase7_2_vision_agent_integration.py` | **NEW** | 19 deterministic tests Aâ€“S covering the new boundary end-to-end.  All pass. |

The 25 zero-byte vision stubs in `vision/strategies/`,
`vision/observations/`, `vision/router/`, and `vision/coordination/`
(see Phase 7 implementation report) have been **replaced** by the
populated modules above.  No zero-byte vision stubs remain on the
V6 critical path.

## Why these placeholders are not removed in Phase 4

1. **V5 references**: V5 documentation and external tickets cite these paths. Removing them before V5 is decommissioned causes link-rot and broken `import` statements in any V5 tooling that is still operational.
2. **Phase 5 contracts need the namespaces**: The Phase 5 Brain/Planner work will populate the previously-zero-byte files in `core/agent/`, `core/planning/`, `memory/`, and `skills/manager/`. Removing the file means re-creating it later.
3. **Vision is genuinely deferred**: The vision subsystem is a real product capability, just one that the V6 Brain/Orchestration phases do not need. Keeping the file shells documents the roadmap.

## Hard rules

- **Zero V5 source is copied into V6 production paths.** A `grep` for `V5` in `core/` shows only historical docstring context â€” no code reuse from V5.
- **Zero duplicate registries.** There is one `CapabilityRegistry`, one `ServiceRegistry`, one `ContextService`, one `EventBus`. The zero-byte `core/agent/` and `core/planning/` modules do not define competing registries.
- **Zero giant modules.** The largest non-`.pyc` file in V6 production code is `core/orchestration/models.py` at 703 lines (Phase 4 dataclass contract pack). No "god module" in the V6 tree.
- **Zero LLM direct Windows access.** LLM code will live in the Phase 5 Brain, and the Brain has no direct access to Windows â€” it can only call capabilities through the `CapabilityRouter` (R-21).

