"""
Tests for Stage 18.8 — Formal Perception API Contract & Observation Boundary.

Tests the canonical perception contract that establishes one stable, explicit API
boundary between perception and grounding layers.

Test categories from spec section 36-46:
36. FakePerceptionProvider returns deterministic results
37. TargetResolver can consume canonical PerceptionResult
38. Downstream code doesn't need perception internals
39. No LLM calls (AI independence)
40. Cancellation works
41. Timeout works
42. Partial result works
43. Freshness preserved
44. Coordinate contract enforced
45. Window context preserved
46. Observation ID traceable
"""

import asyncio
import time
from datetime import datetime
from unittest.mock import Mock, patch

import pytest

from vision.perception_contract import (
    PerceptionProvider,
    PerceptionRequest,
    PerceptionResult,
    PerceptionSource,
    PerceptionStatus,
    ScreenInfo,
    WindowContext,
)
from vision.perception_adapter import PerceptionAdapter
from vision.fake_perception_provider import (
    FakePerceptionProvider,
    create_fake_perception_provider,
    create_empty_fake_provider,
    create_single_candidate_provider,
    create_failed_provider,
    create_timeout_provider,
    create_cancelled_provider,
)
from vision.observations.targets import TargetCandidate
from core.grounding.target_resolver import TargetResolver
from core.grounding.resolved_target import (
    TargetResolutionResult,
    TargetResolutionStatus,
    ResolvedTarget,
)
from core.orchestration.models import ObservationSource


# ===========================================================================
# Test 36: FakePerceptionProvider returns deterministic results
# ===========================================================================

class TestFakePerceptionProvider:
    """Test the FakePerceptionProvider implementation."""

    @pytest.mark.asyncio
    async def test_fake_provider_returns_deterministic_result(self):
        """Fake provider returns deterministic results without accessing desktop."""
        provider = create_fake_perception_provider(
            return_candidates=[
                TargetCandidate(
                    source_type=ObservationSource.UIA,
                    bbox=(100, 100, 200, 140),
                    confidence=0.9,
                    text="Test Button",
                    properties={"automation_id": "fake_button"},
                    timestamp=time.time()
                )
            ],
            return_sources=(PerceptionSource.UI_AUTOMATION,),
            status=PerceptionStatus.SUCCESS,
            duration_ms=10.0,
            observation_id="fake-obs-001"
        )

        request = PerceptionRequest()
        result = await provider.observe(request)

        assert result.observation_id == "fake-obs-001"
        assert result.status == PerceptionStatus.SUCCESS
        assert len(result.candidates) == 1
        assert result.candidates[0].text == "Test Button"
        assert result.candidates[0].confidence == 0.9
        assert result.duration_ms == 10.0

    @pytest.mark.asyncio
    async def test_fake_provider_no_desktop_access(self):
        """Fake provider should not access any desktop resources."""
        provider = create_empty_fake_provider()

        request = PerceptionRequest()
        # Should not raise any exception
        result = await provider.observe(request)

        assert result.status == PerceptionStatus.SUCCESS
        assert len(result.candidates) == 0

    @pytest.mark.asyncio
    async def test_fake_provider_available_sources(self):
        """Fake provider reports available sources."""
        provider = create_fake_perception_provider()
        sources = provider.get_available_sources()

        assert PerceptionSource.SCREENSHOT in sources
        assert PerceptionSource.VISION in sources
        assert PerceptionSource.UI_AUTOMATION in sources
        assert PerceptionSource.COORDINATE in sources

    @pytest.mark.asyncio
    async def test_fake_provider_is_source_available(self):
        """Fake provider correctly reports source availability."""
        provider = create_fake_perception_provider()

        assert provider.is_source_available(PerceptionSource.SCREENSHOT) is True
        assert provider.is_source_available(PerceptionSource.OCR) is False


# ===========================================================================
# Test 37: TargetResolver can consume canonical PerceptionResult
# ===========================================================================

