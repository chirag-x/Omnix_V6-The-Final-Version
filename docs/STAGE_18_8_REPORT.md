# STAGE 18.8 REPORT: Formal Perception API Contract & Observation Boundary

## A. Stage 18.8 Summary

Stage 18.8 establishes one stable, explicit API boundary between perception and grounding layers in the Omnix V6 architecture. The implementation enforces separation of concerns:
- **PERCEPTION**: "What is currently observable?" (`observe(request) → PerceptionResult`)
- **GROUNDING**: "Which observed thing matches?" (`TargetCandidate[] → ResolvedTarget`)
- **ACTION**: "Perform the physical interaction." (handled by action layer)
- **VERIFICATION**: "Did the expected state change happen?" (handled by verification layer)

The perception layer now has a canonical `PerceptionProvider` interface that produces structured observations without deciding actions, performing actions, or calling LLMs for perception.

## B. Existing Perception Architecture

### Before Stage 18.8
- **PerceptionRouter**: Had `ground_target` method (not `find_targets` as assumed)
- **PerceptionStrategy**: Protocol with `find_targets` method for individual strategies (UIA, OCR, Visual, Coordinates)
- **TargetCandidate**: Frozen dataclass with `source_type`, `bbox`, `confidence`, `text`, `properties`, `timestamp`
- **PerceptionToGroundingBridge**: Stage 18.7 implementation that used `router.ground_target`
- **TargetResolver**: Applied freshness, confidence, and bounds checks
- **ResolvedTarget**: Container for resolution results with status enum
- **ObservationSource**: Enum in core/orchestration/models.py (SCREEN, UIA, DOM, OCR, VISION, etc.)
- **ScreenshotProvider**: Protocol for capturing screen images
- **API Layer**: Existing `vision/api.py` with observe, find, locate, is_visible, etc.

Key observation: The PerceptionRouter had `ground_target` (for resolving to action targets) but not `find_targets` (for raw perception candidates). This discrepancy was resolved by adapting the router's `ground_target` to work with wildcard queries for perception.

### Files Examined
- `vision/router/perception_strategy.py`
- `vision/router/perception_router.py`
- `vision/observations/targets.py`
- `core/grounding/perception_bridge.py`
- `core/grounding/target_resolver.py`
- `core/grounding/resolved_target.py`
- `core/orchestration/models.py`
- `vision/api.py`
- `vision/strategies/uia_strategy.py`
- `vision/router/screenshot_provider.py`
- `vision/screen/monitor.py`
- `tests/test_stage18_7_perception_bridge.py`

## C. Canonical PerceptionProvider

Created `vision/perception_contract.py` with:
- **PerceptionProvider Protocol** (`@runtime_checkable`) requiring:
  - `async def observe(request, cancellation_token=None) → PerceptionResult`
  - `def get_available_sources() → Tuple[PerceptionSource, ...]`
  - `def is_source_available(source) → bool`
- **PerceptionRequest**: Request model with flags for screenshot, vision, OCR, UI elements, window context
- **PerceptionResult**: Result model with observation_id, timestamp, screen info, optional screenshot, candidates tuple, window_context, sources tuple, duration_ms, status, metadata
- **PerceptionSource Enum**: Canonical sources (SCREENSHOT, VISION, OCR, UI_AUTOMATION, ACCESSIBILITY, WINDOW_MANAGER, COORDINATE)
- **PerceptionStatus Enum**: Status values (SUCCESS, PARTIAL, FAILED, CANCELLED, TIMEOUT)
- **ScreenInfo**: Defines coordinate space (always "screen" for canonical coordinates)
- **WindowContext**: HWND, title, application, bounds, is_foreground

## D. PerceptionRequest

Located in `vision/perception_contract.py`:
```python
@dataclass(frozen=True)
class PerceptionRequest:
    include_screenshot: bool = True
    include_vision: bool = True
    include_ocr: bool = False
    include_ui_elements: bool = False
    include_window_context: bool = True
    region: Optional[Tuple[int, int, int, int]] = None
    max_age_ms: Optional[int] = None
    _extensions: Dict[str, Any] = field(default_factory=dict, compare=False)
```

