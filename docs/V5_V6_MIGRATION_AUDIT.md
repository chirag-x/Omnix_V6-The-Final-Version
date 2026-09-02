# V5 → V6 Migration Audit

**Phase:** 0 — Forensic Audit Before Migration
**Status:** Complete (no source code modified)
**Date:** 2026-08-29
**Author:** Claude (Phase 0 auditor)
**Reference codebase:** `E:\Coding\Omnix\Omnix_V5\` (340 Python files, 316 with content, 24 empty)
**Target codebase:** `E:\Coding\Omnix\Omnix_V6- The final version\` (285 Python files, **all** 0-byte placeholders)

---

## 1. Purpose

This document is the **single source of truth** for the migration from Omnix V5 to Omnix V6. It records, before any code is touched, exactly:

- what V5 contains,
- what V6 currently is (a structural skeleton),
- the deltas between them,
- the constraints under which migration must occur.

No code in either tree has been modified, copied, refactored, or "fixed" during this audit. Only read-only inspection and this report.

---

## 2. Methodology

Forensic inspection was carried out in the following order, always read-only:

1. **Top-level inventory** of both trees: file counts, package layout, empty vs. populated.
2. **Entry-point inspection** — V5 `main.py`, V5 `README.md`, V5 `requirements.txt`, V5 `.env`.
3. **Subsystem fan-out** — five parallel exploration agents mapped the V5 surface in depth:
   - **Agent A** — `main.py` + `core/` wiring (omnix_engine, services, capability_router, lifecycle, health, dependency manager, engine manager, compatibility).
   - **Agent B** — `ai/`, `automation/`, `context/`, `memory/`, `voice/`.
   - **Agent C** — `vision/` and `skills/`.
   - **Agent D** — `system/` and `tests/`.
   - **Agent E** — `core/agent/`, `core/planning/`, `core/events/`, `core/execution/`, `core/state/`, `core/utils/`, `core/compatibility/`.
4. **Dependency inspection** — both venvs, both `requirements.txt` (V5 only — V6 has none).
5. **Model/asset inspection** — `vision/models/`, `assets/`, `config/`, `logs/`, `users/`.
6. **Test inspection** — all 9 V5 test files reviewed for coverage of the agent loop, vision, routing, verification, recovery, real-Windows execution.
7. **Cross-cutting pattern extraction** — Service-wrapper pattern, Result-dataclass contract, "frozen" pattern, central `OmnixEngine` orchestrator, async-adapter Phase-21 pattern.

---

## 3. V5 — Reference Implementation

### 3.1 Top-level facts

| Item | Value |
|---|---|
| Files | 340 Python files |
| Files with content | 316 |
| Empty files | 24 (mostly `__init__.py` and `frozen/` stubs) |
| Python | 3.11.9 (`.venv`) |
| OS target | Windows 11 |
| UI toolkit | PyQt6 6.11.0 (declared, but live UI in `frozen/`) |
| ML stack present | Yes — 200+ pinned packages |
| YOLO model | `vision/models/yolo11n.pt` (5.6 MB, real weights) |
| Tests | 9 test files |

### 3.2 V5 package layout (live, non-empty)

```
Omnix_V5/
├── main.py                     # 314 lines — boot sequence, branded console
├── README.md
├── requirements.txt            # 217 lines, UTF-16 LE
├── .env                        # OpenRouter + Groq keys
│
├── core/                       # Central wiring + agent + planning
│   ├── omnix_engine.py         # 3637 lines, 49 methods, phase-annotated
│   ├── services/               # 8 services, each 895–1363 lines
│   │   ├── ai_service.py
│   │   ├── automation_service.py
│   │   ├── context_service.py
│   │   ├── memory_service.py
│   │   ├── skills_service.py
│   │   ├── ui_service.py
│   │   ├── vision_service.py
│   │   └── voice_service.py
│   ├── agent/                  # 9 files, ~9800 lines total
│   │   ├── agent_controller.py
│   │   ├── goal_executor.py
│   │   ├── goal_verifier.py
│   │   ├── observation_loop.py
│   │   ├── recovery_engine.py
│   │   ├── retry_manager.py
│   │   ├── step_verifier.py
│   │   ├── wait_engine.py
│   │   └── workflow_planner.py
│   ├── planning/               # 7 files
│   │   ├── intent_classifier.py
│   │   ├── command_processor.py
│   │   ├── task_planner.py
│   │   └── …
│   ├── events/                 # 4 files
│   │   ├── event_bus.py        # Phase 5
│   │   ├── event_types.py
│   │   └── …
│   ├── execution/              # 1 file — execution_status, normalize_result (Phase 6)
│   ├── state/                  # 4 files
│   ├── utils/                  # 5 files
│   ├── compatibility/          # 3 files
│   ├── capability_router.py
│   ├── dependency_manager.py
│   ├── engine_manager.py
│   ├── lifecycle_manager.py
│   └── health_monitor.py
│
├── system/                     # 100+ files, low-level desktop OS surface
│   ├── applications/           # ApplicationManager + discovery/cache/history/monitor
│   ├── automation/             # AutomationManager + ActionExecutor/WorkflowExecutor/Safety/Retry/Verification/Recovery
│   ├── browser/                # BrowserController (async facade)
│   ├── diagnostics/
│   ├── filesystem/
│   ├── input/                  # InputManager + AsyncInputAdapter (Phase 21)
│   ├── interfaces/
│   ├── memory/
│   ├── models/                 # Action, ActionResult, ExecutionResult, Workflow, Window, Application
│   ├── power/
│   ├── processes/
│   ├── scheduler/
│   ├── services/
│   ├── utils/
│   └── windows/                # WindowManager + focus/monitor/detector/finder/state/tracker
│
├── skills/                     # 35+ skill files
│   ├── built_in/               # applications, browser, files, input, media, system, vision
│   ├── core/                   # 6 files
│   ├── manager/                # 4 files
│   ├── capabilities/
│   ├── runtime/                # service_adapters.py
│   └── tests/
│
├── vision/                     # VisionManager + pipeline + observer + detection/hierarchy/summary
│   ├── vision_manager.py       # 247 lines
│   ├── vision_pipeline.py
│   ├── screen_observer.py      # 79 lines
│   ├── text_detector.py
│   ├── ui_detector.py
│   ├── element_locator.py
│   ├── screen_intelligence.py
│   ├── screen_summary.py
│   ├── vision_controller.py
│   ├── detection/              # 5 files
│   ├── models/                 # 5 files + yolo11n.pt (5.6 MB)
│   ├── hierarchy/              # 2 files
│   ├── summary/                # 2 files
│   ├── discovery/              # 2 files
│   └── utils/                  # 2 empty
│
├── voice/                      # VoiceManager + wake/sr/tts/audio
│   ├── voice_manager.py        # 1162 lines
│   ├── wake_listener.py
│   ├── speech_recognizer.py
│   ├── tts_engine.py
│   └── audio_utils.py
│
├── automation/                 # automation_engine.py (722 lines) — top-level public API
│
├── context/                    # context_manager.py (55 lines), screen_context.py
│
├── memory/                     # memory_coordinator.py (1068 lines) + manager/behavior/ui_pattern
│
├── ai/                         # brain_manager.py (232 lines)
│
├── utils/                      # cache_manager, constants, helpers, logger, performance_monitor
│
├── frozen/                     # Deprecated/excluded modules (enforced canonical-import policy)
│   ├── skills/                 # generator, memory, generated
│   ├── state/                  # environment_state.py
│   ├── system_events/
│   ├── system_manager.py
│   ├── system_ui/
│   └── ui/                     # developer_panel, main_window, notification_popup, settings_window, tray_icon, character/
│
├── tests/                      # 9 files (see §3.5)
│
├── users/                      # empty
├── assets/                     # empty (animations, icons, sounds)
├── config/                     # 4 empty JSON files
└── logs/                       # omnix.log (boot evidence)
```

### 3.3 V5 architectural patterns

| Pattern | Where | Migration implication |
|---|---|---|
| **Service wrapper** | `core/services/*Service.py` (8 services) | Each subsystem is wrapped with a typed `*Result` dataclass (`success`, `value`, `provider`, `operation`, `error`, `metadata`). Stable V6 contract. |
| **Result dataclass** | `core/services/*Service.py` | `AIResult`, `MemoryResult`, `AutomationResult`, `VisionResult`, `VoiceResult`, `SkillsResult`, `ContextResult`, `UIResult`. V6 must keep the same shape. |
| **Central orchestrator** | `core/omnix_engine.py` (3637 L, 49 methods, phase-annotated) | Initialize → wire → execute → normalize → publish events. **The single V6 boot path.** |
| **"Frozen" pattern** | `frozen/` | Deprecated code lives in a directory that is excluded from canonical imports. V6 has no `frozen/` — see §4.4. |
| **Async adapter (Phase 21)** | `system/input/async_adapter.py`, `system/browser/browser_controller.py` | Synchronous desktop APIs are wrapped by thin `async` facades. V6 must keep this. |
| **Event bus (Phase 5)** | `core/events/event_bus.py` | Used by voice path and the agent loop. V6 must keep. |
| **Result normalization (Phase 6)** | `core/execution/execution_status.py` | `normalize_result` collapses skill/executor return shapes into a canonical `ExecutionStatus` enum. V6 must keep. |
| **StepVerifier / GoalVerifier** | `core/agent/step_verifier.py`, `core/agent/goal_verifier.py` | Phase 11/19. Step-level + goal-level verification gates. V6 must keep. |
| **Recovery / Retry** | `core/agent/recovery_engine.py`, `system/automation/retry_manager.py` | RETRY + ALTERNATIVE strategies. V6 must keep. |
| **WorkflowPlanner** | `core/agent/workflow_planner.py` | Step DAG construction. V6 must keep. |
| **`OMNIX_HEADLESS` / `OMNIX_QUIET_BOOT`** | `main.py` + tests | Env-var gates. V6 must keep. |

### 3.4 V5 dependencies (`requirements.txt`, 217 lines)

Pinned, top-tier:

- `python 3.11.9` (venv interpreter)
- `torch==2.11.0+cu128`, `torchaudio==2.11.0+cu128`, `torchvision==0.26.0+cu128`
- `ultralytics==8.4.21`, `ultralytics-thop==2.0.18`
- `opencv-python==4.13.0.92`, `opencv-python-headless==5.0.0.93`
- `faster-whisper==1.2.1`, `easyocr==1.7.2`, `edge-tts==7.2.7`
- `sentence-transformers==5.2.3`, `transformers==5.3.0`, `huggingface_hub==1.6.0`
- `pywin32==311`, `PyAutoGUI==0.9.54`, `keyboard==0.13.5`, `mouse==0.7.1`
- `selenium==4.41.0`, `chromadb==1.5.2`, `openwakeword==0.6.0`
- `onnxruntime==1.24.3`, `mss==10.1.0`, `sounddevice==0.5.5`
- `openai==2.26.0`, `requests==2.32.5`, `httpx==0.28.1`
- `psutil==7.2.2`, `pygetwindow==0.0.9`, `comtypes==1.4.16`
- `pydantic==2.12.5`, `loguru==0.7.3`
- `SpeechRecognition==3.14.5`, `pygame==2.6.1`
- `PyQt6==6.11.0`, `PyQt6-Qt6==6.11.1`
- `faiss-cpu==1.13.2`, `scikit-learn==1.8.0`, `numpy==2.4.2`
- `pytest==9.1.1`, `rich==14.3.3`, `pydantic_core==2.41.5`

See `docs/V6_DEPENDENCY_PLAN.md` for the full V6 dependency plan and Python 3.13.15 compatibility assessment.

### 3.5 V5 test files (9)

| File | Phase | Style | Coverage |
|---|---|---|---|
| `test_connected_loop.py` | 10 | pytest (`test_*` functions) + MockBrain/MockMemory/MockScreen/MockAutomation | End-to-end engine wiring with mocks; multi-step agent loop |
| `test_phase16_uia_smoke.py` | 16 | `main()` script (sets `OMNIX_HEADLESS=0`) | Real UIA enumeration; foreground Chrome; address bar detection |
| `test_pipeline.py` | — | module-level script, no `if __name__` | Intent → command processor → task planner pipeline |
| `test_real_execution.py` | 17 | `main()` script, 5 cases | Real Windows launches (psutil-validated); Notepad clipboard readback; step_trace |
| `test_real_loop.py` | 11 | `main()` script | Closed-loop trace VOICE→…→VOICE; goal verification; replan |
| `test_routing_reliability.py` | 12 | `main()` script, 7 cases | Intent/plan/automation routing; compound command collapse; edge cases (e.g. "open SkynetTerminal9000") |
| `test_ui_compound_reliability.py` | 15 | `main()` script, 5 cases | Compound UI actions; dependent-step blocking; address-bar/play-media |
| `test_verification_recovery.py` | 11 | pytest + StubStep/StubGoal/StubRecovery/FakeSkill | StepVerifier/GoalVerifier/RecoveryEngine; replan budget; UNCERTAIN downgrade |
| `test_vision_action_reliability.py` | 16b | `main()` script, 5 cases | Real vision+click; 8-state ladder (APP_NOT_RUNNING … GOAL_VERIFIED) |

---

## 4. V6 — Target Skeleton

### 4.1 Top-level facts

| Item | Value |
|---|---|
| Files | 285 Python files |
| Files with content | **0** (all 285 are 0-byte placeholders) |
| Python | 3.13.15 (`.venv`) |
| ML stack | **None** (venv has only pip, setuptools, wheel, packaging) |
| YOLO model | `vision/models/yolo11n.pt` (0 bytes — placeholder) |
| Tests | 0 |
| `frozen/` | absent |
| `users/` | absent |
| `assets/` | empty subdirs |
| `config/` | empty |
| `logs/` | empty |

### 4.2 V6 package layout (skeleton, 0 bytes each)

Same shape as V5 except:

- No `frozen/`
- No `users/`
- No `skills/runtime/`
- No `vision/utils/`
- No test files

The V6 layout was created structurally — it has the right directory names but no implementations.

### 4.3 V6 venv state

```
.venv/
├── pip
├── setuptools
├── wheel
└── packaging
```

No torch, no ultralytics, no opencv, no faster-whisper, no edge-tts, no pywin32, no PyAutoGUI, no Selenium, no pydantic (≥2), no loguru, no PyQt6.

---

## 5. Deltas (V5 → V6)

| # | Area | V5 | V6 | Migration required |
|---|---|---|---|---|
| 1 | `core/omnix_engine.py` | 3637 L, working | 0 bytes | **Rewrite** from V5 + add Phase 0.5 invariants |
| 2 | `core/services/*` (8) | 895–1363 L each | 0 bytes | **Rewrite** from V5 (preserve Service wrapper contract) |
| 3 | `core/agent/*` (9) | ~9800 L total | 0 bytes | **Rewrite** from V5 (preserve loop, verification, recovery) |
| 4 | `core/planning/*` (7) | populated | 0 bytes | **Rewrite** from V5 |
| 5 | `core/events/*` (4) | populated | 0 bytes | **Rewrite** from V5 |
| 6 | `core/execution/*` (1) | populated | 0 bytes | **Rewrite** from V5 |
| 7 | `core/state/*` (4) | populated | 0 bytes | **Rewrite** from V5 |
| 8 | `core/utils/*` (5) | populated | 0 bytes | **Rewrite** from V5 |
| 9 | `core/compatibility/*` (3) | populated | 0 bytes | **Rewrite** from V5 |
| 10 | `system/*` (100+ files) | populated | 0 bytes | **Rewrite** from V5 (preserve async adapter pattern) |
| 11 | `skills/built_in/*`, `skills/core/*`, `skills/manager/*` | populated | 0 bytes | **Rewrite** from V5 |
| 12 | `vision/*` (incl. `models/yolo11n.pt`) | populated, model real | 0 bytes | **Rewrite** from V5; **download weights** in Phase 0.5 |
| 13 | `voice/*` | populated | 0 bytes | **Rewrite** from V5 |
| 14 | `automation/automation_engine.py` | 722 L | 0 bytes | **Rewrite** from V5 |
| 15 | `context/*` | populated | 0 bytes | **Rewrite** from V5 |
| 16 | `memory/*` | populated | 0 bytes | **Rewrite** from V5 |
| 17 | `ai/brain_manager.py` | 232 L | 0 bytes | **Rewrite** from V5 |
| 18 | `utils/*` | populated | 0 bytes | **Rewrite** from V5 |
| 19 | `frozen/` | populated (deprecated) | absent | **Do not** recreate in V6. Frozen pattern is enforced by absence + linter rule. |
| 20 | `tests/` (9 files) | populated | absent | **Recreate** (see `docs/V6_TEST_MIGRATION_PLAN.md`) |
| 21 | `requirements.txt` | 200+ pins | absent | **Recreate** in Phase 0.5 (see `docs/V6_DEPENDENCY_PLAN.md`) |
| 22 | `.env` | OpenRouter + Groq keys | absent | **Recreate** with same shape, keys in Phase 0.5 |
| 23 | `main.py` | 314 L, branded boot | absent | **Recreate** from V5 (preserve boot sequence) |
| 24 | `assets/`, `config/`, `logs/`, `users/` | empty | empty | None (recreate at runtime / via scripts) |
| 25 | `vision/models/yolo11n.pt` | 5.6 MB (real) | 0 bytes | **Download** in Phase 0.5 |

**Net effect:** V6 is currently a **structural skeleton only**. Every populated V5 file must be reproduced in V6 (or its replacement) before any feature can be exercised.

---

## 6. Risks and known issues (carried from V5)

These are V5 defects observed during read-only audit. They are **not** to be fixed in Phase 0. They are flagged here so Phase 0.5+ can decide whether to keep the behavior, fix the bug, or redesign.

| # | Where | Issue | Severity | Action |
|---|---|---|---|---|
| R1 | `system/automation/workflow_executor.py` | Builds local `queue = ActionQueue()`, enqueues into it, but the `while not self._queue.is_empty` loop dequeues from `self._queue` (the manager's queue), not the local one. Local `queue` is unused after construction. | **High (latent bug)** | Decide in Phase 0.5: fix or preserve + comment. |
| R2 | `system/automation/recovery_manager.py` | `recover` returns the raw exception message; `rollback` always returns `False`. Module is essentially a placeholder. | Medium | Phase 0.5: design real recovery. |
| R3 | `system/automation/verification.py` | `verify(action, result) -> result.success`. No vision/OCR integration despite the docstring hint. | Medium | Phase 0.5: integrate with `vision_manager`. |
| R4 | `system/automation/safety_manager.py` | Blocked-actions set; `validate(action)` is a `bool` only. No policy DSL. | Low | Phase 0.5: optional policy DSL. |
| R5 | `system/applications/application_manager.py` | Uses `threading.Lock`; other system managers do not. Inconsistent locking model. | Low | Phase 0.5: standardize on `RLock` or async ownership. |
| R6 | `tests/` | Mix of pytest-style (`test_connected_loop`, `test_verification_recovery`) and `main()`-style scripts (7 of 9). | Low | Phase 0.5: standardize to pytest. |
| R7 | `main.py` | `print_result` checks `type(result).__name__ == "AIResult"` (stringly-typed) instead of `isinstance`. | Low | Phase 0.5: use `isinstance(result, AIResult)`. |
| R8 | `core/omnix_engine.py` | 3637 L, 49 methods, phase-annotated — but the phase annotations are comments. No enforced phase boundaries. | Low | Phase 0.5: linter rule. |
| R9 | `frozen/` | Excluded by directory; not by `sys.path` manipulation. A determined import can still pull from it. | Low | Phase 0.5: add `pyproject.toml`/`setup.cfg` rule or `__init__.py` block. |
| R10 | `config/*.json` | 4 empty JSON files. No schemas. | Low | Phase 0.5: define JSON schemas. |
| R11 | `logs/omnix.log` | Boot evidence; not rotated. | Low | Phase 0.5: add rotation policy. |
| R12 | `.env` | 4 OpenRouter + 1 Groq key in plaintext. | High (security) | Phase 0.5: secrets manager (or at least chmod + keyring). |

---

## 7. Migration principles (binding for Phase 0.5+)

1. **Service contract is sacred.** Every `core/services/*Service.py` keeps the same `Result` dataclass shape (`success`, `value`, `provider`, `operation`, `error`, `metadata`). Callers depend on this.
2. **Central orchestrator is sacred.** `core/omnix_engine.py` remains the single boot path. No subsystem may bypass it.
3. **Async adapter pattern is sacred.** Synchronous desktop APIs remain sync; the async facade is a thin wrapper. No new sync logic inside `async def` bodies.
4. **Verification gates are sacred.** StepVerifier + GoalVerifier must remain in the loop. No silent success.
5. **The frozen pattern is preserved as absence.** V6 has no `frozen/`. If something must be deprecated, delete it (with a recorded rationale in `docs/V5_V6_MIGRATION_AUDIT.md` addenda) — do not re-create the directory.
6. **`OMNIX_HEADLESS` and `OMNIX_QUIET_BOOT` are sacred env vars.** Any test or boot path that needs to suppress hardware access uses these. No other env-var names for the same purpose.
7. **Result normalization is sacred.** `core.execution.normalize_result` is the single point that maps skill/executor returns into `ExecutionStatus`. No inline mapping elsewhere.
8. **No silent fallback.** If a subsystem cannot start, the engine must surface a typed error — not a degraded `success=True`. The user explicitly asked for honest reporting in `My Goal for Omnix.md` §7.

---

## 8. What is NOT covered by this audit

- AI model behavior (LLM outputs, intent classification accuracy, plan quality).
- Runtime performance under load.
- Memory footprint of the agent loop.
- Windows-version-specific behavior (Win10 vs Win11).
- Real-hardware reliability (microphone, GPU, UIA differences between Windows builds).

These are Phase 1+ concerns and are tracked separately.

---

## 9. Phase 0 deliverables (this audit)

This document plus:

- `docs/V5_V6_FILE_MAP.md` — file-by-file V5 → V6 mapping.
- `docs/V6_DEPENDENCY_PLAN.md` — Python 3.13.15 + dependency compatibility.
- `docs/V6_ARCHITECTURE_RULES.md` — V6 architecture invariants.
- `docs/V6_MODEL_PLAN.md` — which AI models, where they live.
- `docs/V6_TEST_MIGRATION_PLAN.md` — V5 test → V6 test plan.
- `docs/V6_PHASE_ROADMAP.md` — Phase 0.5 through Phase 13.

---

## 10. Phase 0 sign-off

- [x] No source code modified in V5.
- [x] No source code modified in V6.
- [x] No V5 file copied into V6.
- [x] No package installed/uninstalled in either venv.
- [x] No AI model downloaded.
- [x] No Python version changed.
- [x] No test run.
- [x] No bug "fix" applied.
- [x] All 7 required documentation files created under `E:\Coding\Omnix\Omnix_V6- The final version\docs\`.

**PHASE 0 COMPLETE — NO SOURCE CODE MODIFIED. WAITING FOR APPROVAL TO BEGIN PHASE 0.5.**