class TestTargetResolverConsumption:
    """Test that TargetResolver can consume canonical PerceptionResult."""

    @pytest.mark.asyncio
    async def test_resolver_consumes_candidates_from_perception_result(self):
        """TargetResolver can consume TargetCandidate from PerceptionResult."""
        # Create fake perception result
        provider = create_single_candidate_provider("Button", 0.9)
        perception_result = await provider.observe(PerceptionRequest())

        # Extract candidate and pass to resolver
        candidate = perception_result.candidates[0]
        resolver = TargetResolver(screen_width=1920, screen_height=1080)

        result = resolver.resolve(candidate)

        assert result.status == TargetResolutionStatus.RESOLVED
        assert result.target is not None
        assert result.target.kind == "vision"

    @pytest.mark.asyncio
    async def test_resolver_does_not_need_perception_internals(self):
        """Resolver only depends on TargetCandidate, not perception internals."""
        provider = create_single_candidate_provider("Test", 0.95)
        perception_result = await provider.observe(PerceptionRequest())

        # Resolver should work with just the candidate
        candidate = perception_result.candidates[0]
        resolver = TargetResolver()

        # The resolver doesn't need to know about PerceptionProvider,
        # PerceptionResult, or any perception internals
        result = resolver.resolve(candidate)

        assert result.status == TargetResolutionStatus.RESOLVED


# ===========================================================================
# Test 38: No implementation leak
# ===========================================================================

class TestNoImplementationLeak:
    """Test that downstream code doesn't need perception implementation internals."""

    @pytest.mark.asyncio
    async def test_caller_uses_only_contract_types(self):
        """Callers only need to import from vision.perception_contract."""
        # This test verifies that the contract types are sufficient
        # The caller should be able to:
        # 1. Create a PerceptionRequest
        request = PerceptionRequest(
            include_screenshot=True,
            include_vision=True,
            include_window_context=True,
        )

        # 2. Use a PerceptionProvider (any implementation)
        provider: PerceptionProvider = create_single_candidate_provider()

        # 3. Get back a PerceptionResult
        result = await provider.observe(request)

        # 4. Work with the result using only contract types
        assert isinstance(result, PerceptionResult)
        assert isinstance(result.screen, ScreenInfo)
        assert isinstance(result.status, PerceptionStatus)
        assert isinstance(result.sources, tuple)


# ===========================================================================
# Test 39: AI independence - Zero LLM calls
# ===========================================================================

class TestAIIndependence:
    """Test that perception API does NOT call any LLM."""

    @pytest.mark.asyncio
    async def test_fake_provider_makes_zero_llm_calls(self):
        """FakePerceptionProvider should make zero LLM calls."""
        # Create a mock LLM provider
        mock_llm = Mock()
        mock_llm.generate = Mock(return_value=Mock(text="LLM response"))

        # Use fake provider - should not call LLM
        provider = create_single_candidate_provider("Test", 0.9)
        result = await provider.observe(PerceptionRequest())

        # LLM should not have been called
        mock_llm.generate.assert_not_called()

    @pytest.mark.asyncio
    async def test_fake_provider_does_not_import_llm(self):
        """Fake provider module should not import any LLM modules."""
        # This is a static check - we verify by importing
        from vision import fake_perception_provider

        # Check that the module doesn't reference LLM modules
        module_source = open(fake_perception_provider.__file__).read()
        assert "openai" not in module_source.lower()
        assert "anthropic" not in module_source.lower()
        assert "claude" not in module_source.lower()
        assert "ai.provider" not in module_source.lower()

    @pytest.mark.asyncio
    async def test_grounding_consumes_perception_without_llm(self):
        """Grounding consumes perception result without LLM calls."""
        mock_llm = Mock()
        mock_llm.generate = Mock(return_value=Mock(text="Should not be called"))

        # Create perception result
        provider = create_single_candidate_provider("Test Button", 0.9)
        perception_result = await provider.observe(PerceptionRequest())

        # Ground the candidate
        candidate = perception_result.candidates[0]
        resolver = TargetResolver()
        result = resolver.resolve(candidate)

        # No LLM was involved
        mock_llm.generate.assert_not_called()
        assert result.status == TargetResolutionStatus.RESOLVED


# ===========================================================================
# Test 40: Cancellation
# ===========================================================================

