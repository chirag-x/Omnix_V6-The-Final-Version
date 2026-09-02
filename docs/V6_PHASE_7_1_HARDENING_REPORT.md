# Phase 7.1 Vision Integration Hardening Report

## Overview
This report summarizes the work completed for Phase 7.1 Vision Integration Hardening in the Omnix V6 project. The goal was to harden the vision service to comply with the architectural constraints of V6, ensuring that the VisionService acts purely as an observation service without executing actions, and adhering to strict rules regarding nondeterminism, lazy screenshot acquisition, and separation of concerns.

## Architectural Constraints Addressed
The following constraints from the plan were enforced:

- **R-14**: VisionService is a service, not a singleton.
- **R-21**: Vision depends on the closed capability set only through the `ScreenshotProvider` seam; no direct imports of `OmnixEngine` or direct capability calls.
- **R-22**: Routing is adaptive but deterministic; ambiguity must be surfaced explicitly (no silent tie-breaking).
- **R-8**: `observe_state` returns observation, never verification; the service never claims `verified=True` from a single screenshot.
- Additional constraints: No LLM inside Vision, YOLO is evidence not semantic understanding, decoupled screenshot acquisition.

## Key Changes Made

### 1. Fixed Syntax Errors (Unclosed Docstrings)
Multiple files in the `vision/` module and `core/services/vision_service.py` had missing closing triple quotes (`"""`). These were added to ensure all modules parse correctly.

**Files Fixed:**
- `vision/router/perception_strategy.py`
- `vision/router/screenshot_provider.py`
- `vision/strategies/coordinates_strategy.py`
- `vision/strategies/ocr_strategy.py`
- `vision/strategies/uia_strategy.py`
- `vision/strategies/visual_strategy.py`
- `vision/observations/targets.py`
- `core/services/vision_service.py`

### 2. Lazy Screenshot Acquisition
Implemented in `VisionService._maybe_capture_screenshot()` and `_any_strategy_needs_screenshot()`:
- Screenshots are only captured when a strategy that requires them (OCR or Visual) is actually going to be used.
- UIA and Coordinates strategies never trigger screenshot acquisition.
- The router never acquires screenshots; the service does so lazily based on the strategy's `requires_screenshot` property.

### 3. Deterministic Candidate Selection and Ambiguity Handling
Fixed the ranking key in `vision/router/perception_router.py` (`_rank_key` function):
- Removed `id(c)` from the ranking tuple to allow identical candidates to tie.
- Added semantic tie-breaker (source enum value) to maintain total ordering.
- When the router detects a tie (equal ranking keys for top candidates), it raises `AmbiguityError` instead of making an arbitrary choice.
- The service catches `AmbiguityError` and returns a `VisionResult` with `status=AMBIGUOUS`, including the candidates in the observation for the Brain/Agent to disambiguate.

### 4. Pure Observation Service (Never Claims Verified)
Ensured that `VisionResult` never includes a `verified` field and that the service never returns a status of `VERIFIED`:
- `VisionResult` status values are limited to `OBSERVED`, `AMBIGUOUS`, `NOT_FOUND`, `ERROR`.
- Both `ground_target` and `observe_state` methods return `VisionResult` with `status=OBSERVED` on success, never claiming verification.
- The `VerificationVerdict` is left to the Verifier (or Brain/Agent) to compute by comparing before/after observations.

### 5. Safety and Structural Audits
Updated tests to reflect the corrected behavior and ensure no forbidden imports or calls:
- `tests/test_vision_service.py`: Corrected test expectations for lazy screenshot and ambiguity handling.
- `tests/test_vision_safety_audit.py`: Updated to detect `PerceptionStrategy` via base class (not just decorators).
- All tests now pass (605/605).

### 6. Test Updates
Fixed incorrect test assumptions:
- `test_no_screenshot_for_uia_only_query`: Added proper candidate data so the UIA strategy returns a candidate, resulting in `OBSERVED` status and no screenshot.
- `test_ambiguity_surfaced_as_status`: After fixing the router's ambiguity detection, this test now passes by verifying that two indistinguishable candidates yield `AMBIGUOUS` status.

## Verification
- **All tests pass**: 605 tests passing, 0 failing.
- **Safety audit passes**: No forbidden imports or calls in the vision module.
- **Deterministic ambiguity**: Indistinguishable candidates raise `AmbiguityError` (caught and surfaced as `AMBIGUOUS`).
- **Lazy screenshot verified**: Tests confirm screenshots are only acquired when required by the strategy in use.

## Files Modified
```text
vision/router/perception_strategy.py
vision/router/screenshot_provider.py
vision/strategies/coordinates_strategy.py
vision/strategies/ocr_strategy.py
vision/strategies/uia_strategy.py
vision/strategies/visual_strategy.py
vision/observations/targets.py
core/services/vision_service.py
tests/test_vision_service.py
tests/test_vision_safety_audit.py
```

## Conclusion
Phase 7.1 Vision Integration Hardening is complete. The VisionService now:
- Is a proper service (not a singleton).
- Dependencies only on `ScreenshotProvider` (no direct engine access).
- Acquires screenshots lazily (only when needed by OCR/Visual strategies).
- Uses deterministic ranking for candidate selection.
- Surfaces ambiguity explicitly via `AmbiguityError` (leading to `AMBIGUOUS` status).
- Never claims verification; returns pure observations for the Brain/Agent/Verifier to interpret.
- Passes all architectural constraint checks and safety audits.

The vision module is now ready for integration with the Agent and Verifier components in subsequent phases.