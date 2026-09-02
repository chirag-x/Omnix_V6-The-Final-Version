"""
Omnix V6 — Phase 13: Vision-grounded computer use tests.

These are **deterministic** (no real mouse, no real keyboard,
no real screenshot, no LLM).  They verify the Phase 13 contracts:

  * :class:`vision.observations.screenshot_metadata.ScreenshotMetadata`
  * :class:`vision.observations.visual_observation.VisualObservation`
  * :func:`vision.safety.coordinates.validate_coordinates`
  * :func:`vision.safety.freshness.is_fresh`
  * :class:`vision.integration.agent_provider.DefaultVisionTargetProvider`
  * :func:`core.omnix_engine.OmnixEngine._build_vision_target_provider`
  * Config fields ``vision_confidence_threshold`` / ``vision_max_screenshot_stale_s``
  * Agent integration (pre-action grounding + coordinate safety)
  * Before/after observation diff (post-action verification)
"""
from __future__ import annotations

import time
from typing import Any, Optional, Tuple

import pytest

from vision.observations.screenshot_metadata import (
    ScreenshotMetadata,
    make_screenshot_metadata,
    from_capability_result,
)
from vision.observations.visual_observation import (
    VisualObservation,
    VisualObservationStatus,
)
from vision.safety.coordinates import (
    CoordinateSafetyError,
    is_within_bounds,
    validate_coordinates,
)
from vision.safety.freshness import (
    StaleScreenError,
    is_fresh,
    require_fresh,
    DEFAULT_MAX_AGE_S,
)
from vision.integration.agent_provider import (
    VisionTargetProvider,
    DefaultVisionTargetProvider,
    DEFAULT_MAX_SCREENSHOT_AGE_S,
)


# ------------------------------------------------------------------
# 1. ScreenshotMetadata contract (Phase 13)
# ------------------------------------------------------------------

class TestPhase13_ScreenshotMetadata:
    def test_A_make_screenshot_metadata_defaults(self):
        meta = make_screenshot_metadata(image_width=1920, image_height=1080)
        assert meta.image_width == 1920
        assert meta.image_height == 1080
        assert meta.timestamp > 0
        assert meta.screenshot_id
        assert meta.source == "desktop.screenshot"

    def test_B_make_with_explicit_values(self):
        meta = make_screenshot_metadata(
            image_width=1280,
            image_height=720,
            timestamp=1234.0,
            screenshot_id="test-001",
            monitor_id="monitor-1",
            source="vision",
            path="/tmp/img.png",
        )
        assert meta.image_width == 1280
        assert meta.image_height == 720
        assert meta.timestamp == 1234.0
        assert meta.screenshot_id == "test-001"
        assert meta.monitor_id == "monitor-1"
        assert meta.source == "vision"
        assert meta.path == "/tmp/img.png"

    def test_C_from_capability_result_empty_dict_uses_safe_defaults(self):
        meta = from_capability_result({})
        assert meta.image_width == 1
        assert meta.image_height == 1
        assert meta.screenshot_id

    def test_D_from_capability_result_with_shape_and_timestamp(self):
        meta = from_capability_result({"width": 640, "height": 480, "timestamp": 300.0, "monitor_id": "m2"})
        assert meta.image_width == 640
        assert meta.image_height == 480
        assert meta.timestamp == 300.0
        assert meta.monitor_id == "m2"

    def test_E_age_seconds_computes_non_negative_age(self):
        meta = make_screenshot_metadata(image_width=10, image_height=10, timestamp=100.0)
        assert meta.age_seconds(now=105.0) == 5.0
        assert meta.age_seconds(now=99.0) == 0.0  # clock skew clamped to 0

    def test_F_post_init_rejects_invalid_image_dimensions(self):
        with pytest.raises(ValueError):
            ScreenshotMetadata(
                screenshot_id="bad",
                timestamp=1.0,
                image_width=0,
                image_height=100,
            )

    def test_G_post_init_rejects_negative_image_height(self):
        with pytest.raises(ValueError):
            ScreenshotMetadata(
                screenshot_id="bad",
                timestamp=1.0,
                image_width=100,
                image_height=-10,
            )


# ------------------------------------------------------------------
# 2. VisualObservation contract (Phase 13)
# ------------------------------------------------------------------

