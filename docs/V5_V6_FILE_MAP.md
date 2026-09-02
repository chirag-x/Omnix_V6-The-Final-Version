# V5 → V6 File Map

**Phase:** 0 — Forensic Audit
**Status:** Complete
**Date:** 2026-08-29

This document maps every populated V5 file to its V6 destination. The format is:

```
V5 path  (V5 lines if known)  →  V6 path  (V6 status)
```

V6 status legend:
- `[empty]` — 0-byte placeholder, present
- `[missing]` — directory or file does not exist
- `[NEW]` — file did not exist in V5; created fresh in V6
- `[KEEP]` — same file exists in both with same content
- `[REWRITE]` — V6 file exists but is empty; needs to be rewritten from V5 source

---

## 1. Top-level

| V5 | V6 | Notes |
|---|---|---|
| `Omnix_V5/main.py` (314 L) | `Omnix_V6/main.py` [REWRITE] | Boot sequence; branded console; subsystem walk; `engine.start()`; `_print_ready_banner`; `print_result`; `main()`. Preserve `OMNIX_HEADLESS` / `OMNIX_QUIET_BOOT` env-var pattern. |
| `Omnix_V5/README.md` (185 L) | `Omnix_V6/README.md` [REWRITE] | Refresh for V6; link to `My Goal for Omnix.md`. |
| `Omnix_V5/requirements.txt` (217 L, UTF-16) | `Omnix_V6/requirements.txt` [REWRITE] | See `docs/V6_DEPENDENCY_PLAN.md` for the V6 pin plan. |
| `Omnix_V5/.env` (OpenRouter + Groq) | `Omnix_V6/.env` [REWRITE] | Same shape. **Move keys to env-vars / keyring in Phase 0.5.** |
| `Omnix_V5/users/` (empty) | `Omnix_V6/users/` [missing] | Create on first run. |
| `Omnix_V5/assets/animations/` (empty) | `Omnix_V6/assets/animations/` [empty] | Keep; populated at runtime. |
| `Omnix_V5/assets/icons/` (empty) | `Omnix_V6/assets/icons/` [empty] | Keep. |
| `Omnix_V5/assets/sounds/` (empty) | `Omnix_V6/assets/sounds/` [empty] | Keep. |
| `Omnix_V5/config/*.json` (4 empty) | `Omnix_V6/config/*.json` [empty] | Keep; populate in Phase 0.5. |
| `Omnix_V5/logs/omnix.log` | `Omnix_V6/logs/omnix.log` [empty] | Keep; rotation policy added in Phase 0.5. |
| `Omnix_V5/My Goal for Omnix.md` | `Omnix_V6/My Goal for Omnix.md` [KEEP] | Source-of-truth product goal. **Unchanged in V6.** |

---

## 2. `core/` — central wiring

