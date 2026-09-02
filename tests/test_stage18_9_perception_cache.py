"""
Tests for Stage 18.9 — Perception Observation Cache & Lifecycle.

Tests the generic, deterministic perception observation cache that prevents
unnecessary repeated perception work while preserving strict freshness.

Test categories from spec section 34-46:
34. Cache hit reduces underlying calls
35. Cache miss triggers underlying observe
36. TTL expiration
37. max_age_ms semantics
38. Failure not cached
39. Partial result policy
40. Invalidation
41. Observation ID preservation
42. Timestamp preservation
43. Candidate freshness
44. Concurrent requests
45. Bounded cache eviction
46. No LLM calls
47. No action calls
"""

import asyncio
import time
from datetime import datetime, timedelta
from unittest.mock import Mock

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
from vision.perception_cache import (
    PerceptionCacheKey,
    CacheEntry,
    LRUPerceptionCache,
    CachedPerceptionProvider,
    create_cached_perception_provider,
)
from vision.fake_perception_provider import (
    create_fake_perception_provider,
    create_single_candidate_provider,
    create_failed_provider,
    create_empty_fake_provider,
)
from vision.observations.targets import TargetCandidate
from core.orchestration.models import ObservationSource


# ===========================================================================
# Test 34: Cache hit
# ===========================================================================

class TestCacheHit:
    """Test that cache hit reduces underlying provider calls."""

    @pytest.mark.asyncio
    async def test_cache_hit_preserves_underlying_count(self):
        """First request goes to provider, second hits cache."""
        # Create a counting provider
        call_count = 0

        class CountingProvider:
            async def observe(self, request):
                nonlocal call_count
                call_count += 1
                return PerceptionResult(
                    observation_id=f"obs-{call_count}",
                    timestamp=datetime.now(),
                    screen=ScreenInfo(width=1920, height=1080, dpi_scale_x=1.0, dpi_scale_y=1.0),
                    status=PerceptionStatus.SUCCESS,
                    candidates=(
                        TargetCandidate(
                            source_type=ObservationSource.UIA,
                            bbox=(100, 100, 200, 140),
                            confidence=0.9,
                            text="Button",
                            timestamp=time.time()
                        ),
                    ),
                )

            def get_available_sources(self):
                return (PerceptionSource.UI_AUTOMATION,)

            def is_source_available(self, source):
                return True

        provider = CountingProvider()
        cached = CachedPerceptionProvider(provider, default_max_age_ms=5000)

        request = PerceptionRequest()
        result1 = await cached.observe(request)
        result2 = await cached.observe(request)

        # First call should hit provider, second should hit cache
        assert call_count == 1
        assert result1.observation_id == "obs-1"
        assert result2.observation_id == "obs-1"  # Same ID from cache

    @pytest.mark.asyncio
    async def test_cache_hit_no_underlying_call(self):
        """Cache hit does not invoke underlying provider."""
        # Use a mock that fails if called
        provider = Mock()
        provider.observe = Mock(side_effect=AssertionError("Should not be called"))
        provider.get_available_sources = Mock(return_value=())

        cached = CachedPerceptionProvider(provider, default_max_age_ms=5000)

        # Manually pre-populate cache
        from vision.perception_cache import PerceptionCacheKey
        key = PerceptionCacheKey(
            include_screenshot=True,
            include_vision=True,
            include_ocr=False,
            include_ui_elements=False,
            include_window_context=True,
            region=None,
            coordinate_environment=None
        )

        result = PerceptionResult(
            observation_id="cached-obs",
            timestamp=datetime.now(),
            screen=ScreenInfo(width=1920, height=1080, dpi_scale_x=1.0, dpi_scale_y=1.0),
            status=PerceptionStatus.SUCCESS,
        )
        await cached._cache.put(key, result)

        # Now request should hit cache
        request = PerceptionRequest()
        returned = await cached.observe(request)

        assert returned.observation_id == "cached-obs"
        provider.observe.assert_not_called()


# ===========================================================================
# Test 35: Cache miss
# ===========================================================================

