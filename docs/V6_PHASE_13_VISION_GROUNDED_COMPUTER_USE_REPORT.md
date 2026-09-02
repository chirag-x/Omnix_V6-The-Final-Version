# OMNIX V6 — PHASE 13: VISION-GROUNDED COMPUTER USE

**Status:** PHASE 13 COMPLETE — Vision is a first-class, typed V6
grounding component.  Vision **observes** the world; it never **acts**
on it.  All computer actions still flow through the closed capability
set (`desktop.mouse.*` / `desktop.keyboard.*`) via the canonical
`CapabilityRouter`.

**Scope:** Make the existing V6 vision subsystem a **typed, safe, and
deterministic** grounding layer for native computer use.  Phase 13
**extends** what Phases 7.x and 11.5 already built; it does **not**
replace any subsystem and does **not** create a second Brain, Agent,
Engine, or VisionService.

**Date:** 2026-08-31

---

## 1. Audit — what already existed in V6 (before Phase 13)

The Phase 13 specification describes an architecture that is **already
largely in place** in V6 thanks to Phases 7.x, 11.5, and 12.  This
audit documents exactly what is already present, so Phase 13 only
adds the small, focused contracts and gates the spec demands.

### 1.1 Vision is a separate subsystem (R-14)

`core/services/vision_service.py` is a service, not a singleton.
Every consumer instantiates its own; the Agent takes it via the
`vision_service=` keyword.

### 1.2 Screenshot pipeline reused (R-21, R-22)

`vision/router/screenshot_provider.py` defines the
`ScreenshotProvider` Protocol with two implementations:

* `CapabilityScreenshotProvider` — calls the closed
  `desktop.screenshot` capability.  This is the canonical
  computer-use path; vision never acquires a screenshot through any
  other seam.
* `NullScreenshotProvider` — for headless / CI / tests.

`vision_service.py:VisionService._maybe_capture_screenshot()` is
lazy: it acquires a screenshot *only* when a strategy that needs it
is in play, and *only* when the first grounding attempt did not
succeed without a screenshot.

### 1.3 Typed observation contract

`vision/observations/targets.py` defines `TargetCandidate` and
`GroundedTarget` (frozen dataclasses) and `core/orchestration/
grounding.py` defines the typed boundary between vision and the
Agent — `TargetGroundingContract` with statuses `GROUNDED`,
`AMBIGUOUS`, `NOT_FOUND`, `ERROR`, `REJECTED`, `SKIPPED`.

### 1.4 Target grounding is adaptive + deterministic (R-22)

`vision/router/perception_router.py:PerceptionRouter` ranks
strategies by a fixed reliability map:

| Source       | Reliability |
|--------------|-------------|
| UIA          | 0 (most reliable) |
| DERIVED      | 1 |
| OCR          | 2 |
| VISION (YOLO)| 3 |
| SCREEN       | 4 (least reliable) |

Candidates are sorted by `(source_reliability, -confidence, bbox)`;
ties raise `AmbiguityError`.  No LLM in the grounding loop.

### 1.5 Vision does NOT call any action (R-21)

`vision/*` and `core/services/vision_service.py` import **none** of
`pyautogui`, `subprocess`, `win32gui`, `win32api`, `ctypes`, or
`core.capability_router`.  Phase 13 hardens this with AST-level
isolation tests.

### 1.6 Closed capability set is the only path to execution (R-21)

`core/orchestration/vision_adapter.py:adapt_pre_action()` is the
*only* place that turns a `TargetGroundingContract` into an
`ActionRequest`.  It dispatches to `desktop.mouse.click`,
`desktop.mouse.double_click`, `desktop.mouse.right_click`,
`desktop.mouse.move`, `desktop.keyboard.type`, `desktop.keyboard.press`,
`desktop.keyboard.hotkey` — every one of which is in the closed
registry.

### 1.7 Agent integration is already wired (R-22)

`core/orchestration/agent.py:_apply_pre_action_grounding()` is the
*only* place the Agent consults vision.  It (a) returns
`TargetGroundingContract.skipped()` when no `vision_pre_action`
metadata is set, (b) blocks the step when confidence is below
`self.confidence_threshold` (default `0.5`), and (c) routes
`AMBIGUOUS` / `NOT_FOUND` / `ERROR` / `REJECTED` to recovery.

### 1.8 Before/after verification (R-8)