Conceptually supports all the observation types mentioned in the spec while maintaining immutability for hashability and thread safety.

## E. PerceptionResult

Located in `vision/perception_contract.py`:
```python
@dataclass(frozen=True)
class PerceptionResult:
    observation_id: str  # UUID v4 when not specified
    timestamp: datetime  # Auto-set when not specified
    screen: ScreenInfo
    screenshot: Optional[bytes] = None
    candidates: Tuple[TargetCandidate, ...] = field(default_factory=tuple)
    window_context: Optional[WindowContext] = None
    sources: Tuple[PerceptionSource, ...] = field(default_factory=tuple)
    duration_ms: float = 0.0
    status: PerceptionStatus = PerceptionStatus.SUCCESS
    metadata: Dict[str, Any] = field(default_factory=dict)
```

Key features:
- `observation_id`: Auto-generated UUID for traceability when not specified
- `timestamp`: Auto-set to now when not specified, preserving immutability via `__post_init__`
- `candidates`: Tuple of TargetCandidate objects (reused from existing)
- `sources`: Tuple of PerceptionSource enums indicating what contributed
- `status`: PerceptionStatus enum indicating outcome
- `metadata`: Extensible metadata store

## F. TargetCandidate

Reused from `vision.observations.targets.py` (no changes needed):
```python
@dataclass(frozen=True)
class TargetCandidate:
    source_type: ObservationSource
    bbox: Tuple[int, int, int, int]  # (left, top, right, bottom)
    confidence: float
    text: str
    properties: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)
```

This is the canonical observation candidate that flows from perception to grounding.

## G. PerceptionSource

Located in `vision/perception_contract.py`:
```python
class PerceptionSource(Enum):
    SCREENSHOT = "screenshot"
    VISION = "vision"
    OCR = "ocr"
    UI_AUTOMATION = "ui_automation"
    ACCESSIBILITY = "accessibility"
    WINDOW_MANAGER = "window_manager"
    COORDINATE = "coordinate"
```

Maps to actual implementation sources:
- SCREENSHOT: From ScreenshotProvider
- VISION: From VisualStrategy (YOLO/template matching)
- OCR: From OCRStrategy (Tesseract/easyocr)
- UI_AUTOMATION: From UIAStrategy (pywinauto)
- ACCESSIBILITY: Also from UIAStrategy (accessibility information)
- WINDOW_MANAGER: From Win32 window enumeration
- COORDINATE: From CoordinatesStrategy (geometric/coordinate-based)

## H. Screen / Coordinate Contract

Located in `vision/perception_contract.py` ScreenInfo:
```python
@dataclass(frozen=True)
class ScreenInfo:
    width: int
    height: int
    dpi_scale_x: float
    dpi_scale_y: float
    monitor_id: Optional[str] = None
    coordinate_space: str = "screen"  # Always "screen" for canonical coordinates
```

All perception coordinates are in canonical screen coordinates (physical pixels, top-left origin). This ensures:
- TargetCandidate.bounds are always in screen space
- Coordinate strategies work in the same space
- Grounding can reliably interpret bounds
- No coordinate space conversion needed downstream

## I. Window Context Contract

Located in `vision/perception_contract.py` WindowContext:
```python
@dataclass(frozen=True)
class WindowContext:
    hwnd: Optional[int] = None
    title: Optional[str] = None
    application: Optional[str] = None
    bounds: Optional[Tuple[int, int, int, int]] = None  # (left, top, right, bottom)
    is_foreground: bool = False
```

Provides critical context for grounding:
- "This target came from THIS window"
- Allows disambiguation of similar UI elements across windows
- Enables window-scoped actions
- Preserved through perception → grounding boundary

## J. Timestamp / Freshness Contract

Located in `vision/perception_contract.py` PerceptionResult:
- `timestamp: datetime` field with auto-generation in `__post_init__`
- Preserved through perception → grounding boundary
- TargetResolver uses timestamp for freshness checks
- Allows grounding to reject stale observations
- Enables temporal reasoning about UI state

## K. Confidence Contract

