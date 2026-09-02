# V6 Phase Roadmap

**Phase:** 0 â€” Forensic Audit
**Status:** Complete
**Date:** 2026-08-29

---

## 1. Purpose

This document is the **phased migration plan** for Omnix V5 â†’ V6. Each phase has a clear scope, a "done" definition, and a hand-off to the next phase. No phase starts without explicit user approval.

**This document is a plan, not a commitment.** The user reviews each phase before approval. The roadmap can be amended in `V5_V6_MIGRATION_AUDIT.md` addenda.

---

## 2. Phase 0 (current) â€” Forensic Audit

**Scope:** Read V5 and V6. Produce 7 docs under `E:\Coding\Omnix\Omnix_V6- The final version\docs\`. Do not modify any source code.

**Done when:**

- [x] V5 and V6 fully inventoried.
- [x] All 7 docs written.
- [x] No source code modified.
- [x] Final terminal message emitted.

**Status:** âœ… Complete. Awaiting approval to enter Phase 0.5.

---

## 3. Phase 0.5 â€” V6 skeleton boot

**Goal:** Get V6 to "OMNIX V6 IS READY" in <10 seconds, with no real hardware access, on Python 3.13.15.

**Scope:**

1. **Install core runtime** (per `V6_DEPENDENCY_PLAN.md` Â§3.1) only.
2. **Create V6 `requirements.txt`** with the Phase 0.5 boot set.
3. **Create V6 `.env`** with placeholder keys + comment.
4. **Copy/recreate `main.py`** from V5 (branded boot, `OMNIX_HEADLESS=1`, `OMNIX_QUIET_BOOT=1`).
5. **Copy/recreate `core/omnix_engine.py`** from V5 (3637 L).
6. **Create V6 `core/services/*` stubs** that import from V5 internals and forward calls. Goal: engine boots without errors.
7. **Download `yolo11n.pt`** (5.6 MB) to `vision/models/`.
8. **Create V6 `tests/test_no_frozen_directory.py`** + `test_logging_no_stdlib_logging.py` + `test_env_var_gates_respected.py`.
9. **Add `pytest.ini` and `pyproject.toml`**.

**Done when:**

- `python main.py` from V6 prints "OMNIX V6 IS READY" without crashing.
- All Phase 0.5 tests pass.
- No real microphone, camera, or screen access occurs.

**Estimated time:** 4â€“8 hours.

**Status:** Not started. Awaiting approval.

---

## 4. Phase 1 â€” Core services + agent loop

**Goal:** V6 engine handles a text command (no voice, no real hardware) end-to-end with full verification gates.

**Scope:**

1. **Install ML / Vision dependencies** (per `V6_DEPENDENCY_PLAN.md` Â§3.2).
2. **Recreate `core/services/*Service.py`** (8 services) from V5, with full `*Result` dataclass contracts.
3. **Recreate `core/agent/*`** (9 files) from V5.
4. **Recreate `core/planning/*`** (7 files) from V5.
5. **Recreate `core/execution/execution_status.py`** from V5.
6. **Recreate `core/events/*`** (4 files) from V5.
7. **Add `tests/test_service_wrapper_contract.py`** + `test_result_normalization.py` + `test_event_bus_pubsub.py` + `test_orchestrator_initialize_shutdown.py` + `test_subsystem_lifecycle_uniformity.py`.
8. **Migrate `test_connected_loop.py`** + `test_verification_recovery.py` + `test_pipeline.py` to V6 (pytest-style).
9. **Verify the closed loop** for "open chrome" via mock automation (no real apps launched).

**Done when:**

- All 9 V5 tests migrated to V6 pass in `pytest -m "not real_windows"`.
- `engine.execute("open chrome")` returns `AIResult(success=True)` with no real Chrome launched (mock).
- StepVerifier + GoalVerifier fire on every step.
- EventBus records every `EventType.COMMAND_*` and `EventType.PLAN_*` event.

**Estimated time:** 16â€“32 hours.

---

## 5. Phase 2 â€” Voice I/O

**Goal:** "Hey Omnix" wake word + speech-to-text + text-to-speech, all on-device.

**Scope:**

1. **Install voice dependencies** (per `V6_DEPENDENCY_PLAN.md` Â§3.3).
2. **Recreate `voice/*`** (5 files) from V5.
3. **Recreate `voice/wake_listener.py`** with `openwakeword`.
4. **Recreate `voice/speech_recognizer.py`** with `faster-whisper`.
5. **Recreate `voice/tts_engine.py`** with `edge-tts`.
6. **Recreate `core/services/voice_service.py`** with the `VoiceResult` contract.
7. **Wire voice into the event bus** (Phase 5 wake event).
8. **Add `tests/test_voice_pipeline.py`** (mock audio input, mock recognizer, mock TTS).

**Done when:**

- "Hey Omnix" spoken into a real microphone triggers a `WAKE` event.
- A spoken command is transcribed (with confidence â‰¥ 0.6) and passed to the agent loop.
- A spoken response is generated via TTS.
- All Phase 2 tests pass.

**Estimated time:** 12â€“20 hours.

---

## 6. Phase 3 â€” Desktop automation

**Goal:** Omnix can open apps, click buttons, type text, and verify the result on a real Windows desktop.

**Scope:**

1. **Install automation dependencies** (per `V6_DEPENDENCY_PLAN.md` Â§3.4).
2. **Recreate `system/applications/*`** (8 files) from V5.
3. **Recreate `system/automation/*`** (8 files) from V5. **Fix R1 (latent queue bug)** while migrating.
4. **Recreate `system/input/*`** (8 files) from V5, including `async_adapter.py`.
5. **Recreate `system/windows/*`** (6 files) from V5.
6. **Recreate `system/browser/browser_controller.py`** (async facade).
7. **Recreate `system/models/*`** (6 files: Action, ActionResult, ExecutionResult, Workflow, Window, Application).
8. **Recreate `automation/automation_engine.py`** (722 L) from V5.
9. **Recreate `skills/manager/*`** + `skills/core/*` + `skills/built_in/*` from V5.
10. **Create `skills/runtime/`** + `service_adapters.py`.
11. **Migrate `test_real_execution.py`** (5 real commands) + `test_ui_compound_reliability.py` (5 UI cases) to V6.

**Done when:**

- All 10 migrated tests pass on a real Windows host.
- `engine.execute("open chrome")` actually opens Chrome (psutil-validated).
- `engine.execute("type hello world")` types into the focused window.
- `engine.execute("click the save button")` clicks the save button (vision-validated).
- StepVerifier overrides claimed success to failure when the action didn't happen.
- `recover()` in `system/automation/recovery_manager.py` is no longer a stub (R2 fixed).

**Estimated time:** 30â€“50 hours.

---

## 7. Phase 4 â€” Vision + screen understanding

**Goal:** Omnix can see the screen, find UI elements, and act on them.

**Scope:**

1. **Recreate `vision/*`** (~25 files) from V5, plus `vision/utils/` (2 files) if needed.
2. **Wire `vision_manager` into `core/services/vision_service.py`** (already done in Phase 1, but expand).
3. **Recreate `vision/detection/*`** (5 files) with YOLO + easyocr.
4. **Recreate `vision/hierarchy/*`** + `vision/summary/*` + `vision/discovery/*` (6 files).
5. **Recreate `vision/screen_observer.py`** with mss/screenshot.
6. **Hook vision into `system/automation/verification.py`** (R3 fix).
7. **Migrate `test_phase16_uia_smoke.py`** + `test_vision_action_reliability.py` (10 real-Windows cases) to V6.

**Done when:**

- `engine.execute("click the save button")` finds the save button via YOLO/UI detection and clicks it.
- `engine.vision_manager.describe_screen()` returns a structured description of the current screen.
- 8-state ladder (`APP_NOT_RUNNING` â€¦ `GOAL_VERIFIED`) is exercised end-to-end on at least 3 commands.

**Estimated time:** 20â€“35 hours.

---

## 8. Phase 5 â€” Memory + context

**Goal:** Omnix remembers past sessions, recalls relevant context, and uses it for planning.

**Scope:**

1. **Recreate `memory/*`** (4 files) from V5.
2. **Recreate `memory/memory_coordinator.py`** (1068 L) â€” coordinator over 3 backends (semantic, behavior, system).
3. **Recreate `context/*`** (2 files) from V5.
4. **Recreate `core/services/memory_service.py`** + `context_service.py` (already done in Phase 1, but expand to semantic search).
5. **Wire semantic embeddings** (sentence-transformers) into `memory_manager.recall`.
6. **Add `tests/test_memory_recall.py`** â€” given a stored fact, recall it with the right query.
7. **Add `tests/test_context_gathering.py`** â€” context service pulls screen + recent apps + recent commands.

**Done when:**

- `engine.execute("what was I working on yesterday")` returns a relevant past session.
- `engine.execute("open chrome")` benefits from prior context (e.g. restores the previous tab).
- `memory_service.remember` + `memory_service.recall` pass round-trip tests.

**Estimated time:** 12â€“20 hours.

---

## 9. Phase 6 â€” AI brain maturity

**Goal:** The Brain picks the right model for the right task, with rate limiting, retries, and prompt caching.

**Scope:**

1. **Recreate `ai/brain_manager.py`** (232 L) from V5.
2. **Add provider selection logic** (per `V6_MODEL_PLAN.md` Â§5.2).
3. **Add rate limiting + retry** with backoff.
4. **Add prompt caching** for repeated system prompts.
5. **Add `tests/test_brain_provider_selection.py`** â€” Brain picks Groq for fast classify, OpenRouter for plan.
6. **Add `tests/test_brain_fallback.py`** â€” OpenRouter down â†’ Groq â†’ local regex.

**Done when:**

- Brain provider selection is exercised by 4+ tests.
- Rate limiting kicks in after N requests/minute (configurable).
- Brain falls back gracefully when a provider returns 5xx.

**Estimated time:** 10â€“16 hours.

---

## 10. Phase 7 â€” Goal-oriented planning

**Goal:** Complex commands like "Help me prepare for my exam next week" produce a multi-step plan, not a single action.

**Scope:**

1. **Expand `core/agent/workflow_planner.py`** from V5 with explicit goal decomposition.
2. **Add `tests/test_complex_plan_decomposition.py`** â€” given "prepare for exam", produce â‰¥ 5 steps.
3. **Add `tests/test_replan_loop.py`** â€” replan on failure up to `max_replan_attempts`.
4. **Wire the planner into the agent loop** with explicit goal/scope/timeline semantics.
5. **Add goal verification beyond per-step** â€” `goal_verifier` evaluates the final state against the original goal.

**Done when:**

- A natural-language command like "Help me research this topic" produces a multi-step plan.
- The plan is visible in logs and via `engine.debug.last_plan`.
- Replan loop terminates at the configured budget.
- GoalVerifier's verdict overrides the executor's claim (R-5 invariant).

**Estimated time:** 16â€“24 hours.

---

## 11. Phase 8 â€” UI (debug only)

**Goal:** A debug panel to inspect engine state. NOT a user-facing UI.

**Scope:**

1. **Install PyQt6** (per `V6_DEPENDENCY_PLAN.md` Â§3.6).
2. **Create `debug/panel.py`** â€” read-only view of conversation, plan, step results, memory.
3. **Wire to event bus** for live updates.
4. **No tray icon, no notification popup, no settings window.** (Those were in V5's `frozen/ui/`.)

**Done when:**

- Debug panel shows live engine state.
- No production code path depends on the panel.

**Estimated time:** 8â€“12 hours.

---

## 12. Phase 9 â€” Security hardening

**Goal:** API keys out of `.env`. Safety policies enforced. Audit log.

**Scope:**

1. **Move API keys to `keyring`** (Windows Credential Manager).
2. **Update `.env.example`** with placeholders.
3. **Add `pre-commit` hook** to scan `config/*.json` for secret patterns.
4. **Define `system/automation/safety_manager.py` policy DSL** (R4 design).
5. **Add `tests/test_secrets_not_in_config.py`** + `test_safety_policy_blocks_dangerous_action.py`.
6. **Add audit log** to `logs/omnix.audit.log` â€” every action and its outcome.

**Done when:**

- No API keys in `.env` or `config/`.
- Safety policy blocks the configured dangerous actions.
- Audit log is queryable.

**Estimated time:** 8â€“12 hours.

---

## 13. Phase 10 (Voice Completed) â€” Observability

**Goal:** Profiler, metrics, error budget. See where Omnix is slow and where it fails.

**Scope:**

1. **Add `core/utils/profiler.py`** + `metrics.py` (V5 had minimal versions; expand).
2. **Wire profiler into the agent loop** â€” every step, every verifier, every recovery is timed.
3. **Add metrics counters** for: commands received, intents classified, plans created, steps executed, verifications run, recoveries triggered, replans triggered.
4. **Add `/debug/metrics` API** (CLI or debug panel).
5. **Add `tests/test_metrics_collection.py`** + `test_profiler_records_section_durations.py`.

**Done when:**

- `/debug/metrics` shows real numbers for a 10-command run.
- Slowest step in a typical run is identifiable in <5 seconds.

**Estimated time:** 8â€“12 hours.

---

## 14. Phase 11 â€” Performance + scaling

**Goal:** Omnix responds in <2s for simple commands, <10s for complex ones, on a typical Windows 11 host.

**Scope:**

1. **Profile the boot path.** Target: <5s from `python main.py` to "OMNIX V6 IS READY".
2. **Profile the agent loop.** Target: <500ms per step overhead.
3. **Lazy-load models** (already designed in `V6_MODEL_PLAN.md` Â§4.1; verify).
4. **Cache repeated LLM calls** with prompt caching.
5. **Add `tests/test_boot_under_5_seconds.py`** + `test_step_overhead_under_500ms.py`.

**Done when:**

- Boot is <5s on a 2020-era laptop.
- A 3-step plan executes in <5s end-to-end (excluding model inference).

**Estimated time:** 12â€“20 hours.

---

## 15. Phase 12 â€” Documentation + handoff

**Goal:** V6 is documented well enough that a new contributor can boot it in <30 minutes.

**Scope:**

1. **Write `README.md`** â€” quick start, environment, common commands.
2. **Write `docs/ARCHITECTURE.md`** â€” the one diagram + R-1 to R-20.
3. **Write `docs/CONTRIBUTING.md`** â€” how to add a skill, how to add a service, how to add a test.
4. **Write `docs/SECURITY.md`** â€” key management, safety policy, audit log.
5. **Write `docs/TROUBLESHOOTING.md`** â€” common failure modes.
6. **Generate API reference** from docstrings (mkdocstrings or pdoc).

**Done when:**

- A new contributor can boot V6 in <30 minutes following only `README.md`.
- `docs/` is complete and current.

**Estimated time:** 12â€“16 hours.

---

## 16. Phase 13 â€” V6 release

**Goal:** V6 is feature-complete, documented, and stable enough to supersede V5 as the development target.

**Scope:**

1. **All Phase 0.5 through 12 tests pass.**
2. **Tag the release** (V6.0.0).
3. **Archive V5** to `Omnix_V5-archive-2026-XX-XX/`.
4. **Update `My Goal for Omnix.md`** with the new state.
5. **Write `docs/V5_V6_MIGRATION_AUDIT.md` addenda** for any V5 defects fixed in V6.

**Done when:**

- V6.0.0 is tagged.
- V5 is archived.
- The user explicitly approves V6 as the new development target.

**Estimated time:** 4â€“8 hours.

---

## 17. Phase summary

| Phase | Scope | Time | Status |
|---|---|---|---|
| 0 | Audit | â€” | âœ… Complete |
| 0.5 | Skeleton boot | 4â€“8h | Awaiting approval |
| 1 | Core services + agent loop | 16â€“32h | â€” |
| 2 | Voice I/O | 12â€“20h | â€” |
| 3 | Desktop automation | 30â€“50h | â€” |
| 4 | Vision + screen understanding | 20â€“35h | â€” |
| 5 | Memory + context | 12â€“20h | â€” |
| 6 | AI brain maturity | 10â€“16h | â€” |
| 7 | Goal-oriented planning | 16â€“24h | â€” |
| 8 | UI (debug only) | 8â€“12h | â€” |
| 9 | Security hardening | 8â€“12h | â€” |
| 10 | Observability + Voice Subsystem | 8â€“12h | âœ… Complete (Voice added; original observability scope still open if needed) |
| 11 | **Full-system integration** (re-scoped) | n/a | âœ… Complete — see `V6_PHASE_11_IMPLEMENTATION_REPORT.md` |
| 11.5 | Performance + scaling (was §14) | 12â€“20h | Not started |
| 12 | Documentation + handoff | 12â€“16h | â€” |
| 13 | V6 release | 4â€“8h | â€” |
| **Total** | | **~180â€“300h** | |

---

## 18. Open questions for the user (for Phase 0.5 review)

1. **Confirm Python 3.13.15 is the target** (not 3.12 or 3.11).
2. **Confirm OpenRouter + Groq remain the AI providers** (vs. local-first).
3. **Confirm `frozen/` contents are out of scope** (UI, system_events, system_manager, skill generator, environment_state).
4. **Confirm PyQt6 is debug-only** (no user-facing panels in V6).
5. **Confirm `OMNIX_HEADLESS=1` and `OMNIX_QUIET_BOOT=1` are the only env-var gates.**
6. **Confirm security policy: API keys â†’ keyring in Phase 9** (not Phase 0.5).

---

## 19. Phase 0 sign-off

- [x] Roadmap on disk.
- [x] No code modified.
- [x] No phase started beyond Phase 0.

**PHASE 0 COMPLETE â€” NO SOURCE CODE MODIFIED. WAITING FOR APPROVAL TO BEGIN PHASE 0.5.**