| V5 | V6 | Lines (V5) | Migration note |
|---|---|---|---|
| `core/omnix_engine.py` | `core/omnix_engine.py` [REWRITE] | 3637 | The orchestrator. **Single boot path.** Keep all 49 methods; preserve phase annotations; preserve `auto_start=False` + post-construction `start()` pattern used by `main.py`. |
| `core/services/ai_service.py` | `core/services/ai_service.py` [REWRITE] | ~1100 | `AIResult` dataclass. BrainManager wired in. |
| `core/services/automation_service.py` | `core/services/automation_service.py` [REWRITE] | ~1100 | `AutomationResult`. |
| `core/services/context_service.py` | `core/services/context_service.py` [REWRITE] | ~900 | `ContextResult`. |
| `core/services/memory_service.py` | `core/services/memory_service.py` [REWRITE] | ~1300 | `MemoryResult` (full `success/value/provider/operation/error/metadata` shape). |
| `core/services/skills_service.py` | `core/services/skills_service.py` [REWRITE] | ~1100 | `SkillsResult`. |
| `core/services/ui_service.py` | `core/services/ui_service.py` [REWRITE] | ~900 | `UIResult` (no-op for now; UI in `frozen/` is **not** migrated). |
| `core/services/vision_service.py` | `core/services/vision_service.py` [REWRITE] | ~1300 | `VisionResult`. |
| `core/services/voice_service.py` | `core/services/voice_service.py` [REWRITE] | ~1200 | `VoiceResult`. |
| `core/agent/agent_controller.py` | `core/agent/agent_controller.py` [REWRITE] | ~2200 | Phase 11/19. Closed-loop: VOICE→UNDERSTAND→MEMORY/CONTEXT→PLAN→EXECUTE→OBSERVE→STEP VERIFY→RECOVERY/RETRY→GOAL VERIFY→MEMORY→VOICE. |
| `core/agent/goal_executor.py` | `core/agent/goal_executor.py` [REWRITE] | ~1100 | Per-step execution + StepVerifier gate. |
| `core/agent/goal_verifier.py` | `core/agent/goal_verifier.py` [REWRITE] | ~900 | Goal-level verdict (achieved / failed / uncertain). |
| `core/agent/observation_loop.py` | `core/agent/observation_loop.py` [REWRITE] | ~800 | Per-step observation. |
| `core/agent/recovery_engine.py` | `core/agent/recovery_engine.py` [REWRITE] | ~900 | RETRY + ALTERNATIVE strategies. |
| `core/agent/retry_manager.py` | `core/agent/retry_manager.py` [REWRITE] | ~800 | Per-step retry budget. |
| `core/agent/step_verifier.py` | `core/agent/step_verifier.py` [REWRITE] | ~900 | Per-step verdict (passed / failed / uncertain). |
| `core/agent/wait_engine.py` | `core/agent/wait_engine.py` [REWRITE] | ~800 | Wait-for-state helpers. |
| `core/agent/workflow_planner.py` | `core/agent/workflow_planner.py` [REWRITE] | ~1400 | Step DAG. |
| `core/planning/intent_classifier.py` | `core/planning/intent_classifier.py` [REWRITE] | ~700 | Conversation vs. action vs. question. |
| `core/planning/command_processor.py` | `core/planning/command_processor.py` [REWRITE] | ~1100 | Compound commands, slot filling. |
| `core/planning/task_planner.py` | `core/planning/task_planner.py` [REWRITE] | ~1300 | Plan construction. |
| `core/planning/plan_validator.py` | `core/planning/plan_validator.py` [REWRITE] | ~700 | Plan sanity checks. |
| `core/planning/slot_extractor.py` | `core/planning/slot_extractor.py` [REWRITE] | ~700 | Entity extraction. |
| `core/planning/capability_resolver.py` | `core/planning/capability_resolver.py` [REWRITE] | ~700 | Which skill can do what. |
| `core/planning/__init__.py` | `core/planning/__init__.py` [REWRITE] | — | Re-exports. |
| `core/events/event_bus.py` | `core/events/event_bus.py` [REWRITE] | ~700 | Phase 5. Pub/sub. |
| `core/events/event_types.py` | `core/events/event_types.py` [REWRITE] | — | EventType enum. |
| `core/events/event_publisher.py` | `core/events/event_publisher.py` [REWRITE] | — | Thin facade. |
| `core/events/__init__.py` | `core/events/__init__.py` [REWRITE] | — | Re-exports. |
| `core/execution/execution_status.py` | `core/execution/execution_status.py` [REWRITE] | ~600 | `ExecutionStatus` enum + `normalize_result` (Phase 6). |
| `core/state/...` (4 files) | `core/state/...` [REWRITE] | — | Conversation state, session state, etc. |
| `core/utils/...` (5 files) | `core/utils/...` [REWRITE] | — | Engine-internal utilities. |
| `core/compatibility/...` (3 files) | `core/compatibility/...` [REWRITE] | — | Backward-compat shims. |
| `core/capability_router.py` | `core/capability_router.py` [REWRITE] | — | Routes commands to services. |
| `core/dependency_manager.py` | `core/dependency_manager.py` [REWRITE] | — | Service dependency graph. |
| `core/engine_manager.py` | `core/engine_manager.py` [REWRITE] | — | Engine lifecycle. |
| `core/lifecycle_manager.py` | `core/lifecycle_manager.py` [REWRITE] | — | Subsystem initialize/shutdown ordering. |
| `core/health_monitor.py` | `core/health_monitor.py` [REWRITE] | — | Per-subsystem health. |

---

## 3. `system/` — low-level desktop OS surface