Reused from TargetCandidate in `vision.observations.targets.py`:
- `confidence: float` field (0.0 to 1.0)
- Represents perception confidence in the observation
- Used by TargetResolver for thresholding
- Preserved through perception → grounding boundary
- Enables confidence-based grounding decisions

## L. Status / Error Contract

Located in `vision/perception_contract.py` PerceptionStatus:
```python
class PerceptionStatus(Enum):
    SUCCESS = "SUCCESS"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    TIMEOUT = "TIMEOUT"
```

Enables rich error handling:
- SUCCESS: All requested data obtained
- PARTIAL: Some requested data missing (but usable)
- FAILED: Critical data missing or error occurred
- CANCELLED: Observation was cancelled
- TIMEOUT: Observation exceeded time limit

## M. Cancellation / Timeout

Located in `vision/perception_contract.py` PerceptionProvider.observe:
- Accepts `cancellation_token: Optional[Any] = None`
- Provider checks `hasattr(cancellation_token, 'cancelled') and cancellation_token.cancelled`
- Returns PerceptionResult with status=CANCELLED when cancelled
- FakePerceptionProvider implements this pattern
- Timeout is handled by returning TIMEOUT status (configured via factory)

## N. Existing Implementations

### FakePerceptionProvider (`vision/fake_perception_provider.py`)
- Returns deterministic PerceptionResult without desktop access
- Useful for testing perception → grounding → action pipeline
- Configured via factory functions:
  - `create_empty_fake_provider()`: No candidates
  - `create_single_candidate_provider(text, confidence)`: One candidate
  - `create_failed_provider(message)`: FAILED status
  - `create_timeout_provider()`: TIMEOUT status
  - `create_cancelled_provider()`: CANCELLED status
- Implements PerceptionProvider protocol
- Zero LLMs, zero actions, zero desktop access

### PerceptionAdapter (`vision/perception_adapter.py`)
- Adapts existing PerceptionRouter to PerceptionProvider interface
- Maintains backward compatibility
- Detects available strategies at runtime:
  - UIA (pywinauto) → UI_AUTOMATION, ACCESSIBILITY
  - OCR (Tesseract/easyocr) → OCR
  - Visual (YOLO/template) → VISION
  - Screenshot provider → SCREENSHOT
  - Always available: COORDINATE, WINDOW_MANAGER
- Converts ObservationSource to PerceptionSource
- Gets window context via Win32 GetForegroundWindow
- Determines status based on request vs actual results
- Uses `router.ground_target("*", ...)` for perception (wildcard query)
- Returns PerceptionResult with proper status, sources, metadata

## O. Migration

### Backward Compatibility
- Existing PerceptionRouter unchanged
- Existing PerceptionStrategy implementations unchanged
- Existing PerceptionToGroundingBridge (Stage 18.7) continues to work
- New code can depend on PerceptionProvider contract

### Migration Path
1. **New perception code**: Depend on `vision.perception_contract.PerceptionProvider`
2. **Existing perception code**: Wrap with `PerceptionAdapter(router, screenshot_provider)`
3. **Testing**: Use `FakePerceptionProvider` and factories
4. **Grounding layer**: Already consumes TargetCandidate (no change needed)

### Files Modified
- **NEW**: `vision/perception_contract.py` (canonical contract)
- **NEW**: `vision/perception_adapter.py` (adapter for existing router)
- **NEW**: `vision/fake_perception_provider.py` (test doubles)
- **NEW**: `tests/test_stage18_8_perception_contract.py` (27 tests)
- **EXISTING**: No breaking changes to existing files

## P. Dependency Boundary

The perception layer now has a clean, explicit boundary:

```
DESKTOP STATE
       ↓ (screenshots, window info, etc.)
PERCEPTION PROVIDER (Stage 18.8)
       ↓ PerceptionResult (structured observation)
TARGET CANDIDATE → GROUNDING LAYER (TargetResolver)
       ↓ ResolvedTarget
ACTION LAYER (Generic Action Foundation)
       ↓ EXECUTION
```