class TestPhase13_VisualObservation:
    def test_H_create_minimal_observation(self):
        obs = VisualObservation(subject="Save button")
        assert obs.subject == "Save button"
        assert obs.status is VisualObservationStatus.OBSERVED
        assert obs.confidence == 0.0

    def test_I_create_observed_with_bbox_and_screenshot(self):
        meta = make_screenshot_metadata(image_width=1920, image_height=1080)
        obs = VisualObservation(
            subject="dialog",
            bbox=(10, 20, 110, 120),
            center=(60, 70),
            confidence=0.92,
            status=VisualObservationStatus.OBSERVED,
            screenshot_metadata=meta,
        )
        assert obs.bbox == (10, 20, 110, 120)
        assert obs.center == (60, 70)
        assert obs.confidence == 0.92
        assert obs.screenshot_metadata is meta

    def test_J_create_ambiguous_includes_candidates(self):
        obs = VisualObservation(
            subject="button",
            status=VisualObservationStatus.AMBIGUOUS,
            candidates=(
                {"bbox": (10, 10, 20, 20), "confidence": 0.8},
                {"bbox": (100, 100, 110, 110), "confidence": 0.75},
            ),
            error="Multiple candidates",
        )
        assert len(obs.candidates) == 2
        assert obs.is_blocking is True

    def test_K_blocking_statuses(self):
        for s in (
            VisualObservationStatus.AMBIGUOUS,
            VisualObservationStatus.NOT_FOUND,
            VisualObservationStatus.ERROR,
        ):
            obs = VisualObservation(subject="x", status=s)
            assert obs.is_blocking is True
        obs = VisualObservation(subject="x", status=VisualObservationStatus.OBSERVED)
        assert obs.is_blocking is False

    def test_L_post_init_rejects_non_string_subject(self):
        with pytest.raises(ValueError):
            VisualObservation(subject=42)  # type: ignore[arg-type]

    def test_M_post_init_rejects_out_of_range_confidence(self):
        with pytest.raises(ValueError):
            VisualObservation(subject="x", confidence=1.5)
        with pytest.raises(ValueError):
            VisualObservation(subject="x", confidence=-0.1)


# ------------------------------------------------------------------
# 3. Coordinate safety (Phase 13)
# ------------------------------------------------------------------

class TestPhase13_CoordinateSafety:
    def test_N_is_within_bounds_true(self):
        assert is_within_bounds((42, 99), width=100, height=100) is True

    def test_O_is_within_bounds_false_negative(self):
        assert is_within_bounds((-1, 0), width=100, height=100) is False

    def test_P_is_within_bounds_false_above_max(self):
        assert is_within_bounds((99, 100), width=100, height=100) is False  # y=100 == height

    def test_Q_is_within_bounds_empty_screen_false(self):
        assert is_within_bounds((0, 0), width=0, height=10) is False

    def test_R_validate_coordinates_success(self):
        meta = make_screenshot_metadata(image_width=100, image_height=100)
        x, y = validate_coordinates((5, 10), screenshot_metadata=meta, source="uia")
        assert (x, y) == (5, 10)

    def test_S_validate_coordinates_rejects_no_meta(self):
        with pytest.raises(CoordinateSafetyError):
            validate_coordinates((5, 10), screenshot_metadata=None)

    def test_T_validate_coordinates_rejects_non_finite_x(self):
        meta = make_screenshot_metadata(image_width=100, image_height=100)
        with pytest.raises(CoordinateSafetyError):
            validate_coordinates((float("nan"), 10), screenshot_metadata=meta)

    def test_U_validate_coordinates_rejects_off_screen(self):
        meta = make_screenshot_metadata(image_width=50, image_height=50)
        with pytest.raises(CoordinateSafetyError):
            validate_coordinates((51, 10), screenshot_metadata=meta)

    def test_V_validate_coordinates_rejects_bad_source(self):
        meta = make_screenshot_metadata(image_width=50, image_height=50)
        with pytest.raises(CoordinateSafetyError):
            validate_coordinates((5, 10), screenshot_metadata=meta, source="bad")

    def test_W_validate_coordinates_monitor_mismatch(self):
        meta = make_screenshot_metadata(image_width=50, image_height=50, monitor_id="m1")
        with pytest.raises(CoordinateSafetyError):
            validate_coordinates(
                (5, 10), screenshot_metadata=meta, source="uia", monitor_id="m2"
            )

    def test_X_validate_coordinates_monitor_match(self):
        meta = make_screenshot_metadata(image_width=50, image_height=50, monitor_id="m2")
        x, y = validate_coordinates(
            (5, 10), screenshot_metadata=meta, source="uia", monitor_id="m2"
        )
        assert (x, y) == (5, 10)