| V5 | V6 | Migration note |
|---|---|---|
| `system/applications/application_manager.py` | `system/applications/application_manager.py` [REWRITE] | Discovery, launch, monitor, history, cache. |
| `system/applications/...` (~7 sub-files) | `system/applications/...` [REWRITE] | discovery, cache, history, monitor, installer, launch strategy, process resolver. |
| `system/automation/automation_manager.py` | `system/automation/automation_manager.py` [REWRITE] | Public API. |
| `system/automation/action_executor.py` | `system/automation/action_executor.py` [REWRITE] | Per-action dispatcher. |
| `system/automation/workflow_executor.py` | `system/automation/workflow_executor.py` [REWRITE] | **Fix latent queue bug (R1) in Phase 0.5.** |
| `system/automation/recovery_manager.py` | `system/automation/recovery_manager.py` [REWRITE] | Stub today (R2). Design real recovery. |
| `system/automation/retry_manager.py` | `system/automation/retry_manager.py` [REWRITE] | Retry policy. |
| `system/automation/verification.py` | `system/automation/verification.py` [REWRITE] | Hook into vision (R3). |
| `system/automation/safety_manager.py` | `system/automation/safety_manager.py` [REWRITE] | Blocked-actions set. |
| `system/automation/action_history.py` | `system/automation/action_history.py` [REWRITE] | Per-action log. |
| `system/automation/action_queue.py` | `system/automation/action_queue.py` [REWRITE] | Queue. |
| `system/automation/execution_context.py` | `system/automation/execution_context.py` [REWRITE] | Per-workflow context. |
| `system/browser/browser_controller.py` | `system/browser/browser_controller.py` [REWRITE] | Async facade (Phase 21). |
| `system/diagnostics/...` | `system/diagnostics/...` [REWRITE] | Self-test helpers. |
| `system/filesystem/...` | `system/filesystem/...` [REWRITE] | File ops. |
| `system/input/input_manager.py` | `system/input/input_manager.py` [REWRITE] | Public API. |
| `system/input/async_adapter.py` | `system/input/async_adapter.py` [REWRITE] | **Async adapter pattern (Phase 21).** |
| `system/input/clipboard.py` | `system/input/clipboard.py` [REWRITE] | |
| `system/input/gestures.py` | `system/input/gestures.py` [REWRITE] | |
| `system/input/hotkeys.py` | `system/input/hotkeys.py` [REWRITE] | |
| `system/input/keyboard.py` | `system/input/keyboard.py` [REWRITE] | |
| `system/input/mouse.py` | `system/input/mouse.py` [REWRITE] | |
| `system/input/scrolling.py` | `system/input/scrolling.py` [REWRITE] | |
| `system/input/shortcuts.py` | `system/input/shortcuts.py` [REWRITE] | |
| `system/input/typing.py` | `system/input/typing.py` [REWRITE] | |
| `system/interfaces/...` | `system/interfaces/...` [REWRITE] | |
| `system/memory/...` | `system/memory/...` [REWRITE] | System-memory service. |
| `system/models/action.py` | `system/models/action.py` [REWRITE] | Action dataclass. |
| `system/models/action_result.py` | `system/models/action_result.py` [REWRITE] | ActionResult. |
| `system/models/application.py` | `system/models/application.py` [REWRITE] | Application. |
| `system/models/execution_result.py` | `system/models/execution_result.py` [REWRITE] | ExecutionResult. |
| `system/models/window.py` | `system/models/window.py` [REWRITE] | Window. |
| `system/models/workflow.py` | `system/models/workflow.py` [REWRITE] | Workflow. |
| `system/power/...` | `system/power/...` [REWRITE] | |
| `system/processes/...` | `system/processes/...` [REWRITE] | |
| `system/scheduler/...` | `system/scheduler/...` [REWRITE] | |
| `system/services/...` | `system/services/...` [REWRITE] | |
| `system/utils/...` | `system/utils/...` [REWRITE] | |
| `system/windows/window_manager.py` | `system/windows/window_manager.py` [REWRITE] | Public API. |
| `system/windows/...` (6 sub-files) | `system/windows/...` [REWRITE] | focus, monitor, detector, finder, state, tracker. |

---

## 4. `skills/`