`core/orchestration/verifier.py:DefaultStepVerifier.
_verify_vision_effect()` already handles `vision_observed`,
`vision_disappeared`, `vision_changed`.  A PASSED verdict requires a
non-None diff (`"target appeared"` or `"target changed"`); an
UNCERTAIN verdict is returned for missing observations, never a
silent PASS.

### 1.9 Bounded recovery (R-22)

`core/orchestration/recovery.py:RecoveryPolicy` enforces
`max_attempts_per_step=2`, `max_replans=2`,
`max_total_runtime_s=120.0`.  Phase 13 inherits this policy
unchanged.

### 1.10 Coordinate safety — partially present

The vision adapter already validates that a `TargetGroundingContract`
is `GROUNDED` and has a center.  Phase 13 hardens this with explicit
coordinate-safety validation (finite, in-bounds, fresh screenshot,
known source) and a `stale_screen` rejection path.

---

## 2. Gap analysis — what Phase 13 must add

The audit identified five concrete gaps the spec demands that the
existing V6 does not yet enforce explicitly.  Phase 13 fills each
gap with a small, focused addition; it does not duplicate any
existing module.

| Gap | Where it lives now | Phase 13 addition |
| --- | --- | --- |
| Typed `ScreenshotMetadata` (screenshot_id, timestamp, dimensions, monitor) | `ScreenshotProvider.capture(path=None) -> Optional[str]` returns only a path string. | `vision/observations/screenshot_metadata.py` — frozen dataclass with id, timestamp, image_width, image_height, monitor_id, source, path. |
| Typed `VisualObservation` distinct from `TargetGroundingContract` | `core.orchestration.models.Observation` exists; nothing strictly visual. | `vision/observations/visual_observation.py` — frozen dataclass extending the observation payload with `subject`, `bbox`, `source`, `screenshot_metadata`. |
| Coordinate safety validation | `vision_adapter._validate_grounded()` only checks status + center. | `vision/safety/coordinates.py` — `validate_coordinates()` rejects non-finite, out-of-bounds, or stale coordinates. |
| Stale-screen protection | None. | `vision/safety/freshness.py` — `is_fresh(screenshot_metadata, now, max_age_s)`. |
| Configurable confidence threshold | `Agent(..., confidence_threshold=...)` exists; not in `OmnixConfig`. | `OmnixConfig` gains `vision_confidence_threshold`; `Agent` reads it from config. |
| Visual `ExpectedEffect` extensions | `ExpectedEffect` is generic; only one check_name branch. | `ExpectedEffect.check_name` extended set documented; verifier dispatches on it. |
| VisionTargetProvider seam for the Agent | Agent uses `vision_service=` (Any). | `vision/integration/agent_provider.py` — typed `VisionTargetProvider` Protocol + `DefaultVisionTargetProvider` adapter over `VisionService`. |
| Multi-monitor / DPI documentation | None. | Documented in the report; UIA is monitor-aware; `desktop.screenshot` reports the primary; Phase 13 refuses to act on targets outside the screenshot's reported bounds. |
| Phase 13 deterministic tests | Existing `test_phase7_2_vision_agent_integration.py`. | `tests/test_phase13_vision_grounded_computer_use.py`. |
| AST isolation tests | Existing `test_brain_isolation.py` pattern. | `tests/test_phase13_vision_isolation.py`. |
| End-to-end no-real-mouse test | None. | `tests/test_phase13_e2e_no_real_mouse.py`. |
| Real-Windows smoke | `scripts/phase12_real_windows_smoke.py` pattern. | `scripts/phase13_real_windows_smoke.py`. |

---

## 3. Phase 13 design

### 3.1 Architecture

Phase 13 extends the existing V6 vision path:

```
User
 ↓
Intent
 ↓
Goal
 ↓
Brain (Planner)                  ← planner may emit steps with
 ↓                                  metadata["vision_pre_action"]
Plan
 ↓
Agent._execute_plan              ← R-22: closed loop orchestrator
   │
   ├── _apply_pre_action_grounding       ← R-22: vision is the only
   │     │                                  source of "where"
   │     ▼
   │   VisionTargetProvider.ground_target()
   │     │   ┌──────────────────────────────┐
   │     │   │ VisionService (existing)     │
   │     │   │   ├─ PerceptionRouter        │
   │     │   │   ├─ UIAStrategy             │
   │     │   │   ├─ OCRStrategy             │
   │     │   │   ├─ VisualStrategy (YOLO)   │
   │     │   │   └─ CoordinatesStrategy     │
   │     │   └──────────────────────────────┘
   │     │
   │     │   Lazy screenshot via
   │     │   ScreenshotProvider (desktop.screenshot capability)
   │     │
   │     ▼
   │   ScreenshotMetadata + VisualObservation
   │   TargetGroundingContract (existing)
   │   confidence ≥ threshold            ← Phase 13: configurable
   │   coordinate safety validation     ← Phase 13: NEW
   │   fresh-screenshot gate            ← Phase 13: NEW
   │
   ├── vision_adapter.adapt_pre_action()   ← R-21: only path to action
   │
   ├── PlanStep replaced with adapted
   │     capability_name, parameters,
   │     expected_effect, metadata
   │
   ├── PlanExecutor → CapabilityRouter → desktop.mouse.click (etc.)
   │
   ├── Observation → DefaultStepVerifier._verify_vision_effect
   │                  (R-8: tri-state verdict; UNCERTAIN != PASS)
   │
   ├── RecoveryEngine (bounded)
   │
   └── OmnixResponse
```

### 3.2 Vision is OBSERVATION, not action (R-21 enforcement)

Phase 13 adds `vision/safety/` modules that the existing
`VisionService` and the new `VisionTargetProvider` consult.  These
modules are pure functions over already-grounded data; they do not
import any action surface.

### 3.3 No screenshot logging / retention

The new `ScreenshotMetadata` does not include the screenshot's
*image bytes* and the project never writes screenshot files to
disk unless the capability is invoked explicitly with `path=...`.
Vision never reads the screenshot path back as if it were state.

### 3.4 Configurable confidence threshold

`OmnixConfig` gains `vision_confidence_threshold: float = 0.5`.
The engine reads it at boot and passes it to the Agent.  The
existing `Agent(confidence_threshold=...)` keyword is unchanged;
configuration is the only new wiring.

### 3.5 Screenshot metadata contract

`vision/observations/screenshot_metadata.py:ScreenshotMetadata` is a
frozen dataclass:

```python
@dataclass(frozen=True)
class ScreenshotMetadata:
    screenshot_id: str
    timestamp: float
    image_width: int
    image_height: int
    monitor_id: Optional[str] = None
    source: str = "desktop.screenshot"
    path: Optional[str] = None
```

The `ScreenshotProvider.capture()` signature is widened to
`capture() -> Optional[ScreenshotMetadata]` while keeping a
backwards-compatible `capture_path_only() -> Optional[str]` alias.
Existing callers that only need the path keep working.

### 3.6 Visual observation contract

`vision/observations/visual_observation.py:VisualObservation` is a
frozen dataclass:

```python
@dataclass(frozen=True)
class VisualObservation:
    subject: str
    bbox: Optional[Tuple[int, int, int, int]] = None
    center: Optional[Tuple[int, int]] = None
    confidence: float = 0.0
    source: str = ""
    status: str = "OBSERVED"  # OBSERVED | AMBIGUOUS | NOT_FOUND | ERROR
    resolution_method: str = ""
    screenshot_metadata: Optional[ScreenshotMetadata] = None
    candidates: Tuple[Dict[str, Any], ...] = ()
    error: str = ""
```

This is the *post-action* observation; the Agent / verifier consult
it after the executor has dispatched the step.  It is intentionally
distinct from `TargetGroundingContract` (which is a *pre-action*
assertion about where a target is).

### 3.7 Coordinate safety

`vision/safety/coordinates.py:validate_coordinates()`:

* Coordinates must be `(int, int)`.
* Each value must be in `[0, image_width)` × `[0, image_height)`.
* Both must come from a `ScreenshotMetadata` whose `timestamp` is
  within `max_stale_s` of "now".
* The originating `source` must be a known source (`UIA`, `OCR`,
  `DERIVED`, `VISION`, `SCREEN`).

A failure raises `CoordinateSafetyError` (a `ValueError` subclass)
and the Agent routes the step to recovery.

### 3.8 Stale-screen protection

`vision/safety/freshness.py:is_fresh()`:

```python
def is_fresh(meta: ScreenshotMetadata, *, now: float, max_age_s: float) -> bool:
    if meta is None:
        return False
    if not isinstance(meta.timestamp, (int, float)):
        return False
    return (now - meta.timestamp) <= max_age_s
```

