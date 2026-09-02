# V6 Vision Pre-Phase Audit

**Phase:** Pre-Phase 7 (Vision)
**Status:** Analysis only — no code changes
**Date:** 2026-08-30
**Scope:** `vision/` package (25 zero-byte `.py` files + 1 binary model) and `core/services/vision_service.py`

---

## 1. Executive summary

The `vision/` tree in V6 is **entirely architectural placeholder**. Every
`.py` file under `vision/` is 0 bytes, and `core/services/vision_service.py`
is also 0 bytes. None of the 25 placeholder files contains even an
import, a docstring, or a `pass` statement. They exist on disk only
because the directories were copied wholesale from V5 during the
Phase 0 file scaffolding, and V5's vision tree existed in its current
form.

V5's vision tree was a **single-strategy YOLO+OCR visual pipeline**
centered on `VisionManager` (a singleton that threaded a `mss`
screenshot loop and ran every frame through YOLO + EasyOCR + a
fusion/hierarchy/summary chain). V5 had **no `PerceptionRouter`**;
`VisionManager` was the entire perception layer, and `vision_manager`
was imported directly by `core/agent`, skills, `system/services`, and
the brain.

V6's product vision (`docs/OMNIX_V6_PRODUCT_VISION.md` §12) and
`V6_ARCHITECTURAL_DECISIONS.md` AD-13 / R-22 explicitly mandate a
different model:

> "**Multiple perception strategies must coexist.** Not every action
> uses YOLO. **No single ordering of these strategies is universally
> correct.** A `PerceptionRouter` selects the most reliable available
> strategy for the current application, target, action, context, and
> evidence. Sensible defaults exist (DOM-first for browsers, UIA-first
> for native Windows, OCR for text-heavy UIs, vision for unknown or
> custom UIs, coordinates as a last resort), but the router is
> adaptive, not hard-coded."

That is a **structural** change. V5's files are not "V6 vision
waiting to be filled in" — they are a competing architecture that
must be **replaced**, not populated. The right V6 unit of work is not
"implement `VisionManager` and `VisionPipeline`" but "build the
`PerceptionRouter` and register UIA, OCR, vision, and DOM as
`PerceptionStrategy` plug-ins under it, accessed through
`core/services/vision_service.py`."

**Bottom line:** of the 25 files in `vision/` plus
`core/services/vision_service.py`, **zero should be carried forward
as-is** from V5. The 1 binary (`yolo11n.pt`) is a real artifact and
**must** be kept. The `vision/` directory and `core/services/`
entry point **must** remain — they are the canonical namespaces. But
the files inside `vision/` are mostly **wrong shapes** for V6, not
empty boxes.

---

## 2. Inventory of the placeholder tree

All 25 `.py` files are 0 bytes. The structure is exactly V5's.

### 2.1 `vision/` top level (8 files, all 0 bytes)

| File | V5 size | V6 size | Notes on V5 role |
|------|---------|---------|-------------------|
| `__init__.py` | 970 B | 0 B | V5 docstring only — canonical entry points list. |
| `vision_manager.py` | 6297 B | 0 B | Singleton loop. Thread. Imports `VisionPipeline`, `UIPatternMemory`, `pyautogui`. **Anti-pattern under R-14.** |
| `vision_controller.py` | 1261 B | 0 B | Action surface (click / type / press). **Belongs in `system/input/`, not in vision.** |
| `vision_pipeline.py` | 7162 B | 0 B | Sequential detection → fusion → hierarchy → summary pipeline. Hard-coded chain. **Anti-pattern under R-22.** |
| `element_locator.py` | 5480 B | 0 B | Search-the-text helper. **Belongs in a strategy, not at the top.** |
| `ui_detector.py` | 1540 B | 0 B | YOLO wrapper that returns dicts. **Belongs in `vision/detection/`.** |
| `text_detector.py` | 2459 B | 0 B | EasyOCR wrapper that returns `VisionObject`. **Belongs in a strategy package.** |
| `screen_observer.py` | 1901 B | 0 B | `mss` capture loop in a thread. **Already REPLACED in V6 by `system/windows/window_service` + `desktop.screenshot` capability.** |
| `screen_intelligence.py` | 2497 B | 0 B | Wraps `vision_manager` and searches for keywords. **Should be folded into the UIA strategy or removed.** |
| `screen_summary.py` | 3453 B | 0 B | Builds a prompt-shaped string from a frame. **Duplicates what `screen_summary` is for in V6, but V6's `ContextService.world_state` is the right home for the *output*, not a vision module.** |