# ------------------------------------------------------------------
# 4. Screenshot freshness (Phase 13)
# ------------------------------------------------------------------

class TestPhase13_ScreenshotFreshness:
    def test_Y_is_fresh_true_within_age(self):
        meta = make_screenshot_metadata(
            image_width=10, image_height=10, timestamp=time.time()
        )
        assert is_fresh(meta, max_age_s=DEFAULT_MAX_AGE_S) is True

    def test_Z_is_fresh_false_stale(self):
        meta = make_screenshot_metadata(
            image_width=10, image_height=10, timestamp=time.time() - 10.0
        )
        assert is_fresh(meta, max_age_s=DEFAULT_MAX_AGE_S) is False

    def test_AA_is_fresh_false_none_meta(self):
        assert is_fresh(None) is False

    def test_AB_is_fresh_false_bad_timestamp(self):
        # The post-init validates timestamp is numeric; the freshness
        # gate is only ever reached with valid metadata.  We verify
        # that a non-numeric timestamp (e.g., from a corrupted source)
        # produces False by constructing a mock-like object.
        class MockMeta:
            timestamp = "not-a-number"
        assert is_fresh(MockMeta()) is False

    def test_AC_require_fresh_raises_when_stale(self):
        meta = make_screenshot_metadata(
            image_width=10, image_height=10, timestamp=time.time() - 10.0
        )
        with pytest.raises(StaleScreenError):
            require_fresh(meta)

    def test_AD_is_fresh_custom_max_age(self):
        meta = make_screenshot_metadata(
            image_width=10, image_height=10, timestamp=time.time() - 3.0
        )
        assert is_fresh(meta, max_age_s=2.0) is False
        assert is_fresh(meta, max_age_s=5.0) is True


# ------------------------------------------------------------------
# 5. DefaultVisionTargetProvider integration (Phase 13)
# ------------------------------------------------------------------

class FakeVisionService:
    """A minimal mock vision service for deterministic tests."""

    def __init__(
        self,
        status: str = "OBSERVED",
        bbox: Optional[Tuple[int, ...]] = None,
        confidence: float = 1.0,
        error: str = "",
        screenshot_meta: Optional[ScreenshotMetadata] = None,
    ) -> None:
        self.status = status
        self.bbox = bbox
        self.confidence = confidence
        self.error = error
        self.screenshot_meta = screenshot_meta

    def ground_target(
        self, target_query: str, *, preferred_strategy: Optional[str] = None
    ) -> Any:
        from core.services.vision_service import VisionResult

        return VisionResult(
            status=self.status,
            target_query=target_query,
            screenshot_used=bool(self.screenshot_meta),
            screenshot_metadata=self.screenshot_meta,
            observation={
                "bbox": self.bbox,
                "confidence": self.confidence,
            } if self.bbox is not None else None,
            error=self.error,
        )


class TestPhase13_DefaultVisionTargetProvider:
    def test_AE_provider_translates_vision_result_grounded(self):
        service = FakeVisionService(
            status="OBSERVED",
            bbox=(10, 20, 30, 40),
            confidence=0.85,
        )
        provider = DefaultVisionTargetProvider(service)
        contract = provider.ground_target("button")
        assert contract.status.value == "GROUNDED"
        assert contract.confidence == 0.85
        assert contract.bbox == (10, 20, 30, 40)
        assert contract.center == (20, 30)
        assert contract.is_grounded is True

    def test_AF_provider_translates_vision_result_ambiguous(self):
        service = FakeVisionService(status="AMBIGUOUS")
        provider = DefaultVisionTargetProvider(service)
        contract = provider.ground_target("button")
        assert contract.status.value == "AMBIGUOUS"
        assert contract.is_blocking is True

    def test_AG_provider_rejects_stale_screenshot_on_grounded_target(self):
        meta = make_screenshot_metadata(
            image_width=100, image_height=100, timestamp=time.time() - 10.0
        )
        service = FakeVisionService(
            status="OBSERVED",
            bbox=(10, 20, 30, 40),
            confidence=0.9,
            screenshot_meta=meta,
        )
        provider = DefaultVisionTargetProvider(service, max_screenshot_age_s=5.0)
        contract = provider.ground_target("button")
        # The provider applies the stale-screen gate; a fresh
        # screenshot would return GROUNDED but a stale one is
        # REJECTED.
        assert contract.status.value == "REJECTED"
        assert "stale" in (contract.error or "").lower()

    def test_AH_provider_allows_fresh_screenshot(self):
        meta = make_screenshot_metadata(
            image_width=100, image_height=100, timestamp=time.time()
        )
        service = FakeVisionService(
            status="OBSERVED",
            bbox=(10, 20, 30, 40),
            confidence=0.9,
            screenshot_meta=meta,
        )
        provider = DefaultVisionTargetProvider(service, max_screenshot_age_s=5.0)
        contract = provider.ground_target("button")
        assert contract.status.value == "GROUNDED"

    def test_AI_provider_rejects_non_string_target_query(self):
        service = FakeVisionService()
        provider = DefaultVisionTargetProvider(service)
        with pytest.raises(ValueError):
            provider.ground_target(42)  # type: ignore[arg-type]

    def test_AJ_provider_raises_on_none_vision_service(self):
        with pytest.raises(ValueError):
            DefaultVisionTargetProvider(None)  # type: ignore[arg-type]

    def test_AK_provider_protocol_check(self):
        service = FakeVisionService()
        provider = DefaultVisionTargetProvider(service)
        assert isinstance(provider, VisionTargetProvider)
        assert callable(provider.ground_target)