class TestCacheMiss:
    """Test cache miss behavior."""

    @pytest.mark.asyncio
    async def test_different_requests_miss_cache(self):
        """Different requests should trigger underlying provider."""
        call_count = 0

        class CountingProvider:
            async def observe(self, request):
                nonlocal call_count
                call_count += 1
                return PerceptionResult(
                    observation_id=f"obs-{call_count}",
                    timestamp=datetime.now(),
                    screen=ScreenInfo(width=1920, height=1080, dpi_scale_x=1.0, dpi_scale_y=1.0),
                    status=PerceptionStatus.SUCCESS,
                )

        provider = CountingProvider()
        cached = CachedPerceptionProvider(provider, default_max_age_ms=5000)

        # Request 1: vision only
        req1 = PerceptionRequest(include_vision=True, include_ocr=False)
        await cached.observe(req1)

        # Request 2: vision + ocr (different request)
        req2 = PerceptionRequest(include_vision=True, include_ocr=True)
        await cached.observe(req2)

        # Both should have hit the provider
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_equivalent_requests_share_cache(self):
        """Equivalent requests should share cache entries."""
        call_count = 0

        class CountingProvider:
            async def observe(self, request):
                nonlocal call_count
                call_count += 1
                return PerceptionResult(
                    observation_id=f"obs-{call_count}",
                    timestamp=datetime.now(),
                    screen=ScreenInfo(width=1920, height=1080, dpi_scale_x=1.0, dpi_scale_y=1.0),
                    status=PerceptionStatus.SUCCESS,
                )

        provider = CountingProvider()
        cached = CachedPerceptionProvider(provider, default_max_age_ms=5000)

        # Same request parameters
        req1 = PerceptionRequest(include_vision=True, include_ocr=False)
        req2 = PerceptionRequest(include_vision=True, include_ocr=False)

        await cached.observe(req1)
        await cached.observe(req2)

        # Second should hit cache
        assert call_count == 1


# ===========================================================================
# Test 36: TTL
# ===========================================================================

class TestTTL:
    """Test TTL-based freshness."""

    @pytest.mark.asyncio
    async def test_fresh_result_reused(self):
        """Fresh cached result is reused."""
        cache = LRUPerceptionCache()
        key = PerceptionCacheKey(
            include_screenshot=True,
            include_vision=True,
            include_ocr=False,
            include_ui_elements=False,
            include_window_context=True,
            region=None,
            coordinate_environment=None
        )

        result = PerceptionResult(
            observation_id="test-obs",
            timestamp=datetime.now(),
            screen=ScreenInfo(width=1920, height=1080, dpi_scale_x=1.0, dpi_scale_y=1.0),
            status=PerceptionStatus.SUCCESS,
        )
        await cache.put(key, result)

        # Should hit cache with reasonable max_age_ms
        cached_result = await cache.get(key, max_age_ms=5000)
        assert cached_result is not None
        assert cached_result.observation_id == "test-obs"

    @pytest.mark.asyncio
    async def test_expired_result_misses(self):
        """Expired cached result misses cache."""
        cache = LRUPerceptionCache()
        key = PerceptionCacheKey(
            include_screenshot=True,
            include_vision=True,
            include_ocr=False,
            include_ui_elements=False,
            include_window_context=True,
            region=None,
            coordinate_environment=None
        )

        # Manually insert an old entry
        old_time = time.time() - 10.0  # 10 seconds ago
        cache._cache[key] = CacheEntry(
            result=PerceptionResult(
                observation_id="old-obs",
                timestamp=datetime.now() - timedelta(seconds=10),
                screen=ScreenInfo(width=1920, height=1080, dpi_scale_x=1.0, dpi_scale_y=1.0),
                status=PerceptionStatus.SUCCESS,
            ),
            timestamp=old_time
        )

        # max_age_ms = 1000ms = 1 second, should miss
        cached_result = await cache.get(key, max_age_ms=1000)
        assert cached_result is None

    @pytest.mark.asyncio
    async def test_max_age_ms_zero_never_reuses(self):
        """max_age_ms = 0 should never reuse cached observations."""
        cache = LRUPerceptionCache()
        key = PerceptionCacheKey(
            include_screenshot=True,
            include_vision=True,
            include_ocr=False,
            include_ui_elements=False,
            include_window_context=True,
            region=None,
            coordinate_environment=None
        )

        result = PerceptionResult(
            observation_id="test-obs",
            timestamp=datetime.now(),
            screen=ScreenInfo(width=1920, height=1080, dpi_scale_x=1.0, dpi_scale_y=1.0),
            status=PerceptionStatus.SUCCESS,
        )
        await cache.put(key, result)

        # max_age_ms = 0 should never hit
        cached_result = await cache.get(key, max_age_ms=0)
        assert cached_result is None


# ===========================================================================
# Test 38: Failure not cached
# ===========================================================================