Note: V5's `vision/` has 10 files, not 8 — I'm counting only the
top-level module files; `screen_observer.py` and `screen_summary.py`
also live at the top level in V5, so the top-level count is 10
files. V6 mirrors this 1:1. (See the file listing in §3 of the
inventory table for the full count.)

### 2.2 `vision/detection/` (6 files, all 0 bytes)

| File | V5 role |
|------|---------|
| `yolo_detector.py` | YOLO11 inference → `VisionObject` list. |
| `bbox_normalizer.py` | Normalizes a raw detection into a typed object. |
| `detection_fuser.py` | Merges YOLO + OCR detections. |
| `duplicate_filter.py` | Removes overlapping / duplicate detections. |
| `metadata_builder.py` | Adds `frame_id` / `timestamp` / `source` to objects. |
| `object_tracker.py` | Cross-frame tracking (V5 had a stub; never wired into `VisionPipeline`). |

### 2.3 `vision/discovery/` (2 files, all 0 bytes)

| File | V5 role |
|------|---------|
| `__init__.py` | Empty package init. |
| `uia_source.py` | `UIASource` adapter around `pywinauto` UIA — **the most architecturally important file in the V5 tree** (it was the bridge that solved "YOLO can see the address bar but cannot name it"). |

### 2.4 `vision/hierarchy/` (2 files, all 0 bytes)

| File | V5 role |
|------|---------|
| `screen_regions.py` | Assigns top/center/bottom/left/right region tags. |
| `ui_hierarchy.py` | Builds a `UITree` from detected objects. |

### 2.5 `vision/models/` (6 `.py` files + 1 binary)

| File | V5 role |
|------|---------|
| `bounding_box.py` | `BoundingBox` dataclass (x1/y1/x2/y2, center, intersection). |
| `vision_object.py` | `VisionObject` (label, conf, bbox, source, tags, attributes). |
| `vision_frame.py` | `VisionFrame` (timestamp, dims, objects, texts, ui_tree, summary). |
| `ui_element.py` | `UIElement` (name, role, type, clickable, editable, focused, bbox, attributes). |
| `ui_tree.py` | `UITree` (elements list, find(name/text), walk). |
| `relationships.py` | Parent/child/sibling relations (V5 had a partial impl; not used by `VisionPipeline`). |
| `yolo11n.pt` | **Real 5.6 MB YOLO11n model file.** Keep. |

### 2.6 `vision/summary/` (2 files, all 0 bytes)

| File | V5 role |
|------|---------|
| `screen_state.py` | `ScreenState` (clickable_count, editable_count, has_dialog, has_popup, loading, error). |
| `semantic_summary.py` | Builds the LLM-readable summary string. |

### 2.7 `vision/utils/` (0 files — V5 had 2)

| V5 file | V5 role |
|---------|---------|
| `constants.py` | Constants (thresholds, color palettes, class-name maps). |
| `geometry.py` | Geometric helpers (IoU, distance, containment). |

V6 has an **empty** `vision/utils/` directory. V5 had both files; in
V6 the geometry and constants are good candidates for a generic
`utils/` package, not a vision-private one.

### 2.8 `core/services/vision_service.py` (0 bytes)

V6's `core/services/` exists for the seven service wrappers
(`ai_service`, `automation_service`, `context_service`,
`memory_service`, `skills_service`, `ui_service`, `vision_service`,
`voice_service`) per R-2 / R-14. `vision_service.py` is the **only
authorized entry point** for vision access; direct imports of
`vision.vision_manager` / `vision.vision_pipeline` /
`vision.screen_observer` from outside `vision/` and
`skills/built_in/vision/` are forbidden (R-14).

V5's `VisionManager` violated this by being a global singleton with a
free-running thread, accepting direct imports from `core/agent` and
skills. V6's `vision_service` is the right shape; it is empty and
stays empty until Phase 7.

### 2.9 `skills/built_in/vision/` (4 files, all 0 bytes)

`__init__.py`, `click_ui_skill.py`, `find_element_skill.py`,
`wait_ui_skill.py` are also zero-byte placeholders. They are *not*
the focus of this audit (they are skills, not core vision files) but
they are noted because R-14 explicitly says these are the
**only** allowed callers of `vision_service`. When vision ships in
Phase 7, these three skill files are likely the first real consumers
and the right place to start the "what does the public skill API
look like" design.

---

## 3. Analysis by question

### 3.1 Which files are architectural placeholders?