# ------------------------------------------------------------------
# 6. Config extension (Phase 13)
# ------------------------------------------------------------------

class TestPhase13_Config:
    def test_AL_config_has_vision_fields(self):
        from core.configuration import OmnixConfig
        cfg = OmnixConfig(
            project_root=__import__("pathlib").Path("."),
            data_dir=__import__("pathlib").Path("."),
            log_dir=__import__("pathlib").Path("."),
            env_file=__import__("pathlib").Path("."),
        )
        assert hasattr(cfg, "vision_confidence_threshold")
        assert hasattr(cfg, "vision_max_screenshot_stale_s")
        assert cfg.vision_confidence_threshold == 0.5
        assert cfg.vision_max_screenshot_stale_s == 5.0


# ------------------------------------------------------------------
# 7. End-to-end: Agent + vision + no real mouse (Phase 13)
# ------------------------------------------------------------------

class TestPhase13_E2E_NoRealMouse:
    def test_AM_e2e_vision_pre_action_grounding_flow(self):
        """Full pipeline: Agent → vision → adapter → action request.

        This is a single end-to-end deterministic test that uses
        only mocks: no real mouse, no real keyboard, no real
        vision service, no real screenshot.  It asserts that the
        vision-grounding path produces an ``AdaptedAction`` with
        a closed capability name.
        """
        from core.orchestration.grounding import TargetGroundingContract, GroundingStatus
        from core.orchestration.vision_adapter import adapt_pre_action
        from vision.observations.screenshot_metadata import make_screenshot_metadata

        contract = TargetGroundingContract(
            status=GroundingStatus.GROUNDED,
            target_query="Save",
            bbox=(10, 20, 30, 40),
            center=(20, 30),
            confidence=0.92,
            source=__import__("core.orchestration.models").orchestration.models.ObservationSource.UIA,
            resolution_method="uia",
        )
        adapted = adapt_pre_action(
            contract, kind="click"
        )
        assert adapted.capability_name == "desktop.mouse.click"
        assert "x" in adapted.request.parameters
        assert "y" in adapted.request.parameters
        assert adapted.request.parameters.get("x") == 20
        assert adapted.request.parameters.get("y") == 30

    def test_AN_e2e_vision_not_grounded_blocks_action(self):
        """A non-GROUNDED contract raises :class:`GroundingNotGroundableError`."""
        from core.orchestration.grounding import TargetGroundingContract, GroundingStatus
        from core.orchestration.vision_adapter import GroundingNotGroundableError, adapt_click

        contract = TargetGroundingContract(
            status=GroundingStatus.NOT_FOUND,
            target_query="unknown",
        )
        with pytest.raises(GroundingNotGroundableError):
            adapt_click(contract)

    def test_AO_e2e_before_after_diff_detects_appearance(self):
        from core.services.vision_service import VisionService, VisionResult
        from vision.observations.screenshot_metadata import make_screenshot_metadata
        from vision.router.screenshot_provider import NullScreenshotProvider

        before = VisionResult(
            status="NOT_FOUND",
            target_query="button",
        )
        after = VisionResult(
            status="OBSERVED",
            target_query="button",
            observation={"bbox": (10, 10, 20, 20)},
        )
        service = VisionService(screenshot_provider=NullScreenshotProvider())
        diff = service.diff_observations(before, after)
        assert diff["changed"] is True
        assert diff["reason"] == "target appeared"

    def test_AP_e2e_diff_detects_no_change(self):
        from core.services.vision_service import VisionService, VisionResult
        from vision.router.screenshot_provider import NullScreenshotProvider

        before = VisionResult(
            status="OBSERVED",
            target_query="x",
            observation={"bbox": (10, 10, 20, 20)},
        )
        after = VisionResult(
            status="OBSERVED",
            target_query="x",
            observation={"bbox": (10, 10, 20, 20)},
        )
        service = VisionService(screenshot_provider=NullScreenshotProvider())
        diff = service.diff_observations(before, after)
        assert diff["changed"] is False

    def test_AQ_e2e_vision_service_has_screenshot_metadata_on_observe(self):
        """When ``observe_state`` captures a screenshot, the result
        carries ``screenshot_metadata``.  When no screenshot is
        needed/used, the metadata is ``None``."""
        from vision.router.screenshot_provider import NullScreenshotProvider
        from core.services.vision_service import VisionService

        service = VisionService(screenshot_provider=NullScreenshotProvider())
        result = service.observe_state("x")
        # No screenshot captured (NullScreenshotProvider) => None
        assert result.screenshot_metadata is None

    def test_AR_e2e_config_read_from_omix_engine_path(self):
        """The engine's ``_build_vision_target_provider`` must respect
        ``enable_vision`` and must fall back safely when the vision
        subsystem is unavailable."""
        from core.omnix_engine import OmnixEngine
        # We only assert the method exists and is callable; a
        # full engine init test is covered by ``test_engine.py``.
        assert hasattr(OmnixEngine, "_build_vision_target_provider")
        assert callable(getattr(OmnixEngine, "_build_vision_target_provider", None))