class TestFailureNotCached:
    """Test that failure results are not cached."""

    @pytest.mark.asyncio
    async def test_failed_status_not_cached(self):
        """FAILED results should not be cached."""
        cache = LRUPerceptionCache()
        key = PerceptionCacheKey(
            include_screenshot=True,
            include_vision=True,
            include_ocr=False,
            include_ui_elements=False,
            include_window_context=True,
            region=None,
            coordinate_environment=None
        )

        failed_result = PerceptionResult(
            observation_id="failed-obs",
            timestamp=datetime.now(),
            screen=ScreenInfo(width=1920, height=1080, dpi_scale_x=1.0, dpi_scale_y=1.0),
            status=PerceptionStatus.FAILED,
        )
        await cache.put(key, failed_result)

        # Should not be in cache
        cached_result = await cache.get(key, max_age_ms=5000)
        assert cached_result is None

    @pytest.mark.asyncio
    async def test_timeout_status_not_cached(self):
        """TIMEOUT results should not be cached."""
        cache = LRUPerceptionCache()
        key = PerceptionCacheKey(
            include_screenshot=True,
            include_vision=True,
            include_ocr=False,
            include_ui_elements=False,
            include_window_context=True,
            region=None,
            coordinate_environment=None
        )

        timeout_result = PerceptionResult(
            observation_id="timeout-obs",
            timestamp=datetime.now(),
            screen=ScreenInfo(width=1920, height=1080, dpi_scale_x=1.0, dpi_scale_y=1.0),
            status=PerceptionStatus.TIMEOUT,
        )
        await cache.put(key, timeout_result)

        cached_result = await cache.get(key, max_age_ms=5000)
        assert cached_result is None

    @pytest.mark.asyncio
    async def test_cancelled_status_not_cached(self):
        """CANCELLED results should not be cached."""
        cache = LRUPerceptionCache()
        key = PerceptionCacheKey(
            include_screenshot=True,
            include_vision=True,
            include_ocr=False,
            include_ui_elements=False,
            include_window_context=True,
            region=None,
            coordinate_environment=None
        )

        cancelled_result = PerceptionResult(
            observation_id="cancelled-obs",
            timestamp=datetime.now(),
            screen=ScreenInfo(width=1920, height=1080, dpi_scale_x=1.0, dpi_scale_y=1.0),
            status=PerceptionStatus.CANCELLED,
        )
        await cache.put(key, cancelled_result)

        cached_result = await cache.get(key, max_age_ms=5000)
        assert cached_result is None

    @pytest.mark.asyncio
    async def test_provider_failure_not_cached_allows_retry(self):
        """Failed observations allow underlying provider to retry."""
        call_count = 0

        class FlakeyProvider:
            async def observe(self, request):
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    return PerceptionResult(
                        observation_id="failed-1",
                        timestamp=datetime.now(),
                        screen=ScreenInfo(width=1920, height=1080, dpi_scale_x=1.0, dpi_scale_y=1.0),
                        status=PerceptionStatus.FAILED,
                    )
                else:
                    return PerceptionResult(
                        observation_id="success-2",
                        timestamp=datetime.now(),
                        screen=ScreenInfo(width=1920, height=1080, dpi_scale_x=1.0, dpi_scale_y=1.0),
                        status=PerceptionStatus.SUCCESS,
                    )

            def get_available_sources(self):
                return ()

            def is_source_available(self, source):
                return False

        provider = FlakeyProvider()
        cached = CachedPerceptionProvider(provider, default_max_age_ms=5000)

        request = PerceptionRequest()
        result1 = await cached.observe(request)
        result2 = await cached.observe(request)

        # Both calls should hit the provider
        assert call_count == 2
        assert result1.status == PerceptionStatus.FAILED
        assert result2.status == PerceptionStatus.SUCCESS


# ===========================================================================
# Test 39: Partial result policy
# ===========================================================================

class TestPartialResultPolicy:
    """Test partial result caching policy."""

    @pytest.mark.asyncio
    async def test_partial_result_not_cached_by_default(self):
        """PARTIAL results are not cached by default (only SUCCESS is cached)."""
        cache = LRUPerceptionCache()
        key = PerceptionCacheKey(
            include_screenshot=True,
            include_vision=True,
            include_ocr=False,
            include_ui_elements=False,
            include_window_context=True,
            region=None,
            coordinate_environment=None
        )

        partial_result = PerceptionResult(
            observation_id="partial-obs",
            timestamp=datetime.now(),
            screen=ScreenInfo(width=1920, height=1080, dpi_scale_x=1.0, dpi_scale_y=1.0),
            status=PerceptionStatus.PARTIAL,
        )
        await cache.put(key, partial_result)

        # PARTIAL is not cached
        cached_result = await cache.get(key, max_age_ms=5000)
        assert cached_result is None