**Every `.py` file in `vision/` and `core/services/vision_service.py`
is an architectural placeholder.** All 26 files (25 in `vision/` + 1
in `core/services/`) are exactly 0 bytes. They contain no `pass`, no
`from __future__ import annotations`, no docstring, no encoding
declaration. They are not even legally importable as empty modules in
the strict sense (Python is fine with that, but they will be flagged
by any linter that warns on empty modules).

The `yolo11n.pt` binary is the **only non-placeholder artifact** in
the tree. It is a real, working pre-trained model.

### 3.2 Which names should remain?

Names that should remain in V6 (because they are referenced by V6
architecture docs, by other modules, or by the rulebook):

| Name | Reason to keep |
|------|---------------|
| `vision/` (directory) | The canonical namespace. R-14, AD-13, the architecture diagram in `V6_ARCHITECTURE_RULES.md` §2, and the V6 product vision all place vision at the `vision/` path. |
| `vision/__init__.py` | Reserved namespace marker. The V5 docstring was a "canonical entry points" manifest; the V6 `__init__.py` should re-export the new public API from the future vision package. |
| `vision/detection/` (directory) | A `detection` subpackage is the natural home for **the visual (YOLO/OCR) strategy only**, not the universal pipeline. Keep as the visual strategy implementation path. |
| `vision/discovery/` (directory) | The natural home for "where do UI elements come from" backends — UIA, accessibility APIs, future Selenium/DOM bridges. Keep. |
| `vision/hierarchy/` (directory) | The natural home for tree-building code that **applies to multiple sources** (UIA, DOM, OCR) rather than only vision. Keep, but redesign: the V5 design was vision-only. |
| `vision/models/` (directory) | The natural home for **shared data classes** that every strategy uses. Keep. The V5 names inside it are mostly fine (see §3.3). |
| `vision/summary/` (directory) | Reasonable home for screen-state aggregation. Keep, but expect a redesign around `WorldState`. |
| `vision/utils/` (directory, currently empty) | Probably should be **removed** in V6 — V6 has a top-level `utils/` package. Vision-internal helpers (geometry, constants) belong in the detection strategy or in a `vision/strategies/visual/` private module. |
| `vision/models/yolo11n.pt` | Real artifact. **Keep.** |
| `core/services/vision_service.py` | **Keep.** R-14 mandates this is the only authorized entry point. The file is the seam. |

### 3.3 Which files should be merged / replaced?

The V5 names were organized around a single, hard-coded chain. V6 is
organized around a router and plug-in strategies. Many V5 files are
**wrong-shaped**, not just empty. Here is the per-file
merge/replace verdict:

#### Top-level `vision/` (10 files)

| V6 file | Action | Reason |
|---------|--------|--------|
| `__init__.py` | **REPLACE** with new public API re-exports. | V5 listed `VisionManager` / `ElementLocator` / `UIDetector` / `TextDetector` / `ScreenObserver` / `VisionPipeline` as the public surface. V6 should list the `PerceptionRouter` and the registered strategies, plus the public data types. |
| `vision_manager.py` | **REMOVE** (do not recreate). | The whole concept of a singleton vision loop violates R-11 (event bus integration), R-14 (services, not singletons), R-22 (router, not hard-coded chain), and AD-13 (adaptive strategies). Its responsibilities split across: the `PerceptionRouter` (orchestration), the `desktop.screenshot` capability (frame capture, already in V6), the `vision_service` (service wrapper), and the visual strategy (detection). |
| `vision_pipeline.py` | **REMOVE** (do not recreate). | Hard-coded `YOLO → OCR → DuplicateFilter → MetadataBuilder → DetectionFuser → ScreenRegionAnalyzer → UIHierarchyBuilder → ScreenStateBuilder → SemanticSummaryBuilder` chain is exactly the universal-ordering pattern R-22 forbids. Replace with strategy plug-ins selected by the router. |
| `vision_controller.py` | **REMOVE**. | This is an **action** surface (mouse click, type, press). It belongs in `system/input/` (where V6 already has `mouse.py`, `keyboard.py`, `typing.py`, `async_adapter.py`). R-14 also says vision does not do input — vision **observes**; `system/input` **acts**. The two were entangled in V5 because V5's `VisionManager` had `find_element` + `click_element` + `wait_for_element` in one class. V6 separates them. |
| `element_locator.py` | **REPLACE / MOVE**. | In V5, `ElementLocator` was a thin search-the-tree helper on top of `VisionManager`. In V6, "find this UI element" is a `LocatorStrategy` plug-in (AD-21 and R-22 together imply locator strategies are also plug-in). Move to a `vision/locator/` or `vision/strategies/locator/` package, split by strategy (UIA-locator, OCR-locator, vision-locator, DOM-locator). |
| `ui_detector.py` | **REPLACE / MOVE** into `vision/detection/`. | V5 had `ui_detector.py` at the top level and `yolo_detector.py` in `vision/detection/`. They are the same thing in V5 (both wrap YOLO; `ui_detector` returned dicts, `yolo_detector` returned `VisionObject`). Consolidate into the YOLO detection strategy under `vision/strategies/visual/`. |
| `text_detector.py` | **REPLACE / MOVE** into a `vision/strategies/ocr/` strategy. | The EasyOCR wrapper becomes a strategy plug-in. V5's `TextDetector` was coupled to `BoundingBoxNormalizer`; that coupling stays. |
| `screen_observer.py` | **REMOVE**. | Already replaced in V6 by `system.windows.window_service.WindowsWindowService` plus the `desktop.screenshot` capability (`core/capabilities/desktop_observation.py::ScreenshotCapability`). `V6_LEGACY_PLACEHOLDER_MAP.md` already classifies this as REPLACED. |
| `screen_intelligence.py` | **REMOVE / MERGE**. | In V5, `ScreenIntelligence.analyze` rebuilt the `ui_elements` list from a raw vision dict, and `find_text_element` / `find_click_target` / `find_search_box` were a second copy of `ElementLocator` queries. None of this is a V6 need: the visual strategy returns typed `UIElement`s; locator strategies query the typed tree; the `ContextService.world_state` consumes the typed result. Three different concerns, none of which need this file. |
| `screen_summary.py` | **REMOVE / MERGE** into `ContextService.world_state`. | `ScreenSummaryBuilder.build` took a vision dict and a system-context dict and produced a free-form prompt string. V6's `WorldState` (R-23) is the typed home for the *output*; the prompt formatting belongs in the Brain prompt template, not in a vision module. |