| V5 | V6 | Migration note |
|---|---|---|
| `skills/manager/skill_manager.py` | `skills/manager/skill_manager.py` [REWRITE] | 311 L. Public API. |
| `skills/manager/...` (3 more) | `skills/manager/...` [REWRITE] | registry (226 L), loader (196 L), dispatcher. |
| `skills/core/...` (6 files) | `skills/core/...` [REWRITE] | Skill lifecycle. |
| `skills/built_in/applications/...` | `skills/built_in/applications/...` [REWRITE] | Open, close, focus, etc. |
| `skills/built_in/browser/...` | `skills/built_in/browser/...` [REWRITE] | Navigate, search, click. |
| `skills/built_in/files/...` | `skills/built_in/files/...` [REWRITE] | File ops. |
| `skills/built_in/input/...` | `skills/built_in/input/...` [REWRITE] | Click, type, hotkey. |
| `skills/built_in/media/...` | `skills/built_in/media/...` [REWRITE] | Play, pause, volume. |
| `skills/built_in/system/...` | `skills/built_in/system/...` [REWRITE] | Lock, sleep, screenshot. |
| `skills/built_in/vision/...` | `skills/built_in/vision/...` [REWRITE] | Find element, OCR, click. |
| `skills/capabilities/...` | `skills/capabilities/...` [REWRITE] | Capability declarations. |
| `skills/runtime/service_adapters.py` | `skills/runtime/service_adapters.py` [missing] | **Create the `skills/runtime/` directory in Phase 0.5.** |
| `skills/tests/...` (5 + 1 mock) | `skills/tests/...` [missing] | Migrate per `docs/V6_TEST_MIGRATION_PLAN.md`. |

---

## 5. `vision/`

| V5 | V6 | Migration note |
|---|---|---|
| `vision/vision_manager.py` (247 L) | `vision/vision_manager.py` [REWRITE] | Public API. |
| `vision/vision_pipeline.py` | `vision/vision_pipeline.py` [REWRITE] | |
| `vision/screen_observer.py` (79 L) | `vision/screen_observer.py` [REWRITE] | |
| `vision/text_detector.py` | `vision/text_detector.py` [REWRITE] | |
| `vision/ui_detector.py` | `vision/ui_detector.py` [REWRITE] | |
| `vision/element_locator.py` | `vision/element_locator.py` [REWRITE] | |
| `vision/screen_intelligence.py` | `vision/screen_intelligence.py` [REWRITE] | |
| `vision/screen_summary.py` | `vision/screen_summary.py` [REWRITE] | |
| `vision/vision_controller.py` | `vision/vision_controller.py` [REWRITE] | |
| `vision/detection/...` (5 files) | `vision/detection/...` [REWRITE] | |
| `vision/models/...` (5 files) | `vision/models/...` [REWRITE] | |
| `vision/models/yolo11n.pt` (5.6 MB) | `vision/models/yolo11n.pt` [REWRITE] | **Real weights**; Phase 0.5 download. |
| `vision/hierarchy/...` (2 files) | `vision/hierarchy/...` [REWRITE] | |
| `vision/summary/...` (2 files) | `vision/summary/...` [REWRITE] | |
| `vision/discovery/...` (2 files) | `vision/discovery/...` [REWRITE] | |
| `vision/utils/...` (2 empty) | `vision/utils/...` [missing] | **Do not recreate** unless Phase 0.5 needs them. |

---

## 6. `voice/`

| V5 | V6 | Migration note |
|---|---|---|
| `voice/voice_manager.py` (1162 L) | `voice/voice_manager.py` [REWRITE] | Public API. |
| `voice/wake_listener.py` | `voice/wake_listener.py` [REWRITE] | "Hey Omnix" wake word. |
| `voice/speech_recognizer.py` | `voice/speech_recognizer.py` [REWRITE] | faster-whisper. |
| `voice/tts_engine.py` | `voice/tts_engine.py` [REWRITE] | edge-tts. |
| `voice/audio_utils.py` | `voice/audio_utils.py` [REWRITE] | |

---

## 7. `automation/`

| V5 | V6 | Migration note |
|---|---|---|
| `automation/automation_engine.py` (722 L) | `automation/automation_engine.py` [REWRITE] | Top-level public API. |
| `automation/...` (any sub-files) | `automation/...` [REWRITE] | |

---

## 8. `context/`

| V5 | V6 | Migration note |
|---|---|---|
| `context/context_manager.py` (55 L) | `context/context_manager.py` [REWRITE] | |
| `context/screen_context.py` | `context/screen_context.py` [REWRITE] | |

---

## 9. `memory/`