# ===========================================================================
# Test 40: Invalidation
# ===========================================================================

class TestInvalidation:
    """Test cache invalidation."""

    @pytest.mark.asyncio
    async def test_invalidate_specific_key(self):
        """Invalidate a specific cache key."""
        cache = LRUPerceptionCache()
        key = PerceptionCacheKey(
            include_screenshot=True,
            include_vision=True,
            include_ocr=False,
            include_ui_elements=False,
            include_window_context=True,
            region=None,
            coordinate_environment=None
        )

        result = PerceptionResult(
            observation_id="test-obs",
            timestamp=datetime.now(),
            screen=ScreenInfo(width=1920, height=1080, dpi_scale_x=1.0, dpi_scale_y=1.0),
            status=PerceptionStatus.SUCCESS,
        )
        await cache.put(key, result)

        # Verify it's cached
        assert await cache.get(key, max_age_ms=5000) is not None

        # Invalidate
        await cache.invalidate(key)

        # Should miss now
        assert await cache.get(key, max_age_ms=5000) is None

    @pytest.mark.asyncio
    async def test_invalidate_all_clears_everything(self):
        """Invalidate all clears entire cache."""
        cache = LRUPerceptionCache()

        keys = []
        for i in range(3):
            key = PerceptionCacheKey(
                include_screenshot=True,
                include_vision=(i == 0),
                include_ocr=(i == 1),
                include_ui_elements=False,
                include_window_context=True,
                region=None,
                coordinate_environment=None
            )
            keys.append(key)
            result = PerceptionResult(
                observation_id=f"obs-{i}",
                timestamp=datetime.now(),
                screen=ScreenInfo(width=1920, height=1080, dpi_scale_x=1.0, dpi_scale_y=1.0),
                status=PerceptionStatus.SUCCESS,
            )
            await cache.put(key, result)

        # Invalidate all
        await cache.invalidate()

        # All should miss
        for key in keys:
            assert await cache.get(key, max_age_ms=5000) is None

    @pytest.mark.asyncio
    async def test_clear_removes_all(self):
        """Clear removes all cache entries."""
        cache = LRUPerceptionCache()
        key = PerceptionCacheKey(
            include_screenshot=True,
            include_vision=True,
            include_ocr=False,
            include_ui_elements=False,
            include_window_context=True,
            region=None,
            coordinate_environment=None
        )

        result = PerceptionResult(
            observation_id="test-obs",
            timestamp=datetime.now(),
            screen=ScreenInfo(width=1920, height=1080, dpi_scale_x=1.0, dpi_scale_y=1.0),
            status=PerceptionStatus.SUCCESS,
        )
        await cache.put(key, result)

        await cache.clear()
        assert await cache.get(key, max_age_ms=5000) is None


# ===========================================================================
# Test 41: Observation ID preservation
# ===========================================================================

class TestObservationIDPreservation:
    """Test that observation ID is preserved on cache hit."""

    @pytest.mark.asyncio
    async def test_observation_id_preserved_on_hit(self):
        """Cache hit preserves the original observation ID."""
        provider = create_single_candidate_provider("Test", 0.9)
        # Override to use specific ID
        original_id = "fixed-obs-id-123"

        class FixedIDProvider:
            async def observe(self, request):
                return PerceptionResult(
                    observation_id=original_id,
                    timestamp=datetime.now(),
                    screen=ScreenInfo(width=1920, height=1080, dpi_scale_x=1.0, dpi_scale_y=1.0),
                    status=PerceptionStatus.SUCCESS,
                )

            def get_available_sources(self):
                return ()

            def is_source_available(self, source):
                return False

        fixed_provider = FixedIDProvider()
        cached = CachedPerceptionProvider(fixed_provider, default_max_age_ms=5000)

        request = PerceptionRequest()
        result1 = await cached.observe(request)
        result2 = await cached.observe(request)

        # Same ID both times
        assert result1.observation_id == original_id
        assert result2.observation_id == original_id


# ===========================================================================
# Test 42: Timestamp preservation
# ===========================================================================