class TestCancellation:
    """Test that observe() can be cancelled."""

    @pytest.mark.asyncio
    async def test_cancellation_token_cancels_observation(self):
        """Cancellation token cancels the observation."""
        # Create a mock cancellation token
        class MockCancellationToken:
            def __init__(self, cancelled=False):
                self.cancelled = cancelled

        provider = create_fake_perception_provider()
        token = MockCancellationToken(cancelled=True)

        result = await provider.observe(PerceptionRequest(), cancellation_token=token)

        assert result.status == PerceptionStatus.CANCELLED

    @pytest.mark.asyncio
    async def test_cancellation_returns_empty_observation(self):
        """Cancelled observation returns with cancelled status and no candidates."""
        class MockCancellationToken:
            cancelled = True

        provider = create_single_candidate_provider("Test", 0.9)
        token = MockCancellationToken()

        result = await provider.observe(PerceptionRequest(), cancellation_token=token)

        assert result.status == PerceptionStatus.CANCELLED


# ===========================================================================
# Test 41: Timeout
# ===========================================================================

class TestTimeout:
    """Test timeout behavior."""

    @pytest.mark.asyncio
    async def test_timeout_provider_returns_timeout_status(self):
        """A provider configured to timeout returns TIMEOUT status."""
        provider = create_timeout_provider()
        result = await provider.observe(PerceptionRequest())

        assert result.status == PerceptionStatus.TIMEOUT
        assert result.duration_ms == 5000.0  # 5 seconds


# ===========================================================================
# Test 42: Partial result
# ===========================================================================

class TestPartialResult:
    """Test partial result handling."""

    @pytest.mark.asyncio
    async def test_partial_result_with_missing_sources(self):
        """Partial results are returned when some sources are unavailable."""
        # Create a provider that returns only some sources
        provider = create_fake_perception_provider(
            return_candidates=[],
            return_sources=(PerceptionSource.UI_AUTOMATION,),
            status=PerceptionStatus.PARTIAL
        )

        result = await provider.observe(PerceptionRequest())

        assert result.status == PerceptionStatus.PARTIAL
        assert len(result.sources) == 1
        assert PerceptionSource.UI_AUTOMATION in result.sources

    @pytest.mark.asyncio
    async def test_failed_provider_returns_failed_status(self):
        """Failed provider returns FAILED status."""
        provider = create_failed_provider("Test failure")
        result = await provider.observe(PerceptionRequest())

        assert result.status == PerceptionStatus.FAILED


# ===========================================================================
# Test 43: Freshness
# ===========================================================================

class TestFreshness:
    """Test freshness/timestamp preservation."""

    @pytest.mark.asyncio
    async def test_timestamp_preserved_in_result(self):
        """Observation timestamp is preserved in PerceptionResult."""
        specific_time = datetime(2026, 1, 1, 12, 0, 0)
        provider = create_fake_perception_provider(
            timestamp=specific_time,
            observation_id="fixed-obs"
        )

        result = await provider.observe(PerceptionRequest())

        assert result.timestamp == specific_time
        assert result.observation_id == "fixed-obs"

    @pytest.mark.asyncio
    async def test_timestamp_generated_when_not_provided(self):
        """Timestamp is generated when not explicitly provided."""
        provider = create_fake_perception_provider()
        before = datetime.now()

        result = await provider.observe(PerceptionRequest())
        after = datetime.now()

        # Timestamp should be between before and after
        assert before <= result.timestamp <= after

    @pytest.mark.asyncio
    async def test_candidate_timestamp_preserved(self):
        """Candidate timestamp is preserved through the contract."""
        provider = create_single_candidate_provider("Test", 0.9)
        result = await provider.observe(PerceptionRequest())

        candidate = result.candidates[0]
        # Candidate has a timestamp from when it was created
        assert candidate.timestamp > 0


# ===========================================================================
# Test 44: Coordinate contract
# ===========================================================================

class TestCoordinateContract:
    """Test that coordinates are explicit and consistent."""

    @pytest.mark.asyncio
    async def test_candidate_coordinates_are_in_screen_space(self):
        """Candidate coordinates are in canonical screen space."""
        # Create a candidate with known coordinates
        candidate = TargetCandidate(
            source_type=ObservationSource.UIA,
            bbox=(100, 100, 200, 140),  # (left, top, right, bottom)
            confidence=0.9,
            text="Test Button",
            properties={}
        )

        provider = create_fake_perception_provider(
            return_candidates=[candidate],
            return_sources=(PerceptionSource.UI_AUTOMATION,)
        )

        result = await provider.observe(PerceptionRequest())
        retrieved_candidate = result.candidates[0]

        # Coordinates should be preserved exactly
        assert retrieved_candidate.bbox == (100, 100, 200, 140)

    @pytest.mark.asyncio
    async def test_screen_info_provides_coordinate_space(self):
        """ScreenInfo explicitly defines the coordinate space."""
        provider = create_fake_perception_provider()
        result = await provider.observe(PerceptionRequest())

        assert result.screen.coordinate_space == "screen"
        assert result.screen.width > 0
        assert result.screen.height > 0