| V5 | V6 | Migration note |
|---|---|---|
| `memory/memory_coordinator.py` (1068 L) | `memory/memory_coordinator.py` [REWRITE] | Coordinates 3 backends. |
| `memory/memory_manager.py` | `memory/memory_manager.py` [REWRITE] | Semantic memory. |
| `memory/behavior_memory.py` | `memory/behavior_memory.py` [REWRITE] | |
| `memory/ui_pattern_memory.py` | `memory/ui_pattern_memory.py` [REWRITE] | Vision-owned in V5; kept here for V6 storage symmetry. |

---

## 10. `ai/`

| V5 | V6 | Migration note |
|---|---|---|
| `ai/brain_manager.py` (232 L) | `ai/brain_manager.py` [REWRITE] | LLM facade. |

---

## 11. `utils/`

| V5 | V6 | Migration note |
|---|---|---|
| `utils/cache_manager.py` | `utils/cache_manager.py` [REWRITE] | |
| `utils/constants.py` | `utils/constants.py` [REWRITE] | |
| `utils/helpers.py` | `utils/helpers.py` [REWRITE] | |
| `utils/logger.py` | `utils/logger.py` [REWRITE] | loguru setup. |
| `utils/performance_monitor.py` | `utils/performance_monitor.py` [REWRITE] | |

---

## 12. `frozen/` — **NOT migrated**

| V5 | V6 |
|---|---|
| `frozen/skills/...` | **Do not recreate.** |
| `frozen/state/environment_state.py` | **Do not recreate.** |
| `frozen/system_events/...` | **Do not recreate.** |
| `frozen/system_manager.py` | **Do not recreate.** |
| `frozen/system_ui/...` | **Do not recreate.** |
| `frozen/ui/...` | **Do not recreate.** |

V5's `frozen/` pattern enforced "do not import this." V6 enforces the same rule by **absence** — there is no `frozen/` directory. If a module is deprecated, delete it (record the rationale in `V5_V6_MIGRATION_AUDIT.md` addenda).

---

## 13. `tests/` — see `docs/V6_TEST_MIGRATION_PLAN.md`

| V5 | V6 | Migration note |
|---|---|---|
| `tests/test_connected_loop.py` | `tests/test_connected_loop.py` [REWRITE] | pytest-style. Phase 10. |
| `tests/test_phase16_uia_smoke.py` | `tests/test_phase16_uia_smoke.py` [REWRITE] | `main()` script, `OMNIX_HEADLESS=0`. Phase 16. |
| `tests/test_pipeline.py` | `tests/test_pipeline.py` [REWRITE] | Pipeline only. |
| `tests/test_real_execution.py` | `tests/test_real_execution.py` [REWRITE] | Real Windows. Phase 17. |
| `tests/test_real_loop.py` | `tests/test_real_loop.py` [REWRITE] | Closed loop. Phase 11. |
| `tests/test_routing_reliability.py` | `tests/test_routing_reliability.py` [REWRITE] | Phase 12. |
| `tests/test_ui_compound_reliability.py` | `tests/test_ui_compound_reliability.py` [REWRITE] | Phase 15. |
| `tests/test_verification_recovery.py` | `tests/test_verification_recovery.py` [REWRITE] | pytest-style. Phase 11. |
| `tests/test_vision_action_reliability.py` | `tests/test_vision_action_reliability.py` [REWRITE] | Phase 16b. |

---

## 14. Summary

| Domain | V5 non-empty files | V6 placeholder files | Migration mode |
|---|---|---|---|
| Top-level | ~5 (main, README, requirements, .env, My Goal) | 0 | Rewrite |
| `core/` | ~50 | 50 | Rewrite |
| `system/` | ~100 | ~100 | Rewrite |
| `skills/` | ~50 | ~50 | Rewrite |
| `vision/` | ~25 (+ yolo11n.pt) | 25 | Rewrite + download weights |
| `voice/` | 5 | 5 | Rewrite |
| `automation/` | ~1–5 | 1–5 | Rewrite |
| `context/` | 2 | 2 | Rewrite |
| `memory/` | 4 | 4 | Rewrite |
| `ai/` | 1 | 1 | Rewrite |
| `utils/` | 5 | 5 | Rewrite |
| `tests/` | 9 | 0 | **Create** |
| `frozen/` | ~20 | 0 (intentional) | **Do not recreate** |
| **Total** | **~340** | **285** | — |

**PHASE 0 COMPLETE — NO SOURCE CODE MODIFIED. WAITING FOR APPROVAL TO BEGIN PHASE 0.5.**