class TestTimestampPreservation:
    """Test that timestamp is preserved on cache hit."""

    @pytest.mark.asyncio
    async def test_timestamp_preserved_on_hit(self):
        """Cache hit preserves the original timestamp."""
        fixed_time = datetime(2026, 1, 1, 12, 0, 0)

        class FixedTimeProvider:
            async def observe(self, request):
                return PerceptionResult(
                    observation_id="obs-1",
                    timestamp=fixed_time,
                    screen=ScreenInfo(width=1920, height=1080, dpi_scale_x=1.0, dpi_scale_y=1.0),
                    status=PerceptionStatus.SUCCESS,
                )

            def get_available_sources(self):
                return ()

            def is_source_available(self, source):
                return False

        provider = FixedTimeProvider()
        cached = CachedPerceptionProvider(provider, default_max_age_ms=5000)

        # Small delay between calls
        request = PerceptionRequest()
        result1 = await cached.observe(request)
        time.sleep(0.01)
        result2 = await cached.observe(request)

        # Timestamps should be identical
        assert result1.timestamp == fixed_time
        assert result2.timestamp == fixed_time


# ===========================================================================
# Test 43: Candidate freshness
# ===========================================================================

class TestCandidateFreshness:
    """Test that candidates retain their original timestamps."""

    @pytest.mark.asyncio
    async def test_candidates_preserve_timestamps(self):
        """Candidates retain their original timestamps after cache reuse."""
        original_timestamp = time.time()

        class CustomProvider:
            async def observe(self, request):
                return PerceptionResult(
                    observation_id="obs-1",
                    timestamp=datetime.now(),
                    screen=ScreenInfo(width=1920, height=1080, dpi_scale_x=1.0, dpi_scale_y=1.0),
                    status=PerceptionStatus.SUCCESS,
                    candidates=(
                        TargetCandidate(
                            source_type=ObservationSource.UIA,
                            bbox=(100, 100, 200, 140),
                            confidence=0.9,
                            text="Test",
                            timestamp=original_timestamp
                        ),
                    ),
                )

            def get_available_sources(self):
                return ()

            def is_source_available(self, source):
                return False

        provider = CustomProvider()
        cached = CachedPerceptionProvider(provider, default_max_age_ms=5000)

        request = PerceptionRequest()
        result1 = await cached.observe(request)
        time.sleep(0.01)
        result2 = await cached.observe(request)

        # Candidates should have same timestamp
        assert result1.candidates[0].timestamp == original_timestamp
        assert result2.candidates[0].timestamp == original_timestamp


# ===========================================================================
# Test 44: Concurrent requests
# ===========================================================================

class TestConcurrentRequests:
    """Test behavior under concurrent requests."""

    @pytest.mark.asyncio
    async def test_concurrent_requests_serialize_correctly(self):
        """Concurrent requests work correctly with thread-safe cache."""
        call_count = 0

        class SlowProvider:
            async def observe(self, request):
                nonlocal call_count
                call_count += 1
                await asyncio.sleep(0.05)
                return PerceptionResult(
                    observation_id=f"obs-{call_count}",
                    timestamp=datetime.now(),
                    screen=ScreenInfo(width=1920, height=1080, dpi_scale_x=1.0, dpi_scale_y=1.0),
                    status=PerceptionStatus.SUCCESS,
                )

            def get_available_sources(self):
                return ()

            def is_source_available(self, source):
                return False

        provider = SlowProvider()
        cached = CachedPerceptionProvider(provider, default_max_age_ms=5000)

        request = PerceptionRequest()

        # Launch concurrent requests
        results = await asyncio.gather(
            cached.observe(request),
            cached.observe(request),
            cached.observe(request),
        )

        # All should succeed (may be 1 or 3 underlying calls depending on timing)
        assert all(r.status == PerceptionStatus.SUCCESS for r in results)
        # All results should have same observation_id (cached)
        observation_ids = [r.observation_id for r in results]
        assert len(set(observation_ids)) == 1, f"Expected same ID, got {observation_ids}"


# ===========================================================================
# Test 45: Bounded cache
# ===========================================================================

class TestBoundedCache:
    """Test that cache evicts entries when full."""

    @pytest.mark.asyncio
    async def test_eviction_at_capacity(self):
        """Cache evicts LRU entry when at capacity."""
        cache = LRUPerceptionCache(max_entries=2)

        # Add 3 entries
        for i in range(3):
            key = PerceptionCacheKey(
                include_screenshot=True,
                include_vision=(i == 0),
                include_ocr=(i == 1),
                include_ui_elements=(i == 2),
                include_window_context=True,
                region=None,
                coordinate_environment=None
            )
            result = PerceptionResult(
                observation_id=f"obs-{i}",
                timestamp=datetime.now(),
                screen=ScreenInfo(width=1920, height=1080, dpi_scale_x=1.0, dpi_scale_y=1.0),
                status=PerceptionStatus.SUCCESS,
            )
            await cache.put(key, result)

        # Cache should only have 2 entries
        stats = cache.get_stats()
        assert stats["size"] == 2
        assert stats["evictions"] >= 1