Default `max_age_s = 5.0` is read from `OmnixConfig`.  When the
screenshot is stale, `CoordinateSafetyError` is raised and the Agent
returns a SAFETY failure.

### 3.9 VisionTargetProvider seam

`vision/integration/agent_provider.py` defines:

```python
class VisionTargetProvider(Protocol):
    def ground_target(
        self,
        target_query: str,
        *,
        preferred_strategy: Optional[str] = None,
    ) -> TargetGroundingContract: ...


class DefaultVisionTargetProvider:
    """Wraps a VisionService and returns TargetGroundingContracts."""
    def __init__(self, vision_service, *, max_stale_s: float = 5.0) -> None: ...
```

The `Agent` continues to accept `vision_service=` (backwards
compatible) but the engine's `_build_pipeline` constructs a
`DefaultVisionTargetProvider` first.  This isolates the Agent from
the vision service class entirely — the Agent sees only the
protocol.

### 3.10 Multi-monitor and DPI

Phase 13 documents and enforces:

* `ScreenshotMetadata.image_width` / `image_height` are the
  captured image's pixel dimensions.
* The UIA strategy returns *logical* coordinates in the OS coordinate
  system.  When a screenshot is captured with DPI scaling, the
  UIA-returned `bbox` may not equal the screenshot pixel
  coordinates.  Phase 13 treats the screenshot as the source of
  truth for "is this point on screen" and rejects targets whose
  center is outside the screenshot's reported dimensions.
* Multi-monitor: a target with `monitor_id=None` is assumed to be
  on the primary monitor.  A target with a non-None `monitor_id`
  must match the screenshot's `monitor_id` (or the screenshot must
  be `monitor_id=None`); mismatched monitors are rejected.

---

## 4. Files added or extended (Phase 13)

| File | Type | Purpose |
| --- | --- | --- |
| `vision/observations/screenshot_metadata.py` | NEW | `ScreenshotMetadata` frozen dataclass. |
| `vision/observations/visual_observation.py` | NEW | `VisualObservation` frozen dataclass. |
| `vision/safety/__init__.py` | NEW | Package marker. |
| `vision/safety/coordinates.py` | NEW | `validate_coordinates()`, `CoordinateSafetyError`. |
| `vision/safety/freshness.py` | NEW | `is_fresh()`. |
| `vision/integration/__init__.py` | NEW | Package marker. |
| `vision/integration/agent_provider.py` | NEW | `VisionTargetProvider` Protocol + `DefaultVisionTargetProvider`. |
| `vision/router/screenshot_provider.py` | EXTEND | `ScreenshotProvider.capture()` now returns `Optional[ScreenshotMetadata]`; old `capture_path_only()` kept for back-compat. |
| `core/services/vision_service.py` | EXTEND | `VisionService.ground_target()` and `observe_state()` now return `VisualObservation` (with `ScreenshotMetadata`); the existing `VisionResult` dataclass is preserved as the inner `VisionResult` returned by helper `ground_target_legacy()` for back-compat. |
| `core/configuration.py` | EXTEND | New fields: `vision_confidence_threshold`, `vision_max_screenshot_stale_s`. |
| `core/omnix_engine.py` | EXTEND | Reads `vision_confidence_threshold` from config; constructs `DefaultVisionTargetProvider` and passes it to the Agent. |
| `core/orchestration/agent.py` | EXTEND | `vision_service` keyword is now typed as `VisionTargetProvider` (backwards compatible).  `_apply_pre_action_grounding()` calls `is_fresh()` on the contract's screenshot metadata. |
| `tests/test_phase13_vision_grounded_computer_use.py` | NEW | ≥ 20 deterministic tests. |
| `tests/test_phase13_vision_isolation.py` | NEW | AST-level isolation: no `subprocess` / `pyautogui` / `win32gui` / `win32api` / `ctypes` / `core.capability_router` / `core.omnix_engine` imports from `vision/*`. |
| `tests/test_phase13_e2e_no_real_mouse.py` | NEW | End-to-end through the Agent with a mock vision service and a mock executor. |
| `scripts/phase13_real_windows_smoke.py` | NEW | Real-Windows smoke; opt-in; tests selectable via `--tests`. |
| `docs/V6_PHASE_13_VISION_GROUNDED_COMPUTER_USE_REPORT.md` | NEW | This report. |
| `docs/V6_REVISED_ROADMAP.md` | UPDATE | Mark Phase 13 complete; pointer to this report. |

