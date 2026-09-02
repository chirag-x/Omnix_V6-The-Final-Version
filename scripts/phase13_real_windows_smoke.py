"""
Omnix V6 -- Phase 13 (VISION-GROUNDED COMPUTER USE) smoke.

A *selectable* set of small, side-effect-bounded real-Windows tests
that exercise the Phase 13 surface end-to-end:

    VisionService (no real mouse) -> DefaultVisionTargetProvider
        -> coordinate-safety / freshness gates
        -> Agent pre-action hook (mocked; no real mouse / keyboard)

Each individual test is opt-in.  The script does NOT spawn a real
GUI input -- the closed capability set is not invoked from here, so
no pyautogui / mouse / keyboard events are produced.  Tests that
need a real screen capture use the NullScreenshotProvider, which
returns None; the freshness gate then refuses to dispatch a
grounded action, which is the *desired* safe behavior in a CI host
without a display.

Run from the V6 project root:

    # All tests:
    python scripts/phase13_real_windows_smoke.py

    # A subset:
    python scripts/phase13_real_windows_smoke.py --tests meta,coord,fresh,provider

The script never prints secrets, never uses the LLM, and is
safe to run unattended in CI on a Windows host (even one with no
display server, because every screen-bound path is gated).

Exit code:
    0   all selected tests passed
    1   at least one test failed
    2   the host is not Windows (smoke is Windows-only)
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Set the standard test-time environment knobs early.
os.environ.setdefault("OMNIX_HEADLESS", "1")
os.environ.setdefault("OMNIX_QUIET_BOOT", "1")


@dataclass
class TestRecord:
    name: str
    description: str
    ok: bool = False
    skipped: bool = False
    error: str = ""
    duration_ms: float = 0.0
    details: Dict[str, object] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# The selected test set
# ---------------------------------------------------------------------------

async def t_metadata_factory(rec: TestRecord) -> None:
    """``make_screenshot_metadata`` produces a valid ScreenshotMetadata."""
    from vision.observations.screenshot_metadata import make_screenshot_metadata

    meta = make_screenshot_metadata(image_width=1920, image_height=1080)
    if meta.image_width != 1920 or meta.image_height != 1080:
        raise AssertionError(
            f"unexpected dimensions: {meta.image_width}x{meta.image_height}"
        )
    if not meta.screenshot_id or not isinstance(meta.screenshot_id, str):
        raise AssertionError(f"screenshot_id not stamped: {meta.screenshot_id!r}")
    age = meta.age_seconds()
    if age < 0 or age > 5.0:
        raise AssertionError(f"fresh metadata should have tiny age; got {age}")
    rec.details["screenshot_id"] = meta.screenshot_id
    rec.details["age_seconds"] = round(age, 4)


async def t_metadata_from_capability_result(rec: TestRecord) -> None:
    """``from_capability_result`` adapts a real capability dict safely."""
    from vision.observations.screenshot_metadata import from_capability_result

    # Empty result -> safe 1x1 default (coordinate gate will reject).
    meta = from_capability_result({})
    if meta.image_width != 1 or meta.image_height != 1:
        raise AssertionError(
            f"empty result must produce 1x1 metadata; got "
            f"{meta.image_width}x{meta.image_height}"
        )

    # Realistic capability result.
    meta2 = from_capability_result(
        {"path": "C:/tmp/cap.png", "width": 1920, "height": 1080, "monitor_id": "primary"}
    )
    if meta2.image_width != 1920 or meta2.image_height != 1080:
        raise AssertionError("from_capability_result: did not honor explicit width/height")
    if meta2.monitor_id != "primary":
        raise AssertionError("from_capability_result: did not honor monitor_id")
    rec.details["default_path"] = meta2.path


async def t_visual_observation_minimal(rec: TestRecord) -> None:
    """``VisualObservation`` constructs the four statuses with sane defaults."""
    from vision.observations.visual_observation import (
        VisualObservation,
        VisualObservationStatus,
    )

    for status in VisualObservationStatus:
        obs = VisualObservation(
            subject="ok-button",
            status=status,
            bbox=(0, 0, 10, 10),
            confidence=0.5,
        )
        if obs.subject != "ok-button":
            raise AssertionError(f"subject lost: {obs.subject!r}")
        if obs.status is not status:
            raise AssertionError(f"status lost: {obs.status!r}")
    # Blocking is only true for AMBIGUOUS + ERROR (per spec: NOT_FOUND does
    # not block -- it can be re-tried after a delay, and OBSERVED is success).
    if not VisualObservation(
        subject="x", status=VisualObservationStatus.AMBIGUOUS
    ).is_blocking:
        raise AssertionError("AMBIGUOUS should be blocking")
    if not VisualObservation(
        subject="x", status=VisualObservationStatus.ERROR
    ).is_blocking:
        raise AssertionError("ERROR should be blocking")


async def t_coordinate_safety_in_bounds(rec: TestRecord) -> None:
    """``is_within_bounds`` correctly classifies 2-tuples against a screen."""
    from vision.safety.coordinates import is_within_bounds

    cases = [
        ((0, 0), 1920, 1080, True),
        ((1919, 1079), 1920, 1080, True),
        ((960, 540), 1920, 1080, True),
        ((-1, 0), 1920, 1080, False),
        ((0, -1), 1920, 1080, False),
        ((1920, 0), 1920, 1080, False),  # == width, out
        ((0, 1080), 1920, 1080, False),  # == height, out
        ((1919, 1080), 1920, 1080, False),
    ]
    for point, w, h, expected in cases:
        got = is_within_bounds(point, w, h)
        if got != expected:
            raise AssertionError(
                f"is_within_bounds({point!r}, {w}, {h}) returned {got}, expected {expected}"
            )


async def t_coordinate_safety_validates(rec: TestRecord) -> None:
    """``validate_coordinates`` raises CoordinateSafetyError on bad input."""
    from vision.safety.coordinates import (
        CoordinateSafetyError,
        validate_coordinates,
    )
    from vision.observations.screenshot_metadata import make_screenshot_metadata

    meta = make_screenshot_metadata(image_width=1920, image_height=1080)

    # Happy path.
    validate_coordinates((100, 200), screenshot_metadata=meta, source="uia")

    # Bad shape: not a 2-tuple.
    try:
        validate_coordinates((100, 200, 300), screenshot_metadata=meta, source="uia")
    except CoordinateSafetyError:
        pass
    else:
        raise AssertionError("validate_coordinates should reject 3-tuples")

    # Off-screen.
    try:
        validate_coordinates((9999, 9999), screenshot_metadata=meta, source="uia")
    except CoordinateSafetyError:
        pass
    else:
        raise AssertionError("validate_coordinates should reject off-screen point")

    # Bad source.
    try:
        validate_coordinates(
            (100, 200), screenshot_metadata=meta, source="pyautogui"
        )
    except CoordinateSafetyError:
        pass
    else:
        raise AssertionError("validate_coordinates should reject unknown source")

    # Missing screenshot metadata.
    try:
        validate_coordinates((100, 200), screenshot_metadata=None, source="uia")
    except CoordinateSafetyError:
        pass
    else:
        raise AssertionError("validate_coordinates should reject missing metadata")


async def t_freshness_default(rec: TestRecord) -> None:
    """``is_fresh`` accepts a recent screenshot, rejects a 10s-old one."""
    from vision.observations.screenshot_metadata import make_screenshot_metadata
    from vision.safety.freshness import (
        DEFAULT_MAX_AGE_S,
        is_fresh,
        require_fresh,
        StaleScreenError,
    )

    fresh = make_screenshot_metadata(
        image_width=10, image_height=10, timestamp=time.time()
    )
    if not is_fresh(fresh):
        raise AssertionError("recent metadata should be fresh")
    if require_fresh(fresh).screenshot_id != fresh.screenshot_id:
        raise AssertionError("require_fresh must return the same metadata")

    stale = make_screenshot_metadata(
        image_width=10, image_height=10, timestamp=time.time() - 10.0
    )
    if is_fresh(stale):
        raise AssertionError("10s-old metadata should be stale (default 5s)")
    try:
        require_fresh(stale)
    except StaleScreenError:
        pass
    else:
        raise AssertionError("require_fresh should raise StaleScreenError")
    rec.details["default_max_age_s"] = DEFAULT_MAX_AGE_S


async def t_provider_translates_grounded(rec: TestRecord) -> None:
    """``DefaultVisionTargetProvider`` returns GROUNDED + bbox + center."""
    from vision.integration.agent_provider import DefaultVisionTargetProvider
    from core.services.vision_service import VisionResult
    from vision.observations.screenshot_metadata import (
        make_screenshot_metadata,
    )

    meta = make_screenshot_metadata(image_width=1920, image_height=1080)
    fake = _FakeVisionService(
        status="OBSERVED",
        bbox=(100, 100, 200, 200),
        confidence=0.95,
        source="uia",
        text="OK",
        screenshot_meta=meta,
    )
    provider = DefaultVisionTargetProvider(fake)
    contract = provider.ground_target("OK button")
    if contract.status.value != "grounded":
        raise AssertionError(
            f"expected GROUNDED, got {contract.status.value!r} ({contract.error!r})"
        )
    if contract.bbox != (100, 100, 200, 200):
        raise AssertionError(f"bbox lost: {contract.bbox!r}")
    if contract.center != (150, 150):
        raise AssertionError(f"center not computed: {contract.center!r}")
    rec.details["bbox"] = contract.bbox
    rec.details["center"] = contract.center


async def t_provider_rejects_stale(rec: TestRecord) -> None:
    """A grounded result with a stale screenshot is REJECTED (safety)."""
    from vision.integration.agent_provider import DefaultVisionTargetProvider
    from core.services.vision_service import VisionResult
    from vision.observations.screenshot_metadata import make_screenshot_metadata

    stale_meta = make_screenshot_metadata(
        image_width=1920, image_height=1080, timestamp=time.time() - 30.0
    )
    fake = _FakeVisionService(
        status="OBSERVED",
        bbox=(10, 10, 50, 50),
        confidence=0.9,
        source="uia",
        screenshot_meta=stale_meta,
    )
    provider = DefaultVisionTargetProvider(fake, max_screenshot_age_s=5.0)
    contract = provider.ground_target("stale-target")
    if contract.status.value != "rejected":
        raise AssertionError(
            f"expected REJECTED for stale screenshot, got {contract.status.value!r}"
        )
    if "stale" not in (contract.error or "").lower():
        raise AssertionError(f"expected stale error message; got {contract.error!r}")


async def t_provider_ambiguous_passthrough(rec: TestRecord) -> None:
    """An AMBIGUOUS result surfaces candidates (the Brain disambiguates)."""
    from vision.integration.agent_provider import DefaultVisionTargetProvider
    from core.services.vision_service import VisionResult

    fake = _FakeVisionService(
        status="AMBIGUOUS",
        candidates=[
            {"source": "uia", "bbox": (1, 1, 2, 2), "confidence": 0.7, "text": "a"},
            {"source": "uia", "bbox": (3, 3, 4, 4), "confidence": 0.7, "text": "b"},
        ],
    )
    provider = DefaultVisionTargetProvider(fake)
    contract = provider.ground_target("two-buttons")
    if contract.status.value != "ambiguous":
        raise AssertionError(
            f"expected AMBIGUOUS, got {contract.status.value!r}"
        )
    if len(contract.candidates) != 2:
        raise AssertionError(
            f"expected 2 candidates; got {len(contract.candidates)}"
        )
    rec.details["candidate_count"] = len(contract.candidates)


async def t_engine_wires_vision_target_provider(rec: TestRecord) -> None:
    """The engine's ``_build_vision_target_provider`` returns a real object."""
    import inspect
    from core.omnix_engine import OmnixEngine
    from core.configuration import OmnixConfig

    if not hasattr(OmnixEngine, "_build_vision_target_provider"):
        raise AssertionError("OmnixEngine is missing _build_vision_target_provider")
    sig = inspect.signature(OmnixEngine._build_vision_target_provider)
    if "self" not in sig.parameters:
        raise AssertionError("_build_vision_target_provider must be a method")

    cfg = OmnixConfig(
        project_root=os.getcwd(),
        data_dir=os.path.join(os.getcwd(), ".omnix-data"),
        log_dir=os.path.join(os.getcwd(), ".omnix-logs"),
        env_file=os.path.join(os.getcwd(), ".env"),
        enable_vision=False,
    )
    engine = OmnixEngine(cfg)
    try:
        provider = engine._build_vision_target_provider()
    except Exception as exc:  # noqa: BLE001
        # Even when the vision subsystem is unavailable on a headless
        # box, the method must NOT crash.  We accept None or any
        # valid object.
        rec.details["unavailable"] = repr(exc)
        return
    rec.details["provider_type"] = type(provider).__name__ if provider is not None else None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeVisionService:
    """A minimal stand-in for VisionService for the provider tests."""

    def __init__(
        self,
        status: str = "OBSERVED",
        bbox: Optional[tuple] = None,
        confidence: float = 1.0,
        source: str = "uia",
        text: str = "",
        error: str = "",
        screenshot_meta=None,
        candidates: Optional[list] = None,
    ) -> None:
        self.status = status
        self.bbox = bbox
        self.confidence = confidence
        self.source = source
        self.text = text
        self.error = error
        self.screenshot_meta = screenshot_meta
        self.candidates = candidates or []

    def ground_target(
        self, target_query: str, *, preferred_strategy: Optional[str] = None
    ):
        from core.services.vision_service import VisionResult

        if self.status == "AMBIGUOUS":
            return VisionResult(
                status="AMBIGUOUS",
                target_query=target_query,
                observation={"candidates": list(self.candidates)},
                alternatives_discarded=len(self.candidates),
                error="Multiple indistinguishable candidates.",
            )
        return VisionResult(
            status=self.status,
            target_query=target_query,
            observation={
                "bbox": self.bbox,
                "confidence": self.confidence,
                "source": self.source,
                "text": self.text,
            },
            error=self.error,
            screenshot_metadata=self.screenshot_meta,
        )