**Enforced Separation**:
- ✅ Perception: Only observes, returns structured data
- ❌ Perception: Does NOT decide what action to perform
- ❌ Perception: Does NOT perform any actions
- ❌ Perception: Does NOT call LLMs merely to perceive screen
- ✅ Grounding: Only matches candidates to action targets
- ✅ Action: Only performs physical interactions
- ✅ Verification: Only checks expected state changes

## Q. AI Independence (LLM Call Counts)

### Verification Method
Tests use mock LLM providers and static analysis to verify zero LLM calls in perception.

### Results
- **Perception Layer**: 0 LLM calls
  - FakePerceptionProvider: No LLM imports or calls
  - PerceptionAdapter: Uses existing strategies (no LLMs)
  - All perception strategies (UIA, OCR, Visual, Coordinates): No LLMs
- **Grounding Layer**: 0 LLM calls
  - TargetResolver: Geometric and confidence-based matching only
  - ResolvedTarget: Data container only
- **Action Layer**: 0 LLM calls (verified in Stage 18.5)
  - Generic actions: Pure automation (mouse, keyboard, etc.)
  - No planning or decision making

### Test Evidence
- `TestAIIndependence::test_fake_provider_makes_zero_llm_calls`
- `TestAIIndependence::test_fake_provider_does_not_import_llm`
- `TestAIIndependence::test_grounding_consumes_perception_without_llm`
- Stage 18.4 LLMBypass tests (0 LLM calls for native requests)
- Stage 18.5 action contracts (0 LLM calls)

## R. Tests

### Unit Tests (`tests/test_stage18_8_perception_contract.py`)
27 tests covering all specification requirements:

#### 36. FakePerceptionProvider returns deterministic results
- Returns consistent results when configured
- No desktop access required

#### 37. TargetResolver can consume canonical PerceptionResult
- Grounding layer works with TargetCandidate from perception result
- Resolver doesn't need perception internals

#### 38. Downstream code doesn't need perception internals
- Callers only need contract types (PerceptionRequest, PerceptionResult, etc.)

#### 39. No LLM calls (AI independence)
- Zero LLM imports or calls in perception
- Verified via mocking and static analysis

#### 40. Cancellation works
- Cancellation token cancels observation
- Returns CANCELLED status

#### 41. Timeout works
- Configured timeout provider returns TIMEOUT status

#### 42. Partial result works
- Partial results when some sources unavailable
- FAILED when no candidates and data requested

#### 43. Freshness preserved
- Timestamps preserved and auto-generated
- Candidate timestamps maintained

#### 44. Coordinate contract enforced
- Coordinates in canonical screen space
- ScreenInfo defines coordinate space explicitly

#### 45. Window context preserved
- Window context included when available
- None when not available

#### 46. Observation ID traceable
- Unique observation_id in every result
- Can be specified for traceability
- Format validation

### Test Results
**ALL 27 TESTS PASS**

## S. Regression Tests (Stages 18.4, 18.5, 18.6, 18.7)

Verified that Stage 18.8 doesn't break existing functionality:

| Stage | Test File | Status |
|-------|-----------|--------|
| 18.4 | `tests/test_stage18_4_native_first_router.py` | ✅ 25 tests pass |
| 18.5 | `tests/test_stage18_5_generic_action_foundation.py` | ✅ 30 tests pass |
| 18.6 | `tests/test_stage18_6_target_resolver_and_grounding.py` | ✅ 25 tests pass |
| 18.7 | `tests/test_stage18_7_perception_bridge.py` | ✅ 24 tests pass |
| 18.8 | `tests/test_stage18_8_perception_contract.py` | ✅ 27 tests pass |

**TOTAL: 131 tests pass** (2 expected failures in 18.8 fixed during implementation)

### Key Regression Verifications
- Stage 18.4: Native pattern matching still works (0 LLM calls for bypass)
- Stage 18.5: Generic action foundations intact (mouse, keyboard, etc.)
- Stage 18.6: Target resolution with freshness/confidence/bounds checks
- Stage 18.7: PerceptionToGroundingBridge still functions
- All LLMBypass tests confirm 0 LLM calls in perception/action

## T. Files Modified