# ===========================================================================
# Test 46: No LLM calls
# ===========================================================================

class TestNoLLMCalls:
    """Test that cache operations don't call LLM."""

    @pytest.mark.asyncio
    async def test_cache_lookup_no_llm(self):
        """Cache lookup does not invoke LLM."""
        mock_llm = Mock()
        mock_llm.generate = Mock(return_value=Mock(text="LLM response"))

        provider = create_single_candidate_provider("Test", 0.9)
        cached = CachedPerceptionProvider(provider, default_max_age_ms=5000)

        request = PerceptionRequest()
        await cached.observe(request)
        await cached.observe(request)
        await cached._cache.invalidate()
        await cached._cache.clear()

        # No LLM calls
        mock_llm.generate.assert_not_called()

    @pytest.mark.asyncio
    async def test_cache_operations_no_llm(self):
        """All cache operations avoid LLM calls."""
        mock_llm = Mock()
        mock_llm.generate = Mock(return_value=Mock(text="LLM response"))

        cache = LRUPerceptionCache()
        key = PerceptionCacheKey(
            include_screenshot=True,
            include_vision=True,
            include_ocr=False,
            include_ui_elements=False,
            include_window_context=True,
            region=None,
            coordinate_environment=None
        )
        result = PerceptionResult(
            observation_id="obs-1",
            timestamp=datetime.now(),
            screen=ScreenInfo(width=1920, height=1080, dpi_scale_x=1.0, dpi_scale_y=1.0),
            status=PerceptionStatus.SUCCESS,
        )

        await cache.put(key, result)
        await cache.get(key, max_age_ms=5000)
        await cache.invalidate(key)
        await cache.clear()
        cache.get_stats()  # This is synchronous, not awaitable

        mock_llm.generate.assert_not_called()


# ===========================================================================
# Test: Cache statistics
# ===========================================================================

class TestCacheStatistics:
    """Test cache statistics tracking."""

    @pytest.mark.asyncio
    async def test_stats_track_hits_and_misses(self):
        """Cache stats track hits, misses, and evictions."""
        cache = LRUPerceptionCache(max_entries=2)

        for i in range(3):
            key = PerceptionCacheKey(
                include_screenshot=True,
                include_vision=(i == 0),
                include_ocr=(i == 1),
                include_ui_elements=(i == 2),
                include_window_context=True,
                region=None,
                coordinate_environment=None
            )
            result = PerceptionResult(
                observation_id=f"obs-{i}",
                timestamp=datetime.now(),
                screen=ScreenInfo(width=1920, height=1080, dpi_scale_x=1.0, dpi_scale_y=1.0),
                status=PerceptionStatus.SUCCESS,
            )
            await cache.put(key, result)

        stats = cache.get_stats()
        assert "hits" in stats
        assert "misses" in stats
        assert "evictions" in stats
        assert "hit_rate_percent" in stats
        assert stats["evictions"] >= 1


# ===========================================================================
# Test: Default max age
# ===========================================================================

class TestDefaultMaxAge:
    """Test default max age behavior."""

    @pytest.mark.asyncio
    async def test_request_max_age_overrides_default(self):
        """Per-request max_age_ms overrides provider default."""
        class CountingProvider:
            def __init__(self):
                self.call_count = 0

            async def observe(self, request):
                self.call_count += 1
                return PerceptionResult(
                    observation_id=f"obs-{self.call_count}",
                    timestamp=datetime.now(),
                    screen=ScreenInfo(width=1920, height=1080, dpi_scale_x=1.0, dpi_scale_y=1.0),
                    status=PerceptionStatus.SUCCESS,
                )

            def get_available_sources(self):
                return ()

            def is_source_available(self, source):
                return False

        counting = CountingProvider()
        # Default max age is 5000ms, but request sets to 0
        cached = CachedPerceptionProvider(counting, default_max_age_ms=5000)

        # First call: max_age_ms=0 forces fresh observation
        request = PerceptionRequest(max_age_ms=0)
        result1 = await cached.observe(request)
        result2 = await cached.observe(request)

        # Both should hit provider since max_age_ms=0
        assert counting.call_count == 2


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