#### `vision/detection/` (6 files)

| V6 file | Action | Reason |
|---------|--------|--------|
| `yolo_detector.py` | **KEEP NAME but MOVE/RESHAPE** into `vision/strategies/visual/yolo.py`. | The YOLO wrapper is a real, useful unit of code. The V5 docstring is correct: load model, run inference, convert to typed objects, measure time. Keep the contract; move the location. |
| `bbox_normalizer.py` | **KEEP NAME and LOCATION**. | A `BoundingBoxNormalizer.normalize(label, conf, bbox, frame_width, frame_height, source, model, frame_id, timestamp)` is genuinely useful. Used by both YOLO and OCR. Could move to `vision/models/` as a class method, but a standalone helper is fine. |
| `detection_fuser.py` | **REMOVE or RENAME**. | `DetectionFuser.fuse(objects, texts)` was V5's way to merge OCR text into YOLO-detected UI elements. In V6, fusion is a **router** concern, not a per-strategy one. The router may decide "for this app, OCR-only is enough; don't even call YOLO" or "for this app, UIA-only; ignore both YOLO and OCR." A pre-baked fuser that always runs both is a V5 design. Remove; if a real fusion need arises, it lives in the router, not the visual strategy. |
| `duplicate_filter.py` | **KEEP but RESHAPE** into a **per-strategy** filter, not a global one. | "Remove duplicate detections" is a useful per-frame operation for YOLO, but a UIA strategy does not need a YOLO-style NMS. Move into the visual strategy (YOLO + NMS overlap removal) and into the OCR strategy (text-region overlap). Do not call it from a global pipeline. |
| `metadata_builder.py` | **MERGE into the detection strategies**. | Adding `frame_id` / `timestamp` / `source` to a `VisionObject` is one or two lines. Promoting it to a class is V5 bloat. Fold into `BoundingBoxNormalizer` or the strategy constructors. |
| `object_tracker.py` | **REMOVE**. | V5's `object_tracker.py` was a stub — `VisionPipeline` never called it. Cross-frame tracking for V6 is either (a) not needed (single-frame decisions are enough for most `PerceptionRouter` queries) or (b) done by the Brain / `WorldState.diff()` over observations. Not a V6 vision module. |

#### `vision/discovery/` (2 files)

| V6 file | Action | Reason |
|---------|--------|--------|
| `__init__.py` | **KEEP** (empty package init). | |
| `uia_source.py` | **KEEP NAME, MOVE/RESHAPE** into `vision/strategies/uia/source.py`. | This is the highest-value V5 file. The 0.6.x pywinauto integration, the `_CLICKABLE_TYPES` / `_EDITABLE_TYPES` allow-lists, the `_uia_rect` defensive geometry, and the `available` capability probe are all worth keeping. Move it to a UIA strategy package; rename `enumerate(handle)` to match a `PerceptionStrategy` protocol; the `attach_to_foreground(window)` helper becomes a method on the UIA strategy, not a free function. |