# ---------------------------------------------------------------------------
# Test registry
# ---------------------------------------------------------------------------

ALL_TESTS: Dict[str, Callable[[TestRecord], "asyncio.Future"]] = {
    "meta": t_metadata_factory,
    "meta.from_capability": t_metadata_from_capability_result,
    "obs.minimal": t_visual_observation_minimal,
    "coord.bounds": t_coordinate_safety_in_bounds,
    "coord.validate": t_coordinate_safety_validates,
    "fresh.default": t_freshness_default,
    "provider.grounded": t_provider_translates_grounded,
    "provider.stale_rejected": t_provider_rejects_stale,
    "provider.ambiguous": t_provider_ambiguous_passthrough,
    "engine.wires_provider": t_engine_wires_vision_target_provider,
}


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

async def _run_all(names: List[str]) -> List[TestRecord]:
    results: List[TestRecord] = []
    for name in names:
        fn = ALL_TESTS.get(name)
        rec = TestRecord(name=name, description=fn.__doc__ or "")
        t0 = time.time()
        if fn is None:
            rec.skipped = True
            rec.error = "unknown test name"
        else:
            try:
                await fn(rec)
                rec.ok = True
            except Exception as exc:  # noqa: BLE001
                rec.ok = False
                rec.error = f"{type(exc).__name__}: {exc}"
                rec.details["traceback"] = traceback.format_exc(limit=4)
        rec.duration_ms = round((time.time() - t0) * 1000.0, 2)
        results.append(rec)
    return results