No files in `core/capability_router.py`, `core/capability.py`, or
`core/orchestration/agent.py` were rewritten.  Every extension is
backwards compatible.

---

## 5. Safety boundaries (Phase 13, explicit)

* Vision is **observation only**.  It never imports or calls
  `pyautogui`, `subprocess`, `win32gui`, `win32api`, `ctypes`,
  `os.system`, `os.popen`.  Enforced by AST isolation tests.
* Computer actions flow through the closed capability set only.
  The vision adapter is the **only** place a target becomes a
  `desktop.mouse.*` or `desktop.keyboard.*` capability call.
* `main.py` is unchanged.  No vision code is injected into the user
  front door.
* The `Brain` is unchanged.  No new intent kinds, no new plans
  inside the brain — vision-grounding metadata is set by the
  planner when an intent demands it.
* The `Engine` does not import `vision`.  It reads config, wires
  the `DefaultVisionTargetProvider`, and hands the provider to the
  `Agent`.  The engine remains a thin orchestrator.

---

## 6. Tests added (Phase 13)

* `tests/test_phase13_vision_grounded_computer_use.py` — 24
  deterministic tests covering: `ScreenshotMetadata` contract,
  `VisualObservation` contract, coordinate safety validation
  (finite / in-bounds / source / multi-monitor mismatch),
  freshness (stale / fresh / missing), `VisionTargetProvider`
  protocol and default implementation, `OmnixConfig` new fields,
  Agent integration with the provider, and the
  `DefaultStepVerifier._verify_vision_effect` tri-state behaviour.
* `tests/test_phase13_vision_isolation.py` — AST-level check that
  every `vision/**/*.py` file does not import the forbidden action
  surface.
* `tests/test_phase13_e2e_no_real_mouse.py` — a single end-to-end
  test that wires a mock vision service, a mock
  `CapabilityRouter`, and asserts that the Agent:
    1. consults vision before clicking;
    2. routes a low-confidence grounding to recovery;
    3. succeeds on a grounded target;
    4. routes an ambiguous grounding to recovery;
    5. never calls a non-closed capability.

Total: 24 + (8 isolation) + (1 e2e) = 33 new Phase 13 tests.

---

## 7. Definition of done (Phase 13)

* [x] Audit of existing V6 vision architecture completed and
      documented in this report (Section 1, 2).
* [x] `ScreenshotMetadata` typed contract added.
* [x] `VisualObservation` typed contract added (distinct from
      `TargetGroundingContract`).
* [x] `validate_coordinates()` rejects non-finite / out-of-bounds /
      stale / mismatched-monitor coordinates.
* [x] `is_fresh()` enforces a configurable max age.
* [x] `OmnixConfig` gains `vision_confidence_threshold` and
      `vision_max_screenshot_stale_s`; engine wires them.
* [x] `VisionTargetProvider` Protocol + `DefaultVisionTargetProvider`
      exist; Agent accepts them via the existing `vision_service=`
      keyword.
* [x] Multi-monitor / DPI rejection documented and enforced.
* [x] ≥ 20 deterministic Phase 13 tests; isolation tests; e2e test
      (Section 6).
* [x] Real-Windows smoke script
      (`scripts/phase13_real_windows_smoke.py`).
* [x] V5 source code audit: no V5 code in Phase 13 paths.
* [x] Roadmap updated to mark Phase 13 complete.
* [x] No security boundaries weakened.  No forbidden imports added
      to `vision/`.
* [x] `main.py` is unchanged.

---

## 8. What Phase 13 is NOT (explicit non-goals)

* **NOT** Phase 14.  No final hardening, no bug-fixing campaign.
* **NOT** a redesign.  Every change is an extension to an existing
  V6 module or a new focused module under `vision/`.
* **NOT** a new `Brain`, `Agent`, `Engine`, or `VisionService`.
* **NOT** a `pyautogui` integration.  The mouse and keyboard
  capabilities continue to use the existing
  `system.input.input_service` / `WindowsInputService`.
* **NOT** an LLM-in-the-verification-loop.  R-22 is preserved.
* **NOT** a screenshot retriever.  Screenshots are produced by the
  closed `desktop.screenshot` capability only.
* **NOT** a `main.py` change.  The user front door is untouched.

---

## 9. Stop condition

`PHASE 13 COMPLETE — VISION-GROUNDED COMPUTER USE VALIDATED. READY
FOR PHASE 14.`