#### `vision/hierarchy/` (2 files)

| V6 file | Action | Reason |
|---------|--------|--------|
| `screen_regions.py` | **REMOVE / DOWNSCOPE**. | "Assign this object to top/center/bottom/left/right" is a V5 affordance to make YOLO-only output usable. With UIA and DOM, region assignment is unnecessary. If retained at all, it should be a per-strategy helper for the visual strategy only, not a global hierarchy module. |
| `ui_hierarchy.py` | **KEEP NAME, RESHAPE** to be **source-agnostic**. | The `UIHierarchyBuilder.build(objects)` function takes a flat list of `UIElement`s and produces a `UITree`. That is a useful, strategy-agnostic operation. The UIA strategy already returns a tree (pywinauto walks a tree), so UIA uses its own tree. The visual strategy (YOLO + OCR) benefits from this builder. Make it a free function or a `vision.tree.builder` module, not a class. |

#### `vision/models/` (6 .py + 1 binary)

| V6 file | Action | Reason |
|---------|--------|--------|
| `bounding_box.py` | **KEEP** | `BoundingBox` is the geometry type used by every strategy, every `Observation` (under `Observation.data`), and every `UIElement`. V5's impl is fine. |
| `vision_object.py` | **KEEP** | `VisionObject` is a reasonable intermediate type for "raw detection." It is what YOLO and OCR produce before they are resolved into `UIElement`. Keep. |
| `vision_frame.py` | **REPLACE / RESHAPE** | V5's `VisionFrame` carries a `ui_tree`, a `summary`, and a `screen_state`. In V6, the frame is a transient per-strategy output; the persistent state lives in `WorldState`. Either (a) keep `VisionFrame` as the visual-strategy's output type and let the UIA strategy have its own, or (b) introduce a `PerceptionOutput` discriminated union and one concrete type per source. Either way, do not promote the visual frame to be the universal V6 frame. |
| `ui_element.py` | **KEEP** | `UIElement` is the universal "one UI thing" type. Every strategy produces it. V5's impl is fine; the V6 addition is the `source` attribute (UIA, OCR, vision, DOM) and a `strategy_confidence` field. |
| `ui_tree.py` | **KEEP** | `UITree` is the universal "collection of UIElement with find/walk" type. V5's impl is fine. |
| `relationships.py` | **REMOVE or RESHAPE** | V5's `relationships.py` was a partial implementation of parent/child/sibling relations between `UIElement`s. UIA naturally exposes these. The visual strategy does not produce them. Keep the **type definitions** if useful, but the relational logic is per-strategy. Promote nothing to the top of `vision/models/`. |
| `yolo11n.pt` | **KEEP** | Real artifact. 5.6 MB YOLO11n weights. Wired up in Phase 7 by the visual strategy's model loader. |

#### `vision/summary/` (2 files)

| V6 file | Action | Reason |
|---------|--------|--------|
| `screen_state.py` | **REPLACE / RENAME** to `ScreenState` shape, but route the **data** into `WorldState`. | V5's `ScreenState` was a value object inside a `VisionFrame`. In V6, the *fields* (`clickable_count`, `editable_count`, `has_dialog`, `has_popup`, `loading`, `error`) are properties of the `WorldState` container (R-23). Keep the type if useful as a snapshot; do not give it its own module. |
| `semantic_summary.py` | **REMOVE**. | `SemanticSummaryBuilder` was V5's prompt-shaping step. V6's Brain owns the prompt template. Vision does not know about prompts. |

#### `vision/utils/` (0 files; V5 had 2)

| V5 file | V6 action |
|---------|-----------|
| `constants.py` | **DO NOT RECREATE in `vision/utils/`.** The V6 constants (class-name maps, color palettes, confidence thresholds) belong with the strategy that uses them. YOLO class names go in the visual strategy. OCR config goes in the OCR strategy. Vision-private thresholds are not "utils" — they are strategy config. |
| `geometry.py` | **MOVE to top-level `utils/` if V6 needs it.** The geometry helpers (IoU, distance, containment) are general-purpose. If V6 introduces a top-level `utils/geometry.py`, this is its home. Otherwise, fold the small handful of helpers into the strategies that need them. |

#### `core/services/vision_service.py`