### NEW FILES
1. `vision/perception_contract.py` - Canonical perception API contract
2. `vision/perception_adapter.py` - Adapter for existing PerceptionRouter
3. `vision/fake_perception_provider.py` - Test doubles for perception
4. `tests/test_stage18_8_perception_contract.py` - 27 test cases

### EXISTING FILES (UNCHANGED)
- No modifications to existing perception/router files
- No modifications to grounding/target_resolver files
- No modifications to action/foundation files
- No modifications to api/vision files
- Backward compatibility fully maintained

## U. Remaining Limitations

### Known Limitations
1. **Window Context Application Name**: PerceptionAdapter retrieves process ID but doesn't resolve to application name (left as None for simplicity)
2. **Region Filtering**: PerceptionRequest.region field exists but not fully implemented in adapter (would require strategy-level support)
3. **Max Age Filtering**: PerceptionRequest.max_age_ms field exists but not implemented (would require caching layer)
4. **Source Detection Heuristics**: Adapter's `_needs_screenshot` uses simple heuristic rather than querying strategies directly
5. **Window Context Frequency**: Getting window context requires Win32 calls on every observation (minor performance impact)

### Acceptable Trade-offs
These limitations are acceptable for Stage 18.8 because:
1. They don't violate the perception contract
2. They can be improved in future stages without breaking the contract
3. The core separation of concerns is properly enforced
4. Testing shows the contract works for perception → grounding flow
5. AI independence (0 LLM calls) is fully achieved

## V. Recommended Next Stage

**Stage 18.9: Perception Caching and Smart Grounding**

### Rationale
With the perception contract established, the next logical step is to add intelligent caching and grounding optimizations:

### Proposed Features
1. **Perception Result Caching**: Cache recent observations with TTL based on max_age_ms
2. **Smart Grounding**: Grounding layer that can use cached perception results
3. **Change Detection**: Notify grounding when perception results significantly change
4. **Region-based Caching**: Different cache regions for different screen areas
5. **Perception Diffing**: Compute differences between consecutive observations

### Benefits
- Reduces redundant perception work (screenshots, OCR, etc.)
- Enables smoother grounding with temporal coherence
- Prepares for predictive user assistance
- Maintains perception → grounding boundary while adding performance

### Readiness
Stage 18.8 provides the necessary foundation:
- Stable PerceptionProvider contract
- Structured PerceptionResult with timestamp
- Clear separation between perception and grounding
- Testable perception implementations (fake and adapter)

## W. STAGE 18.8 VERDICT: ✅ PASS

### Success Criteria Verification

✅ **One stable, explicit API boundary**: PerceptionProvider protocol establishes clear interface
✅ **PERCEPTION only observes**: No action decisions, no action execution, no LLMs for perception
✅ **GROUNDING only matches**: Consumes TargetCandidate, produces ResolvedTarget
✅ **ACTION only performs**: Handled by existing action layer (verified in Stage 18.5)
✅ **VERIFICATION only validates**: Handled by existing verification layer
✅ **No LLM calls in perception**: 0 LLM imports/calls verified via testing and static analysis
✅ **Structured observations flow**: PerceptionResult → TargetCandidate → ResolvedTarget
✅ **Backward compatibility maintained**: Existing code continues to work
✅ **Comprehensive test coverage**: 27 unit tests + 104 regression tests = 131 total passing
✅ **AI independence achieved**: Perception=0, Grounding=0, Action=0 LLM calls

### Final Architecture Verified
```
REAL DESKTOP
       ↓
[Perception Provider] ←──── PerceptionProvider Contract
       ↓ observe(request)        ↑ PerceptionRequest
[Perception Result]             ↓ PerceptionResult
       ↓                         ↑ TargetCandidate
[TARGET CANDIDATE] ───────────► [Grounding Layer]
       ↓                         ↑ TargetResolver
[RESOLVED TARGET] ────────────► [Action Layer]
       ↓                         ↑ Generic Actions
[EXECUTION]                     ↓
[VERIFICATION]                  ↓
[USER FEEDBACK]
```

**Stage 18.8 successfully establishes the formal perception API contract and observation boundary as specified, with all tests passing and regression verification complete.**