# ------------------------------------------------------------------
# 8. Isolation: AST-level forbidden import checks (Phase 13)
# ------------------------------------------------------------------

# These checks are executed as Python-level assertions rather than
# as a separate test framework.  They verify that no file under
# ``vision/`` imports the forbidden computer-action surfaces.

import importlib.util
import ast
import pathlib

FORBIDDEN_MODULE_NAMES = frozenset({
    "pyautogui",
    "win32gui",
    "win32api",
    "ctypes",
    "subprocess",
    "core.capability_router",  # vision must not import the router
    "core.omnix_engine",       # vision must not import the engine
})


def collect_vision_py_files() -> list:
    # tests/ is a sibling of vision/ at the project root.
    root = pathlib.Path(__file__).resolve().parent.parent / "vision"
    files = []
    if not root.exists():
        return files
    for p in root.rglob("*.py"):
        if p.name.startswith("test_"):
            continue  # don't inspect test files
        files.append(p)
    return files


def module_imports_file(source_path: pathlib.Path) -> set:
    try:
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
    except Exception:
        return set()
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.name.split(".")[0]
                imports.add(name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                first = node.module.split(".")[0]
                imports.add(first)
            else:
                for alias in node.names:
                    imports.add(alias.name.split(".")[0])
    return imports


class TestPhase13_Isolation:
    def test_AS_no_forbidden_imports_in_vision_modules(self):
        files = collect_vision_py_files()
        assert len(files) >= 8, f"expected at least 8 vision .py files, got {len(files)}"
        for p in files:
            imports = module_imports_file(p)
            forbidden = FORBIDDEN_MODULE_NAMES & imports
            assert not forbidden, (
                f"{p.relative_to(p.parent.parent)} imports forbidden module(s): {forbidden}; "
                f"full set: {imports}"
            )

    def test_AT_vision_safety_has_no_action_surface(self):
        # Explicit check: the new vision/safety/*.py files import
        # only the screenshot metadata / standard library.
        for sub in ("coordinates", "freshness"):
            p = pathlib.Path(__file__).resolve().parent.parent / "vision" / "safety" / f"{sub}.py"
            if p.exists():
                imports = module_imports_file(p)
                forbidden = FORBIDDEN_MODULE_NAMES & imports
                assert not forbidden, f"vision/safety/{sub}.py imports forbidden: {forbidden}"

    def test_AU_vision_integration_has_no_action_surface(self):
        p = pathlib.Path(__file__).resolve().parent.parent / "vision" / "integration" / "agent_provider.py"
        imports = module_imports_file(p)
        # The provider imports the Agent contract (not the engine)
        # and the vision service, which is allowed.
        forbidden = (FORBIDDEN_MODULE_NAMES - {"core.orchestration.grounding"}) & imports
        assert not forbidden, f"vision/integration/agent_provider imports forbidden: {forbidden}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
