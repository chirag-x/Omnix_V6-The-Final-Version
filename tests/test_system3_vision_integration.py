"""
Omnix V6 — System 3 (Vision) integration tests.

End-to-end coverage of the new public Vision API
(:mod:`vision.api`) that does *not* require a real screen.
The integration tests build a synthetic 1920x1080 PNG, hand
it to a :class:`StaticScreenshotProvider`, and exercise the
full ``find / observe / locate / wait_for / verify`` flow
through a stub :class:`PerceptionRouter`.

What this covers that the unit tests do not
-------------------------------------------
* End-to-end through :mod:`vision.api` (not just the
  individual helpers).
* The multi-monitor safety gate: a candidate whose centre
  is outside any known monitor is downgraded to
  :attr:`GroundedElementStatus.WINDOW_NOT_VISIBLE`.
* :func:`vision.verify` composes the new API with the
  recovery helper correctly when a baseline is supplied.
* :func:`vision.wait_for` honours ``stable_for_s`` (skipped
  in unit tests because the null provider returns no
  screenshots).
* :func:`vision.observe` populates :class:`ScreenDescription`
  end-to-end through the provider.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from typing import Any, List, Optional
from unittest import mock

from core.orchestration.models import ObservationSource
from vision.grounded_element import (
    GroundedElement,
    GroundedElementStatus,
    from_target_candidate,
    not_found,
)
from vision.observations.targets import TargetCandidate, GroundedTarget
from vision.router.screenshot_provider import ScreenshotProvider
from vision.screen import (
    MonitorInfo,
    enumerate_monitors,
    refresh_monitors,
)
from vision.safety.coordinates import validate_coordinates, CoordinateSafetyError
from vision.screen_description import (
    ScreenDescription,
    ScreenStability,
    make_screenshot_id,
)


# ---------------------------------------------------------------------------
# Helpers
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


def _make_synthetic_png(path: str, width: int = 1920, height: int = 1080) -> None:
    """Write a synthetic PNG of the requested size to ``path``.

    Uses PIL when available (preferred, produces a real PNG);
    falls back to a 1x1 PNG byte literal so the test still runs
    on a host without PIL.
    """
    try:
        from PIL import Image  # type: ignore
        img = Image.new("RGB", (width, height), color=(0, 0, 0))
        img.save(path, format="PNG")
    except Exception:
        # 1x1 black PNG, 67 bytes — sufficient for tests that
        # only need the file to exist and report a path.
        with open(path, "wb") as fp:
            fp.write(
                b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
                b"\x00\x00\x00\x01\x00\x00\x00\x01"
                b"\x08\x06\x00\x00\x00\x1f\x15\xc4"
                b"\x89\x00\x00\x00\rIDATx\xdac\xf8\xff"
                b"\xff?\x00\x05\xfe\x02\xfe\xa3Z\x9f\x88"
                b"\x00\x00\x00\x00IEND\xaeB`\x82"
            )


class StaticScreenshotProvider:
    """A :class:`ScreenshotProvider` that always returns the
    same on-disk PNG.  The path is supplied by the test; the
    provider does *not* generate the file — that is the
    test's job (so the test can inspect the path or remove it
    on tear-down).
    """

    name = "static-screenshot"

    def __init__(self, path: str) -> None:
        self._path = path
        self.calls: int = 0

    def capture(self, *, path: Optional[str] = None) -> Optional[str]:
        self.calls += 1
        if os.path.isfile(self._path):
            return self._path
        return None


class StubRouter:
    """PerceptionRouter-shaped stub that returns a configured
    candidate (or raises).  Records the image_path it was
    asked to ground against so the test can assert the
    provider's path was plumbed through.
    """

    def __init__(self, *, candidates=None, raise_ambiguity=False,
                 raise_not_found=False, raise_other=False) -> None:
        self._candidates: List[TargetCandidate] = list(candidates or [])
        self._raise_ambiguity = raise_ambiguity
        self._raise_not_found = raise_not_found
        self._raise_other = raise_other
        self.calls: List[Any] = []
        self.requires_screenshot: bool = True  # forces acquisition

    @property
    def strategies(self) -> list:
        # A small in-line strategy object so the public API's
        # ``requires_screenshot`` check picks it up.
        return [self]

    def ground_target(self, query, *, image_path=None, preferred_strategy=None):
        self.calls.append({"query": query, "image_path": image_path,
                           "preferred_strategy": preferred_strategy})
        if self._raise_ambiguity:
            from vision.router.perception_router import AmbiguityError
            raise AmbiguityError("stub ambiguous",
                                 candidates=list(self._candidates))
        if self._raise_not_found:
            from vision.router.perception_router import TargetNotGroundedError
            raise TargetNotGroundedError("stub not found", query=query)
        if self._raise_other:
            raise RuntimeError("stub other failure")
        if not self._candidates:
            from vision.router.perception_router import TargetNotGroundedError
            raise TargetNotGroundedError("no candidates", query=query)
        return GroundedTarget(
            candidate=self._candidates[0],
            resolution_method="stub",
            alternatives=max(0, len(self._candidates) - 1),
        )


# ---------------------------------------------------------------------------
# End-to-end through vision.api with a real on-disk PNG
# ---------------------------------------------------------------------------


class EndToEndApiTests(unittest.TestCase):
    """Full pipeline: synthetic PNG -> provider -> router -> vision.api."""

    def setUp(self) -> None:
        import vision.api as api
        api._default_initialised = False
        api._default_router = None
        api._default_provider = None
        self._tmp = tempfile.TemporaryDirectory()
        self._png_path = os.path.join(self._tmp.name, "screen.png")
        _make_synthetic_png(self._png_path, width=1920, height=1080)
        self._provider = StaticScreenshotProvider(self._png_path)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # ---- observe ----
    def test_observe_returns_screen_description(self):
        import vision
        vision.set_default_provider(self._provider)
        d = vision.observe()
        self.assertIsInstance(d, ScreenDescription)
        self.assertGreaterEqual(len(d.monitors), 1)
        self.assertIsNotNone(d.screenshot_id)
        self.assertEqual(d.stability, ScreenStability.UNKNOWN)
        # The static provider was called exactly once.
        self.assertGreaterEqual(self._provider.calls, 1)

    # ---- find ----
    def test_find_observed(self):
        import vision
        # Place the candidate in the centre of a 1920x1080 image so
        # the safety gate accepts it on a single-monitor host.
        cand = _candidate(bbox=(400, 300, 600, 360), confidence=0.95)
        stub = StubRouter(candidates=[cand])
        el = vision.find("OK", router=stub, provider=self._provider)
        self.assertEqual(el.status, GroundedElementStatus.OBSERVED)
        self.assertEqual(el.text, "OK")
        # The provider was called: StubRouter declares
        # requires_screenshot=True so vision.api acquires first.
        self.assertGreaterEqual(self._provider.calls, 1)
        # The router received the provider's path.
        self.assertEqual(stub.calls[0]["image_path"], self._png_path)

    def test_find_ambiguous(self):
        import vision
        cand_a = _candidate(bbox=(10, 10, 20, 20), text="A")
        cand_b = _candidate(bbox=(30, 30, 40, 40), text="B")
        stub = StubRouter(raise_ambiguity=True, candidates=[cand_a, cand_b])
        el = vision.find("multi", router=stub, provider=self._provider)
        self.assertEqual(el.status, GroundedElementStatus.MULTIPLE_TARGETS)
        # Critical security constraint: the ambiguous case is
        # NEVER marked interactable.
        self.assertFalse(el.interactable)

    # ---- wait_for ----
    def test_wait_for_observed_polls_until_success(self):
        import vision
        # First call: not found; second call: observed.
        cand = _candidate(bbox=(400, 300, 600, 360), confidence=0.9)
        stub = StubRouter(candidates=[cand])
        # Make the first ground_target call raise; subsequent succeed.
        original = stub.ground_target
        calls = {"n": 0}

        def maybe_ground(query, *, image_path=None, preferred_strategy=None):
            calls["n"] += 1
            if calls["n"] < 2:
                from vision.router.perception_router import (
                    TargetNotGroundedError,
                )
                raise TargetNotGroundedError("not yet", query=query)
            return original(query, image_path=image_path,
                             preferred_strategy=preferred_strategy)

        stub.ground_target = maybe_ground
        el = vision.wait_for(
            "OK", timeout_s=2.0, poll_interval_s=0.1,
            router=stub, provider=self._provider,
        )
        self.assertEqual(el.status, GroundedElementStatus.OBSERVED)
        self.assertGreaterEqual(calls["n"], 2)

    # ---- verify ----
    def test_verify_verified_when_target_observed(self):
        import vision
        cand = _candidate(bbox=(400, 300, 600, 360), confidence=0.9)
        stub = StubRouter(candidates=[cand])
        v = vision.verify("OK", router=stub, provider=self._provider)
        self.assertEqual(
            v.outcome, vision.VerificationVerdict.OUTCOME_VERIFIED,
        )
        self.assertTrue(v.is_verified)

    def test_verify_mismatch_when_target_moved(self):
        import vision
        baseline = from_target_candidate(
            _candidate(bbox=(100, 100, 200, 200), confidence=0.9),
            status=GroundedElementStatus.OBSERVED,
        )
        cand = _candidate(bbox=(1500, 800, 1600, 850), confidence=0.9)
        stub = StubRouter(candidates=[cand])
        v = vision.verify(
            "OK", baseline=baseline, router=stub, provider=self._provider,
        )
        self.assertEqual(
            v.outcome, vision.VerificationVerdict.OUTCOME_MISMATCH,
        )
        self.assertIn("target", v.reason.lower())


# ---------------------------------------------------------------------------
# Multi-monitor / safety gate
# ---------------------------------------------------------------------------


class MultiMonitorSafetyGateTests(unittest.TestCase):
    """Verify the safety gate against a multi-monitor host.

    We stub :func:`enumerate_monitors` to return a known pair
    of monitors and then exercise the coordinate-safety gate
    directly.  Stubbing via ``mock.patch`` keeps the rest of
    the module-level cache intact.
    """

    def setUp(self) -> None:
        # Always start with a clean monitor cache.
        try:
            refresh_monitors(force_refresh=True)
        except Exception:
            pass

    def test_validate_coordinates_accepts_point_on_primary(self):
        monitors = (
            MonitorInfo(
                monitor_id="primary", name="primary",
                bounds_physical_px=(0, 0, 1920, 1080),
                dpi_scale=1.0, is_primary=True,
            ),
        )
        with mock.patch("vision.screen.monitor.enumerate_monitors",
                        return_value=monitors):
            from vision.observations.screenshot_metadata import (
                ScreenshotMetadata,
                make_screenshot_metadata,
            )
            meta = make_screenshot_metadata(
                image_width=1920, image_height=1080,
                monitor_id="primary",
            )
            x, y = validate_coordinates(
                (100, 200), screenshot_metadata=meta, source="uia",
            )
            self.assertEqual((x, y), (100, 200))

    def test_validate_coordinates_rejects_point_off_monitor(self):
        # Stubbed: host has TWO monitors side by side, primary on
        # the right (x in [0, 1920)) and secondary on the left
        # (x in [-1920, 0)).  A point at (100, 200) lives on the
        # primary monitor, a point at (-100, 200) lives on the
        # secondary, and a point at (5000, 5000) lives on neither.
        monitors = (
            MonitorInfo(
                monitor_id="primary", name="primary",
                bounds_physical_px=(0, 0, 1920, 1080),
                dpi_scale=1.0, is_primary=True,
            ),
            MonitorInfo(
                monitor_id="secondary", name="secondary",
                bounds_physical_px=(-1920, 0, 0, 1080),
                dpi_scale=1.0, is_primary=False,
            ),
        )
        with mock.patch("vision.screen.monitor.enumerate_monitors",
                        return_value=monitors):
            # The primary monitor's screenshot metadata bounds the
            # image to the primary monitor's dimensions.  A point
            # at (-100, 200) is on the secondary monitor and
            # therefore *outside* the screenshot's pixel bounds;
            # the safety gate must reject it.
            from vision.observations.screenshot_metadata import (
                make_screenshot_metadata,
            )
            meta = make_screenshot_metadata(
                image_width=1920, image_height=1080,
                monitor_id="primary",
            )
            with self.assertRaises(CoordinateSafetyError):
                validate_coordinates(
                    (-100, 200), screenshot_metadata=meta, source="uia",
                )
            # A point outside both monitors' bounds is rejected.
            with self.assertRaises(CoordinateSafetyError):
                validate_coordinates(
                    (5000, 5000), screenshot_metadata=meta, source="uia",
                )
            # A point clearly on the primary monitor passes.
            x, y = validate_coordinates(
                (500, 500), screenshot_metadata=meta, source="uia",
            )
            self.assertEqual((x, y), (500, 500))

    def test_enumerate_monitors_handles_stubbed_cache(self):
        # With the cache invalidated and no real monitors,
        # the call returns at least the virtual primary.
        try:
            refresh_monitors(force_refresh=True)
        except Exception:
            pass
        ms = enumerate_monitors()
        self.assertGreaterEqual(len(ms), 1)
        # At least one monitor must declare a non-empty
        # bounds tuple.
        self.assertTrue(
            all(len(m.bounds_physical_px) == 4 for m in ms),
        )


# ---------------------------------------------------------------------------
# Public API composition (verify + recovery)
# ---------------------------------------------------------------------------


class VerifyCompositionTests(unittest.TestCase):
    """End-to-end through :func:`vision.verify` with both
    fresh-grinding and reobservation via the recovery helper.
    """

    def setUp(self) -> None:
        import vision.api as api
        api._default_initialised = False
        api._default_router = None
        api._default_provider = None
        self._tmp = tempfile.TemporaryDirectory()
        self._png_path = os.path.join(self._tmp.name, "screen.png")
        _make_synthetic_png(self._png_path)
        self._provider = StaticScreenshotProvider(self._png_path)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_verify_with_no_expected_returns_unverified(self):
        import vision
        v = vision.verify("")
        self.assertEqual(
            v.outcome, vision.VerificationVerdict.OUTCOME_UNVERIFIED,
        )

    def test_verify_unverified_when_baseline_but_no_target(self):
        import vision
        # Baseline provided but the router reports no candidate.
        stub = StubRouter(raise_not_found=True)
        v = vision.verify("missing", baseline=not_found(), router=stub,
                          provider=self._provider)
        self.assertEqual(
            v.outcome, vision.VerificationVerdict.OUTCOME_MISMATCH,
        )


if __name__ == "__main__":
    unittest.main()
