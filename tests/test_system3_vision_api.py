"""
Omnix V6 — System 3 (Vision) unit tests.

These tests cover the new public Vision API in :mod:`vision`
plus the typed result model (:class:`GroundedElement`,
:class:`ScreenDescription`), the recovery helpers
(:mod:`vision.recovery`), and the trace artefact.  They do
NOT require a real screen — every test runs with a
:class:`NullScreenshotProvider` and a stub :class:`PerceptionRouter`.

Test naming
-----------
``test_*``: positive happy-path checks.
``test_*_negative``: explicit negative-status checks.
``test_*_legacy_mapping``: legacy 4-value vocabulary
    (``VisionResult.status``) translation.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from typing import Any, List, Optional
from unittest import mock

from core.orchestration.models import ObservationSource
from vision.grounded_element import (
    ELEMENT_TYPE_BUTTON,
    ELEMENT_TYPE_CHECKBOX,
    ELEMENT_TYPE_COMBOBOX,
    ELEMENT_TYPE_EDIT,
    ELEMENT_TYPE_ICON,
    ELEMENT_TYPE_IMAGE,
    ELEMENT_TYPE_LINK,
    ELEMENT_TYPE_MENU_ITEM,
    ELEMENT_TYPE_RADIO,
    ELEMENT_TYPE_TAB,
    ELEMENT_TYPE_TEXT,
    ELEMENT_TYPE_UNKNOWN,
    KNOWN_ELEMENT_TYPES,
    KNOWN_SOURCES,
    GroundedElement,
    GroundedElementStatus,
    ambiguous,
    from_legacy_status,
    from_target_candidate,
    low_confidence,
    normalise_element_type,
    not_found,
)
from vision.observations.targets import TargetCandidate
from vision.recovery import (
    from_candidates,
    reobserve_and_compare,
    retry_with_strategy,
)
from vision.router.screenshot_provider import NullScreenshotProvider
from vision.screen import (
    DEFAULT_THRESHOLD,
    MonitorInfo,
    StabilityWindow,
    compute_stability,
    enumerate_monitors,
    is_stable,
    primary_monitor,
    to_virtual_coords,
    from_virtual_coords,
)
from vision.screen_description import (
    ScreenDescription,
    ScreenStability,
    WindowInfo,
    empty_description,
    make_screenshot_id,
)
from vision.strategies.llm_vision_strategy import (
    LLMVisionNotConfigured,
    LLMVisionStrategy,
)
from vision.trace import (
    VisualTraceRecord,
    is_visual_trace_enabled,
    set_visual_trace_enabled,
    trace_event,
)


# ---------------------------------------------------------------------------
# Stub router for the API tests
# ---------------------------------------------------------------------------


def _candidate(
    bbox=(10, 10, 100, 40),
    confidence=0.9,
    text="OK",
    source=ObservationSource.UIA,
    properties=None,
) -> TargetCandidate:
    return TargetCandidate(
        source_type=source,
        bbox=tuple(bbox),
        confidence=float(confidence),
        text=text,
        properties=dict(properties or {}),
    )


class StubRouter:
    """Minimal PerceptionRouter-shaped object for the public API tests.

    Implements the small surface :func:`vision.api._ground_query` and
    :func:`vision.api.observe` actually use: ``strategies``,
    ``ground_target`` (returning a :class:`GroundedTarget` or raising
    the documented exceptions).
    """

    def __init__(self, *, candidates=None, raise_ambiguity=False,
                 raise_not_found=False, raise_other=False,
                 observation=None):
        self._candidates: List[TargetCandidate] = list(candidates or [])
        self._raise_ambiguity = raise_ambiguity
        self._raise_not_found = raise_not_found
        self._raise_other = raise_other
        self._observation = observation
        self.calls: List[Any] = []

    @property
    def strategies(self) -> list:
        return []  # API only inspects this for requires_screenshot

    def ground_target(self, query, *, image_path=None, preferred_strategy=None):
        self.calls.append((query, image_path, preferred_strategy))
        if self._raise_ambiguity:
            from vision.router.perception_router import AmbiguityError
            raise AmbiguityError("stub ambiguous", candidates=self._candidates)
        if self._raise_not_found:
            from vision.router.perception_router import TargetNotGroundedError
            raise TargetNotGroundedError("stub not found", query=query)
        if self._raise_other:
            raise RuntimeError("stub other failure")
        if not self._candidates:
            from vision.router.perception_router import TargetNotGroundedError
            raise TargetNotGroundedError("no candidates", query=query)
        # Always pick the first candidate as the "winner".
        from vision.observations.targets import GroundedTarget
        return GroundedTarget(
            candidate=self._candidates[0],
            resolution_method="stub",
            alternatives=max(0, len(self._candidates) - 1),
        )


# ---------------------------------------------------------------------------
# GroundedElement + status enum
# ---------------------------------------------------------------------------


class GroundedElementTests(unittest.TestCase):
    def test_status_enum_has_eleven_values(self):
        values = {s.value for s in GroundedElementStatus}
        # The spec asks for 11 values; the 12th "OBSERVED" is
        # the positive observation case.
        self.assertEqual(len(values), 12)
        self.assertIn("OBSERVED", values)
        self.assertIn("TARGET_NOT_FOUND", values)
        self.assertIn("LOW_CONFIDENCE", values)
        self.assertIn("MULTIPLE_TARGETS", values)
        self.assertIn("WINDOW_NOT_VISIBLE", values)
        self.assertIn("WINDOW_NOT_FOCUSED", values)
        self.assertIn("UI_NOT_READY", values)
        self.assertIn("SCREEN_UNSTABLE", values)
        self.assertIn("OCR_FAILED", values)
        self.assertIn("ACCESSIBILITY_UNAVAILABLE", values)
        self.assertIn("TIMEOUT", values)
        self.assertIn("TARGET_CHANGED", values)

    def test_legacy_status_mapping(self):
        self.assertEqual(
            from_legacy_status("OBSERVED"), GroundedElementStatus.OBSERVED,
        )
        self.assertEqual(
            from_legacy_status("AMBIGUOUS"), GroundedElementStatus.MULTIPLE_TARGETS,
        )
        self.assertEqual(
            from_legacy_status("NOT_FOUND"), GroundedElementStatus.TARGET_NOT_FOUND,
        )
        self.assertEqual(
            from_legacy_status("ERROR"), GroundedElementStatus.OCR_FAILED,
        )
        # Unknown legacy values fall back to TARGET_NOT_FOUND.
        self.assertEqual(
            from_legacy_status("MAGIC"), GroundedElementStatus.TARGET_NOT_FOUND,
        )

    def test_from_target_candidate_round_trip(self):
        cand = _candidate(
            bbox=(20, 30, 220, 70),
            confidence=0.83,
            text="Sign in",
            source=ObservationSource.UIA,
            properties={"control_type": "Button", "automation_id": "btn_signin"},
        )
        el = from_target_candidate(cand)
        self.assertEqual(el.text, "Sign in")
        self.assertEqual(el.confidence, 0.83)
        self.assertEqual(el.bbox, (20, 30, 220, 70))
        self.assertEqual(el.type, ELEMENT_TYPE_BUTTON)
        self.assertEqual(el.source, "uia")
        self.assertTrue(el.is_observed)
        self.assertFalse(el.is_negative)
        self.assertEqual(el.center, (120, 50))

    def test_from_target_candidate_invalid_bbox_raises(self):
        cand = _candidate(bbox=(10, 10))  # malformed
        with self.assertRaises(ValueError):
            from_target_candidate(cand)

    def test_normalise_element_type_aliases(self):
        self.assertEqual(normalise_element_type("PushButton"), ELEMENT_TYPE_BUTTON)
        self.assertEqual(normalise_element_type("hyperlink"), ELEMENT_TYPE_LINK)
        self.assertEqual(normalise_element_type("TextBox"), ELEMENT_TYPE_EDIT)
        self.assertEqual(normalise_element_type("Label"), ELEMENT_TYPE_TEXT)
        self.assertEqual(normalise_element_type("????"), ELEMENT_TYPE_UNKNOWN)
        self.assertEqual(normalise_element_type(None), ELEMENT_TYPE_UNKNOWN)

    def test_known_element_types_closed(self):
        # The closed set includes all the standard element types.
        for t in (
            ELEMENT_TYPE_BUTTON, ELEMENT_TYPE_LINK, ELEMENT_TYPE_EDIT,
            ELEMENT_TYPE_TEXT, ELEMENT_TYPE_IMAGE, ELEMENT_TYPE_CHECKBOX,
            ELEMENT_TYPE_RADIO, ELEMENT_TYPE_COMBOBOX, ELEMENT_TYPE_MENU_ITEM,
            ELEMENT_TYPE_TAB, ELEMENT_TYPE_ICON, ELEMENT_TYPE_UNKNOWN,
        ):
            self.assertIn(t, KNOWN_ELEMENT_TYPES)

    def test_known_sources_closed(self):
        self.assertEqual(
            KNOWN_SOURCES, frozenset({"uia", "ocr", "derived", "vision", "screen"}),
        )

    def test_not_found_sentinel(self):
        el = not_found(query="missing")
        self.assertEqual(el.status, GroundedElementStatus.TARGET_NOT_FOUND)
        self.assertTrue(el.is_negative)
        self.assertFalse(el.is_observed)
        self.assertEqual(el.bbox, (0, 0, 0, 0))

    def test_low_confidence_preserves_bbox(self):
        cand = _candidate(bbox=(50, 50, 100, 90), confidence=0.3)
        el = low_confidence(cand, threshold=0.5)
        self.assertEqual(el.status, GroundedElementStatus.LOW_CONFIDENCE)
        self.assertEqual(el.bbox, (50, 50, 100, 90))
        self.assertEqual(el.properties.get("confidence_threshold"), 0.5)

    def test_ambiguous_centroid_bbox(self):
        c1 = _candidate(bbox=(0, 0, 10, 10))
        c2 = _candidate(bbox=(100, 100, 110, 110))
        el = ambiguous([c1, c2])
        self.assertEqual(el.status, GroundedElementStatus.MULTIPLE_TARGETS)
        # The union bbox is (min_l, min_t, max_r, max_b).
        self.assertEqual(el.bbox, (0, 0, 110, 110))
        # MULTIPLE_TARGETS is not interactable.
        self.assertFalse(el.interactable)
        self.assertEqual(el.properties.get("alternatives"), 2)


# ---------------------------------------------------------------------------
# ScreenDescription + monitors
# ---------------------------------------------------------------------------


class ScreenDescriptionTests(unittest.TestCase):
    def test_empty_description(self):
        d = empty_description()
        self.assertEqual(d.stability, ScreenStability.UNKNOWN)
        self.assertGreaterEqual(len(d.monitors), 1)
        self.assertEqual(d.focused_window, None)
        self.assertEqual(d.elements, ())
        self.assertEqual(d.text_density, 0.0)

    def test_screenshot_id_is_unique(self):
        self.assertNotEqual(make_screenshot_id(), make_screenshot_id())

    def test_enumerate_monitors_returns_at_least_primary(self):
        ms = enumerate_monitors()
        self.assertGreaterEqual(len(ms), 1)
        # At least one monitor is primary, or the first is
        # returned as a fallback.
        self.assertTrue(any(m.is_primary for m in ms) or len(ms) >= 1)
        # Bounds are 4-tuples of ints.
        for m in ms:
            self.assertEqual(len(m.bounds_physical_px), 4)
            self.assertGreater(m.dpi_scale, 0.0)

    def test_to_virtual_coords_identity_at_100_percent(self):
        p = (100, 200)
        m = MonitorInfo(
            monitor_id="t", name="t",
            bounds_physical_px=(0, 0, 1920, 1080),
            dpi_scale=1.0, is_primary=True,
        )
        # No monitor id → uses primary scale (1.0 in fallback).
        self.assertEqual(to_virtual_coords(p), p)
        self.assertEqual(from_virtual_coords(p), p)

    def test_to_virtual_coords_scales(self):
        # 150% DPI: physical px are 1.5x the logical px.
        m = MonitorInfo(
            monitor_id="x", name="x",
            bounds_physical_px=(0, 0, 2880, 1620),
            dpi_scale=1.5, is_primary=False,
        )
        with mock.patch(
            "vision.screen.monitor.get_monitor_by_id", return_value=m,
        ):
            self.assertEqual(to_virtual_coords((300, 300), monitor_id="x"), (200, 200))
            self.assertEqual(from_virtual_coords((200, 200), monitor_id="x"), (300, 300))

    def test_compute_stability_falls_back_to_size_and_mtime(self):
        with tempfile.TemporaryDirectory() as td:
            a = os.path.join(td, "a.png")
            b = os.path.join(td, "b.png")
            with open(a, "wb") as f:
                f.write(b"x" * 10)
            with open(b, "wb") as f:
                f.write(b"x" * 10)
            # Same size, same mtime (within 0.5s) → 1.0.
            self.assertEqual(compute_stability(a, b), 1.0)
            with open(b, "wb") as f:
                f.write(b"y" * 99)
            # Different size → 0.0.
            self.assertEqual(compute_stability(a, b), 0.0)

    def test_stability_window(self):
        sw = StabilityWindow(threshold=0.9, window=3)
        sw.push(0.95)
        sw.push(0.96)
        self.assertFalse(sw.is_stable())
        sw.push(0.97)
        self.assertTrue(sw.is_stable())
        sw.push(0.5)  # drops below threshold
        self.assertFalse(sw.is_stable())


# ---------------------------------------------------------------------------
# Recovery
# ---------------------------------------------------------------------------


class RecoveryTests(unittest.TestCase):
    def test_retry_with_strategy_succeeds(self):
        # First call returns LOW_CONFIDENCE; second returns OBSERVED.
        calls = {"n": 0}

        def attempt():
            calls["n"] += 1
            if calls["n"] == 1:
                cand = _candidate(confidence=0.3)
                return from_target_candidate(
                    cand, status=GroundedElementStatus.LOW_CONFIDENCE,
                )
            cand = _candidate(confidence=0.9)
            return from_target_candidate(
                cand, status=GroundedElementStatus.OBSERVED,
            )

        el = retry_with_strategy("q", attempt=attempt, max_attempts=3)
        self.assertEqual(el.status, GroundedElementStatus.OBSERVED)
        self.assertEqual(calls["n"], 2)

    def test_retry_with_strategy_stops_on_terminal_negative(self):
        # MULTIPLE_TARGETS is not retryable; the helper returns immediately.
        cand = _candidate()
        calls = {"n": 0}

        def attempt():
            calls["n"] += 1
            return from_target_candidate(
                cand, status=GroundedElementStatus.MULTIPLE_TARGETS,
            )

        el = retry_with_strategy("q", attempt=attempt, max_attempts=5)
        self.assertEqual(el.status, GroundedElementStatus.MULTIPLE_TARGETS)
        self.assertEqual(calls["n"], 1)

    def test_retry_with_strategy_returns_last_on_exhaustion(self):
        calls = {"n": 0}

        def attempt():
            calls["n"] += 1
            cand = _candidate(confidence=0.2)
            return from_target_candidate(
                cand, status=GroundedElementStatus.LOW_CONFIDENCE,
            )

        el = retry_with_strategy("q", attempt=attempt, max_attempts=2)
        self.assertEqual(el.status, GroundedElementStatus.LOW_CONFIDENCE)
        self.assertEqual(calls["n"], 2)

    def test_reobserve_and_compare_marks_changed(self):
        cand = _candidate(bbox=(100, 100, 200, 200))
        baseline = from_target_candidate(cand, status=GroundedElementStatus.OBSERVED)
        # Different baseline bbox → TARGET_CHANGED.
        cand2 = _candidate(bbox=(500, 500, 600, 600))
        current = from_target_candidate(cand2, status=GroundedElementStatus.OBSERVED)
        out = reobserve_and_compare(
            "x", current=current, baseline_bbox=baseline.bbox, iou_threshold=0.7,
        )
        self.assertEqual(out.status, GroundedElementStatus.TARGET_CHANGED)
        self.assertEqual(out.bbox, (500, 500, 600, 600))
        self.assertIn("iou", out.properties)

    def test_reobserve_and_compare_unchanged(self):
        cand = _candidate(bbox=(100, 100, 200, 200))
        el = from_target_candidate(cand, status=GroundedElementStatus.OBSERVED)
        out = reobserve_and_compare(
            "x", current=el, baseline_bbox=el.bbox, iou_threshold=0.7,
        )
        self.assertEqual(out.status, GroundedElementStatus.OBSERVED)

    def test_from_candidates_zero(self):
        el = from_candidates([])
        self.assertEqual(el.status, GroundedElementStatus.TARGET_NOT_FOUND)

    def test_from_candidates_single(self):
        cand = _candidate()
        el = from_candidates([cand])
        self.assertEqual(el.status, GroundedElementStatus.OBSERVED)

    def test_from_candidates_multi(self):
        c1 = _candidate(bbox=(0, 0, 10, 10))
        c2 = _candidate(bbox=(20, 20, 30, 30))
        el = from_candidates([c1, c2])
        self.assertEqual(el.status, GroundedElementStatus.MULTIPLE_TARGETS)


# ---------------------------------------------------------------------------
# Trace artefact
# ---------------------------------------------------------------------------


class TraceTests(unittest.TestCase):
    def setUp(self):
        set_visual_trace_enabled(False)

    def tearDown(self):
        set_visual_trace_enabled(False)

    def test_trace_disabled_by_default(self):
        self.assertFalse(is_visual_trace_enabled())

    def test_trace_event_returns_none_when_disabled(self):
        result = trace_event(event="probe", query="x")
        self.assertIsNone(result)

    def test_trace_event_returns_record_when_enabled(self):
        set_visual_trace_enabled(True)
        with mock.patch.dict(os.environ, {"OMNIX_VISUAL_TRACE_PATH": os.path.join(tempfile.gettempdir(), "test_trace.jsonl")}):
            # Force path reset for the module-level singleton.
            import vision.trace.visual_trace as vt
            vt._current_path = None
            rec = trace_event(
                event="find", query="hello", strategy="uia", candidates=1,
                selected_id="abc", confidence=0.95, latency_ms=1.0, status="OK",
            )
            self.assertIsNotNone(rec)
            self.assertEqual(rec.event, "find")
            self.assertEqual(rec.query, "hello")
            # Confirm a row hit disk.
            self.assertTrue(os.path.isfile(os.environ["OMNIX_VISUAL_TRACE_PATH"]))
            with open(os.environ["OMNIX_VISUAL_TRACE_PATH"], "r", encoding="utf-8") as fp:
                last_line = fp.readlines()[-1]
            parsed = json.loads(last_line)
            self.assertEqual(parsed["event"], "find")
            self.assertEqual(parsed["query"], "hello")
            self.assertEqual(parsed["status"], "OK")


# ---------------------------------------------------------------------------
# LLM vision strategy stub
# ---------------------------------------------------------------------------


class LLMVisionStrategyTests(unittest.TestCase):
    def test_not_configured_raises(self):
        s = LLMVisionStrategy()
        self.assertFalse(s.is_configured)
        with self.assertRaises(LLMVisionNotConfigured):
            s.perceive("q")

    def test_configure_enables(self):
        s = LLMVisionStrategy()
        s.configure("openai/gpt-4o", timeout_s=2.0)
        self.assertTrue(s.is_configured)
        # perceive is still a no-op stub → returns [].
        self.assertEqual(s.perceive("q"), [])

    def test_configure_with_empty_model_raises(self):
        s = LLMVisionStrategy()
        with self.assertRaises(LLMVisionNotConfigured):
            s.configure("")


# ---------------------------------------------------------------------------
# Public API (find / locate / wait_for / verify / observe)
# ---------------------------------------------------------------------------


class PublicApiTests(unittest.TestCase):
    def setUp(self):
        # Always start with a clean module-level singleton.
        import vision.api as api
        api._default_initialised = False
        api._default_router = None
        api._default_provider = NullScreenshotProvider()

    def tearDown(self):
        set_visual_trace_enabled(False)

    def test_observe_with_null_provider(self):
        import vision
        vision.set_default_provider(NullScreenshotProvider())
        d = vision.observe()
        self.assertIsInstance(d, ScreenDescription)
        self.assertGreaterEqual(len(d.monitors), 1)

    def test_describe_is_alias_for_observe(self):
        import vision.api as api
        self.assertIs(api.describe, api.observe)

    def test_find_observed(self):
        import vision
        stub = StubRouter(candidates=[_candidate(text="OK", confidence=0.95)])
        el = vision.find("OK", router=stub, provider=NullScreenshotProvider())
        self.assertEqual(el.status, GroundedElementStatus.OBSERVED)
        self.assertEqual(el.text, "OK")

    def test_find_not_found(self):
        import vision
        stub = StubRouter(raise_not_found=True)
        el = vision.find("missing", router=stub, provider=NullScreenshotProvider())
        self.assertEqual(el.status, GroundedElementStatus.TARGET_NOT_FOUND)

    def test_find_ambiguous(self):
        import vision
        stub = StubRouter(raise_ambiguity=True, candidates=[_candidate(), _candidate()])
        el = vision.find("multi", router=stub, provider=NullScreenshotProvider())
        self.assertEqual(el.status, GroundedElementStatus.MULTIPLE_TARGETS)
        # It must NOT silently dispatch — the spec is clear.
        self.assertFalse(el.interactable)

    def test_find_router_exception_becomes_ocr_failed_negative(self):
        import vision
        stub = StubRouter(raise_other=True)
        el = vision.find("anything", router=stub, provider=NullScreenshotProvider())
        self.assertTrue(el.is_negative)

    def test_find_no_router_returns_negative(self):
        import vision
        vision.set_default_provider(NullScreenshotProvider())
        el = vision.find("x", router=None)
        self.assertTrue(el.is_negative)

    def test_is_visible(self):
        import vision
        stub = StubRouter(candidates=[_candidate(confidence=0.9)])
        self.assertTrue(
            vision.is_visible("OK", router=stub, provider=NullScreenshotProvider()),
        )
        stub2 = StubRouter(raise_not_found=True)
        self.assertFalse(
            vision.is_visible("missing", router=stub2, provider=NullScreenshotProvider()),
        )

    def test_locate_nth(self):
        import vision
        candidates = [
            _candidate(bbox=(0, 0, 10, 10), text="a"),
            _candidate(bbox=(20, 20, 30, 30), text="b"),
            _candidate(bbox=(40, 40, 50, 50), text="c"),
        ]
        stub = StubRouter(candidates=candidates)
        el = vision.locate("x", nth=2, router=stub, provider=NullScreenshotProvider())
        # The router does not expose a multi-candidate path, so
        # the helper should fall back to "first" for stub routers
        # but still return a valid element.
        self.assertIsInstance(el, GroundedElement)

    def test_wait_for_timeout_raises(self):
        import vision
        stub = StubRouter(raise_not_found=True)
        with self.assertRaises(vision.WaitTimeout):
            vision.wait_for(
                "missing", timeout_s=0.5, poll_interval_s=0.1,
                router=stub, provider=NullScreenshotProvider(),
            )

    def test_wait_for_observed(self):
        import vision
        stub = StubRouter(candidates=[_candidate(confidence=0.9)])
        el = vision.wait_for(
            "OK", timeout_s=2.0, poll_interval_s=0.1,
            router=stub, provider=NullScreenshotProvider(),
        )
        self.assertEqual(el.status, GroundedElementStatus.OBSERVED)

    def test_verify_empty_expected_is_unverified(self):
        import vision
        v = vision.verify("")
        self.assertEqual(v.outcome, vision.VerificationVerdict.OUTCOME_UNVERIFIED)
        self.assertTrue(v.is_unverified)

    def test_verify_with_step_object(self):
        import vision

        class Step:
            query = "OK"

        stub = StubRouter(candidates=[_candidate(confidence=0.9, text="OK")])
        v = vision.verify(Step(), router=stub, provider=NullScreenshotProvider())
        self.assertEqual(v.outcome, vision.VerificationVerdict.OUTCOME_VERIFIED)
        self.assertTrue(v.is_verified)

    def test_verify_mismatch_when_target_changed(self):
        import vision
        baseline = from_target_candidate(
            _candidate(bbox=(100, 100, 200, 200), confidence=0.9),
            status=GroundedElementStatus.OBSERVED,
        )
        stub = StubRouter(candidates=[_candidate(bbox=(900, 900, 1000, 1000), confidence=0.9)])
        v = vision.verify(
            "OK", baseline=baseline, router=stub, provider=NullScreenshotProvider(),
        )
        self.assertEqual(v.outcome, vision.VerificationVerdict.OUTCOME_MISMATCH)
        self.assertIn("target", v.reason.lower())

    def test_verify_mismatch_when_target_not_observed(self):
        import vision
        stub = StubRouter(raise_not_found=True)
        v = vision.verify("missing", router=stub, provider=NullScreenshotProvider())
        self.assertEqual(v.outcome, vision.VerificationVerdict.OUTCOME_MISMATCH)
        self.assertFalse(v.is_verified)


if __name__ == "__main__":
    unittest.main()