| File | Action | Reason |
|------|--------|--------|
| `core/services/vision_service.py` | **KEEP the path; REPLACE the contents with a service wrapper** in Phase 7. | R-14 mandates this is the only authorized entry point. The V6 service must expose methods that return a `*Result` dataclass (R-2). It delegates to the `PerceptionRouter` and the registered strategies; it does not own a thread loop, does not import `VisionManager` / `VisionPipeline` / `ScreenObserver` (those are V5 names being deleted). The public method shape is something like `vision_service.describe(observation_request) -> VisionResult`, `vision_service.locate(target, context) -> LocateResult`, `vision_service.snapshot() -> SnapshotResult` — and the service is the only thing that touches the strategies. |

### 3.4 Should the new V6 Vision architecture use these files?

**As filled-in placeholders, no.** The shapes are wrong:

1. **Wrong unit of decomposition.** V5 decomposed by
   *processing stage* (detect → filter → fuse → region → tree →
   state → summary). V6 decomposes by *perception strategy* (UIA,
   DOM, OCR, vision, coordinates) selected by a router. The V5
   `vision_pipeline.py` chain is exactly what R-22 forbids.

2. **Wrong ownership.** V5 made `vision_manager` a singleton with a
   thread loop that imported `pyautogui`, `UIPatternMemory`, and the
   window manager. R-14 says vision is a **service**, not a
   singleton. R-11 says cross-subsystem calls go through the event
   bus. R-4 says synchronous desktop APIs (pyautogui) are wrapped by
   an async adapter, not called from a background thread inside
   vision. None of the V5 ownership rules are V6-compatible.

3. **Wrong integration point.** V5 wired `vision_manager` directly
   into `core/agent` and into skills. R-14 forbids that. R-21
   (capability set is closed) means even if vision is a capability,
   the Brain cannot call it directly — it goes through the
   `CapabilityRouter`.

4. **Wrong public surface.** V5's `vision_manager` exposed
   `find_element` (async) and `click_element` (async, with pyautogui)
   in the same class. R-23 separates "what did I see" from "what did
   I do." V6 splits this: vision **observes**, `system/input` and
   the `desktop.mouse` / `desktop.keyboard` capabilities **act**.

5. **Wrong coupling to UIA.** V5's `VisionPipeline` never called
   `uia_source.py`. The UIA adapter was an orphan that the agent
   layer sometimes used directly. V6 makes UIA a first-class
   `PerceptionStrategy` registered with the router — but the
   strategy plug-in and the V5 `uia_source.py` are not the same
   shape. The V5 `uia_source.py` returns a list of `UIElement`s from
   a single window handle. The V6 strategy must also report
   `ObservationSource.UIA` provenance, confidence, capability
   metadata, and a uniform `PerceptionOutput` shape.

**In their new V6 form, several files are useful starting points:**

- `vision/discovery/uia_source.py` is the highest-leverage carry-over.
  The pywinauto integration, the control-type allow-lists, and the
  `available` capability probe are not reinvented cheaply. **Move it
  to `vision/strategies/uia/source.py`** and rewrap it as a
  `UiaPerceptionStrategy`.

- `vision/models/bounding_box.py`, `ui_element.py`, `ui_tree.py` are
  the right data shapes. V5's impls are usable. **Keep the names
  and the data model**; the V6 contribution is the `source` /
  `strategy_confidence` fields.

- `vision/detection/yolo_detector.py`'s contract (load model, run
  inference, return typed objects, measure time) is correct. The
  V6 contribution is to **wrap it as a strategy**, not as a stage
  in a pipeline.

- `vision/detection/bbox_normalizer.py` is a useful helper. The V5
  shape is correct. Keep.

- `vision/models/yolo11n.pt` is a real artifact. Keep on disk.

### 3.5 Should any files eventually be removed?

**Yes.** Concretely, when Phase 7 ships, the following V6 files
should be **deleted** (not filled in):