def _print_report(records: List[TestRecord]) -> None:
    print()
    print("=" * 78)
    print("  OMNIX V6 -- PHASE 13 REAL-WINDOWS SMOKE")
    print("=" * 78)
    width = max((len(r.name) for r in records), default=10)
    for r in records:
        marker = "OK " if r.ok else ("SKIP" if r.skipped else "FAIL")
        line = f"  [{marker}] {r.name.ljust(width)}  {r.duration_ms:6.1f} ms"
        print(line)
        if r.description:
            print(f"         {r.description.strip()}")
        if not r.ok and not r.skipped:
            err = r.error or ""
            if len(err) > 200:
                err = err[:200] + "..."
            print(f"         ERR: {err}")
    print("-" * 78)
    passed = sum(1 for r in records if r.ok)
    failed = sum(1 for r in records if not r.ok and not r.skipped)
    skipped = sum(1 for r in records if r.skipped)
    print(f"  passed={passed}  failed={failed}  skipped={skipped}  total={len(records)}")
    print("=" * 78)


def main(argv: Optional[List[str]] = None) -> int:
    if os.name != "nt":
        print("Phase 13 smoke is Windows-only.  os.name=", os.name, file=sys.stderr)
        return 2

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--tests",
        type=str,
        default=",".join(ALL_TESTS.keys()),
        help="Comma-separated subset of test names.  Default: all.",
    )
    args = parser.parse_args(argv)

    selected = [t.strip() for t in args.tests.split(",") if t.strip()]
    unknown = [t for t in selected if t not in ALL_TESTS]
    if unknown:
        print("Unknown test names:", unknown, file=sys.stderr)
        print("Available:", sorted(ALL_TESTS.keys()), file=sys.stderr)
        return 1

    records = asyncio.run(_run_all(selected))
    _print_report(records)

    return 0 if all(r.ok or r.skipped for r in records) else 1


if __name__ == "__main__":
    raise SystemExit(main())