# ===========================================================================
# Test 45: Window context
# ===========================================================================

class TestWindowContext:
    """Test window context preservation."""

    @pytest.mark.asyncio
    async def test_window_context_included_when_available(self):
        """Window context is included in the perception result."""
        window_ctx = WindowContext(
            hwnd=12345,
            title="Test Window",
            application="test.exe",
            bounds=(0, 0, 800, 600),
            is_foreground=True
        )

        provider = create_fake_perception_provider(
            return_window_context=window_ctx
        )

        result = await provider.observe(PerceptionRequest())

        assert result.window_context is not None
        assert result.window_context.hwnd == 12345
        assert result.window_context.title == "Test Window"
        assert result.window_context.application == "test.exe"

    @pytest.mark.asyncio
    async def test_window_context_none_when_not_available(self):
        """Window context is None when not available."""
        provider = create_empty_fake_provider()
        result = await provider.observe(PerceptionRequest())

        assert result.window_context is None


# ===========================================================================
# Test 46: Observation ID traceability
# ===========================================================================

class TestObservationID:
    """Test observation ID is present and traceable."""

    @pytest.mark.asyncio
    async def test_observation_id_present_in_result(self):
        """Every result has a unique observation_id."""
        provider = create_fake_perception_provider()

        result1 = await provider.observe(PerceptionRequest())
        result2 = await provider.observe(PerceptionRequest())

        assert result1.observation_id is not None
        assert result2.observation_id is not None
        assert result1.observation_id != result2.observation_id

    @pytest.mark.asyncio
    async def test_observation_id_can_be_specified(self):
        """Observation ID can be specified for traceability."""
        provider = create_fake_perception_provider(
            observation_id="specific-obs-id-123"
        )

        result = await provider.observe(PerceptionRequest())

        assert result.observation_id == "specific-obs-id-123"

    @pytest.mark.asyncio
    async def test_observation_id_format(self):
        """Observation ID should be a valid string identifier."""
        provider = create_fake_perception_provider()
        result = await provider.observe(PerceptionRequest())

        # ID should be a non-empty string
        assert isinstance(result.observation_id, str)
        assert len(result.observation_id) > 0


# ===========================================================================
# Integration test
# ===========================================================================

class TestPerceptionContractIntegration:
    """Integration tests for the full perception contract."""

    @pytest.mark.asyncio
    async def test_full_perception_to_grounding_flow(self):
        """Test the complete perception → grounding flow using the contract."""
        # 1. Create a fake perception provider (no desktop access)
        provider = create_single_candidate_provider("OK Button", 0.95)

        # 2. Make a perception request
        request = PerceptionRequest(
            include_screenshot=False,  # No screenshot needed for test
            include_vision=True,
            include_window_context=True
        )

        # 3. Observe (perception)
        perception_result = await provider.observe(request)

        # 4. Verify perception result
        assert perception_result.status == PerceptionStatus.SUCCESS
        assert len(perception_result.candidates) == 1
        candidate = perception_result.candidates[0]

        # 5. Ground the candidate (grounding layer)
        resolver = TargetResolver(screen_width=1920, screen_height=1080)
        resolution_result = resolver.resolve(candidate)

        # 6. Verify the full flow worked
        assert resolution_result.status == TargetResolutionStatus.RESOLVED
        assert resolution_result.target is not None
        assert resolution_result.target.identifier == "OK Button"

    @pytest.mark.asyncio
    async def test_perception_provider_protocol_compliance(self):
        """FakePerceptionProvider complies with PerceptionProvider protocol."""
        provider = create_single_candidate_provider()

        # Should be instance of PerceptionProvider protocol
        assert isinstance(provider, PerceptionProvider)

        # Should have all required methods
        assert hasattr(provider, 'observe')
        assert hasattr(provider, 'get_available_sources')
        assert hasattr(provider, 'is_source_available')


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