| V6 placeholder file | Removal trigger | Replacement in V6 |
|--------------------|-----------------|-------------------|
| `vision/vision_manager.py` | Phase 7 GA | None — concept replaced by `PerceptionRouter` + `vision_service`. |
| `vision/vision_pipeline.py` | Phase 7 GA | None — concept replaced by per-strategy plug-ins. |
| `vision/vision_controller.py` | Phase 7 GA | `system/input/mouse.py`, `system/input/keyboard.py`, `system/input/typing.py`, `core/capabilities/desktop_mouse.py`, `core/capabilities/desktop_keyboard.py` (all already in V6). |
| `vision/screen_observer.py` | Phase 7 GA | `system/windows/window_service.WindowsWindowService` + `core/capabilities/desktop_observation.py::ScreenshotCapability` (already in V6). `V6_LEGACY_PLACEHOLDER_MAP.md` already classifies this as REPLACED. |
| `vision/screen_intelligence.py` | Phase 7 GA | Locator strategies under `vision/strategies/*/locator.py`. |
| `vision/screen_summary.py` | Phase 7 GA | `core/state/context_service.world_state.WorldState` + Brain prompt template. |
| `vision/element_locator.py` | Phase 7 GA | Locator strategies, one per perception source. |
| `vision/detection/detection_fuser.py` | Phase 7 GA | Router-level fusion (or no fusion at all if the router picks one strategy per call). |
| `vision/detection/object_tracker.py` | Phase 7 GA | `WorldState.diff()` over `Observation`s (in `core/orchestration/`). |
| `vision/hierarchy/screen_regions.py` | Phase 7 GA | Per-strategy helper inside the visual strategy, if retained at all. |
| `vision/summary/screen_state.py` | Phase 7 GA | `WorldState` fields (`has_dialog`, `has_popup`, `loading`, `error`, `clickable_count`, `editable_count`, `visible_count`). |
| `vision/summary/semantic_summary.py` | Phase 7 GA | Brain prompt template (in `ai/`). |
| `vision/models/relationships.py` | Phase 7 GA | UIA strategy exposes parent/child naturally; visual strategy doesn't need it; per-strategy. |
| `vision/utils/` (the directory) | Phase 7 GA | Strategy config lives with the strategy; geometry lives in `utils/`. |
| `vision/ui_detector.py` (top level) | Phase 7 GA | Consolidated into the visual strategy's YOLO wrapper. |
| `vision/text_detector.py` (top level) | Phase 7 GA | Moved into the OCR strategy. |
| `core/services/vision_service.py` | **DO NOT REMOVE** | The service wrapper stays. Its *contents* are rewritten, but the path is the seam. |

**Net file count after Phase 7 GA:**
- `vision/` shrinks from 25 to roughly 10–14 files (one package per
  strategy, plus the shared data models, plus a tiny public API).
- `core/services/vision_service.py` stays at 1 file, but the file
  becomes a real implementation.

The 1 binary (`yolo11n.pt`) and 3 shared model files
(`bounding_box.py`, `ui_element.py`, `ui_tree.py`) survive
unchanged. The 1 discovery file (`uia_source.py`) survives in a new
location. The 1 detection file (`yolo_detector.py` in the new visual
strategy) survives in a new location. Everything else is deleted.

---

## 4. Proposed V6 vision package shape (for Phase 7 reference, not for implementation now)

The following is the **target shape** that Phase 7 should produce. It
is recorded here for the audit, not as work to be done now.

```
vision/
  __init__.py                          # public API: PerceptionRouter + types
  router/
    __init__.py
    perception_router.py               # adaptive strategy selection
    perception_strategy.py             # PerceptionStrategy Protocol
  strategies/
    __init__.py
    uia/
      __init__.py
      uia_source.py                    # moved from vision/discovery/
      uia_strategy.py                  # implements PerceptionStrategy
    visual/
      __init__.py
      yolo_strategy.py                 # YOLO + NMS
      yolo_detector.py                 # moved from vision/detection/
      bbox_normalizer.py               # moved from vision/detection/
    ocr/
      __init__.py
      ocr_strategy.py                  # EasyOCR (or Tesseract) wrapper
      text_detector.py                 # moved from top-level vision/
    dom/
      __init__.py
      dom_strategy.py                  # Playwright/Selenium DOM walker
    locator/
      __init__.py
      uia_locator.py
      ocr_locator.py
      visual_locator.py
      dom_locator.py
  models/
    __init__.py
    bounding_box.py                    # kept
    ui_element.py                      # kept (extended with source)
    ui_tree.py                         # kept
    vision_object.py                   # kept (visual strategy only)
    perception_output.py               # new: discriminated union
  observations/
    __init__.py
    screen_observation.py              # ObservationSource.SCREEN payload
    uia_observation.py                 # ObservationSource.UIA payload
    ocr_observation.py                 # ObservationSource.OCR payload
    vision_observation.py              # ObservationSource.VISION payload

vision/models/yolo11n.pt               # kept
```

And `core/services/vision_service.py` becomes a thin service wrapper:

```
VisionService:
    describe(observation_request) -> VisionResult
    locate(target, context) -> LocateResult
    snapshot() -> SnapshotResult
    available() -> CapabilityAvailability
```

The service delegates to the `PerceptionRouter`. The router delegates
to the registered strategies. The strategies call into the shared
data models. **No file in this tree imports `pyautogui`, owns a
thread loop, or is a singleton.** The vision package is a library,
not a service.

---

## 5. Key findings

1. **All 25 vision `.py` placeholders and `core/services/vision_service.py` are 0 bytes.** There is no implementation to preserve, no
   logic to migrate. V6 inherited V5's file shape, not V5's code.

2. **The V5 file names are wrong for V6.** V5 organized vision by
   processing stage (detect → fuse → hierarchy → summary) inside a
   single hard-coded pipeline. V6 organizes perception by strategy
   plug-in (UIA, DOM, OCR, vision, coordinates) selected by an
   adaptive `PerceptionRouter`. These are not equivalent
   decompositions.

3. **Two R-22 violations would happen if we filled the V5 files in
   naively.** V5's `vision_pipeline.py` is a hard-coded chain. V5's
   `vision_manager.py` is a singleton with a background thread that
   calls `pyautogui` directly. Both are the exact anti-patterns R-22
   and R-4 call out.

4. **Three V5 files are worth carrying forward** in new locations:
   `uia_source.py` (move to `vision/strategies/uia/`), `yolo_detector.py`
   (move to `vision/strategies/visual/`), and the data models
   `bounding_box.py`, `ui_element.py`, `ui_tree.py` (keep in
   `vision/models/`).

5. **One binary must be kept on disk** through Phase 7:
   `vision/models/yolo11n.pt` (5.6 MB YOLO11n weights).

6. **`vision/utils/` should be deleted**, not populated. V5's
   `constants.py` and `geometry.py` are V5-stage scaffolding; V6
   keeps strategy config with the strategy and general geometry
   in a top-level `utils/`.

7. **`core/services/vision_service.py` stays** and is the **only**
   path other modules use to reach vision. R-14. The file is empty
   today; it will be the service wrapper in Phase 7. It does not
   import `VisionManager` or `VisionPipeline` — those names do not
   exist in V6.

8. **`vision/screen_observer.py` is already REPLACED in V6** by
   `system.windows.window_service.WindowsWindowService` plus the
   `desktop.screenshot` capability. `V6_LEGACY_PLACEHOLDER_MAP.md`
   has this correctly classified. The other vision files in that
   map are classified DEFERRED — but that classification predates
   the V6 PerceptionRouter decision (AD-13) and the rewrite
   described here. The map should be updated in the next doc pass
   to mark the V5-shape files as REMOVED (to be deleted in Phase 7
   GA) rather than DEFERRED.

9. **The placeholder `__init__.py` files in `vision/`,
   `vision/discovery/`, and the subpackages should be replaced**
   with V6 public-API re-exports. An empty `__init__.py` is fine
   for now (it is, after all, 0 bytes), but they should not become
   0 bytes of "future code" — they should be re-export modules for
   the strategies and types that ship in Phase 7.

10. **Phase 7 file count** (target): ~14 Python files in `vision/`
    plus `core/services/vision_service.py` plus the binary. Down
    from 25. The 11-file reduction is the V5-stage files being
    replaced by the V6-strategy files plus a few deletions
    (`screen_regions`, `relationships`, `object_tracker`,
    `detection_fuser`, `screen_state`, `semantic_summary`,
    `screen_intelligence`, `screen_summary`).

---

## 6. Pre-Phase checklist (for the record, not for action now)

- [x] Confirmed all 25 `vision/` `.py` files are 0 bytes.
- [x] Confirmed `core/services/vision_service.py` is 0 bytes.
- [x] Confirmed `vision/models/yolo11n.pt` is a real 5.6 MB YOLO11n model.
- [x] Confirmed `vision/utils/` is empty (V5 had 2 files; V6 has none).
- [x] Cross-referenced `V6_LEGACY_PLACEHOLDER_MAP.md` — its `vision/`
      classification predates AD-13 / R-22 and needs updating in a
      later doc pass to reflect this audit.
- [x] Cross-referenced `V6_ARCHITECTURE_RULES.md` (R-2, R-4, R-11,
      R-14, R-22) and `V6_ARCHITECTURAL_DECISIONS.md` (AD-13) to
      confirm the V5 shape is incompatible with V6 rules.
- [x] Cross-referenced `V6_PHASE_ROADMAP.md` Phase 4 description
      ("Recreate `vision/*` from V5, plus `vision/utils/` (2 files)
      if needed") — **this roadmap is now stale** for vision. The
      Phase 7 work is not "recreate from V5"; it is "build the
      `PerceptionRouter` and register strategies." A future roadmap
      amendment should be filed.
- [x] No source code modified. No V5 code copied. No file created
      except this audit document.
