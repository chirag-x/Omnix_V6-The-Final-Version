"""
Omnix V6 — Perception Observation Cache for Stage 18.9.

This module implements a generic, deterministic perception observation cache
that sits ABOVE the concrete perception implementation to avoid unnecessary
repeated perception work while preserving strict freshness and correctness.

The cache stores PerceptionResult instances and provides deterministic cache
keys based on observation requests, with TTL-based freshness checking.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple, Protocol, runtime_checkable
from collections import OrderedDict
import logging

from vision.perception_contract import (
    PerceptionProvider,
    PerceptionRequest,
    PerceptionResult,
    PerceptionSource,
    PerceptionStatus,
    ScreenInfo,
    WindowContext,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PerceptionCacheKey:
    """
    Deterministic cache key for perception observations.

    The key incorporates all request parameters that affect what observation
    is meaningful, ensuring cache correctness.
    """
    include_screenshot: bool
    include_vision: bool
    include_ocr: bool
    include_ui_elements: bool
    include_window_context: bool
    region: Optional[Tuple[int, int, int, int]]  # (x, y, width, height)
    coordinate_environment: Optional[Dict[str, Any]]  # ScreenInfo as dict

    def __hash__(self) -> int:
        """Make the key hashable for use in dictionaries."""
        return hash((
            self.include_screenshot,
            self.include_vision,
            self.include_ocr,
            self.include_ui_elements,
            self.include_window_context,
            self.region,
            tuple(sorted(self.coordinate_environment.items())) if self.coordinate_environment else None
        ))

    def __eq__(self, other: object) -> bool:
        """Check equality based on all fields."""
        if not isinstance(other, PerceptionCacheKey):
            return False
        return (
            self.include_screenshot == other.include_screenshot and
            self.include_vision == other.include_vision and
            self.include_ocr == other.include_ocr and
            self.include_ui_elements == other.include_ui_elements and
            self.include_window_context == other.include_window_context and
            self.region == other.region and
            self.coordinate_environment == other.coordinate_environment
        )


@dataclass
class CacheEntry:
    """Single cache entry containing a perception result and metadata."""
    result: PerceptionResult
    timestamp: float  # Unix timestamp when observation was made
    access_count: int = 0
    last_accessed: float = field(default_factory=time.time)

    def is_fresh(self, max_age_ms: Optional[int]) -> bool:
        """
        Check if the cache entry is still fresh based on max_age_ms.

        Args:
            max_age_ms: Maximum age in milliseconds, or None to use default

        Returns:
            True if entry is fresh, False otherwise
        """
        if max_age_ms is None:
            # Use project default policy - 5 seconds for now
            max_age_ms = 5000

        if max_age_ms == 0:
            # Never reuse cached observations
            return False

        age_ms = (time.time() - self.timestamp) * 1000
        return age_ms <= max_age_ms


@runtime_checkable
class PerceptionCache(Protocol):
    """Protocol defining the perception cache interface."""

    def get(
        self,
        key: PerceptionCacheKey,
        *,
        max_age_ms: Optional[int] = None,
    ) -> PerceptionResult | None:
        """Get a perception result from cache if fresh and available."""
        ...

    def put(
        self,
        key: PerceptionCacheKey,
        result: PerceptionResult,
    ) -> None:
        """Store a perception result in cache."""
        ...

    def invalidate(
        self,
        key: PerceptionCacheKey | None = None,
    ) -> None:
        """Invalidate cache entry by key, or all entries if key is None."""
        ...

    def clear(self) -> None:
        """Clear all cache entries."""
        ...

    def get_stats(self) -> Dict[str, Any]:
        """Get cache statistics for monitoring."""
        ...


class LRUPerceptionCache:
    """
    LRU (Least Recently Used) perception cache implementation.

    Provides deterministic eviction when cache exceeds maximum size.
    """

    def __init__(self, max_entries: int = 100):
        """
        Initialize the LRU cache.

        Args:
            max_entries: Maximum number of cache entries before eviction
        """
        self._max_entries = max_entries
        self._cache: OrderedDict[PerceptionCacheKey, CacheEntry] = OrderedDict()
        self._lock = asyncio.Lock()
        self._stats = {
            "hits": 0,
            "misses": 0,
            "evictions": 0,
            "invalidations": 0,
            "stale_rejections": 0
        }

    async def get(
        self,
        key: PerceptionCacheKey,
        *,
        max_age_ms: Optional[int] = None,
    ) -> PerceptionResult | None:
        """
        Get a perception result from cache if fresh and available.

        Args:
            key: Cache key to lookup
            max_age_ms: Maximum age in milliseconds for freshness

        Returns:
            Cached PerceptionResult if fresh, None otherwise
        """
        async with self._lock:
            if key not in self._cache:
                self._stats["misses"] += 1
                logger.debug(f"Cache miss for key: {key}")
                return None

            entry = self._cache[key]

            # Check freshness
            if not entry.is_fresh(max_age_ms):
                logger.debug(f"Cache entry stale for key: {key} (age: {(time.time() - entry.timestamp)*1000:.2f}ms)")
                self._stats["stale_rejections"] += 1
                # Remove stale entry
                del self._cache[key]
                self._stats["misses"] += 1  # Count as miss since we're rejecting it
                return None

            # Move to end (most recently used)
            self._cache.move_to_end(key)
            entry.access_count += 1
            entry.last_accessed = time.time()

            self._stats["hits"] += 1
            logger.debug(f"Cache hit for key: {key}")
            return entry.result

    async def put(
        self,
        key: PerceptionCacheKey,
        result: PerceptionResult,
    ) -> None:
        """
        Store a perception result in cache.

        Args:
            key: Cache key for the result
            result: PerceptionResult to cache
        """
        async with self._lock:
            # Only cache successful results
            if result.status != PerceptionStatus.SUCCESS:
                logger.debug(f"Not caching result with status: {result.status}")
                return

            # Check if we need to evict
            if key in self._cache:
                # Update existing entry
                self._cache.move_to_end(key)
                self._cache[key] = CacheEntry(
                    result=result,
                    timestamp=time.time(),
                    access_count=self._cache[key].access_count + 1,
                    last_accessed=time.time()
                )
            else:
                # Add new entry
                if len(self._cache) >= self._max_entries:
                    # Evict least recently used
                    evicted_key, _ = self._cache.popitem(last=False)
                    self._stats["evictions"] += 1
                    logger.debug(f"Evicted cache entry for key: {evicted_key}")

                self._cache[key] = CacheEntry(
                    result=result,
                    timestamp=time.time()
                )

            logger.debug(f"Cached perception result for key: {key}")

    async def invalidate(
        self,
        key: PerceptionCacheKey | None = None,
    ) -> None:
        """
        Invalidate cache entry by key, or all entries if key is None.

        Args:
            key: Specific key to invalidate, or None to invalidate all
        """
        async with self._lock:
            if key is None:
                # Invalidate all
                cleared_count = len(self._cache)
                self._cache.clear()
                self._stats["invalidations"] += cleared_count
                logger.debug(f"Cleared all {cleared_count} cache entries")
            else:
                # Invalidate specific key
                if key in self._cache:
                    del self._cache[key]
                    self._stats["invalidations"] += 1
                    logger.debug(f"Invalidated cache entry for key: {key}")
                else:
                    logger.debug(f"Attempted to invalidate non-existent key: {key}")

    async def clear(self) -> None:
        """Clear all cache entries."""
        async with self._lock:
            cleared_count = len(self._cache)
            self._cache.clear()
            self._stats["invalidations"] += cleared_count
            logger.debug(f"Cleared all {cleared_count} cache entries")

    def get_stats(self) -> Dict[str, Any]:
        """
        Get cache statistics for monitoring.

        Returns:
            Dictionary containing cache statistics
        """
        total_requests = self._stats["hits"] + self._stats["misses"]
        hit_rate = (self._stats["hits"] / total_requests * 100) if total_requests > 0 else 0

        return {
            **self._stats,
            "size": len(self._cache),
            "max_entries": self._max_entries,
            "hit_rate_percent": round(hit_rate, 2),
            "total_requests": total_requests
        }


class CachedPerceptionProvider:
    """
    Wrapper that adds caching capabilities to any PerceptionProvider.

    This provider implements the PerceptionProvider interface and delegates
    to an underlying provider while adding caching capabilities.
    """

    def __init__(
        self,
        underlying_provider: PerceptionProvider,
        cache: PerceptionCache | None = None,
        default_max_age_ms: int = 5000
    ):
        """
        Initialize the cached perception provider.

        Args:
            underlying_provider: The actual perception provider to wrap
            cache: Cache implementation to use (creates LRU cache if None)
            default_max_age_ms: Default maximum age for cache entries in milliseconds
        """
        self._underlying = underlying_provider
        self._cache = cache or LRUPerceptionCache()
        self._default_max_age_ms = default_max_age_ms
        logger.info(f"Initialized CachedPerceptionProvider wrapping {type(underlying_provider).__name__}")

    async def observe(self, request: PerceptionRequest) -> PerceptionResult:
        """
        Observe with caching - return cached result if fresh, otherwise get new observation.

        Args:
            request: Perception request specifying what to observe

        Returns:
            PerceptionResult from cache or fresh observation
        """
        # Convert request to cache key
        cache_key = self._request_to_key(request)

        # Try to get from cache first
        cached_result = await self._cache.get(
            cache_key,
            max_age_ms=request.max_age_ms
        )

        if cached_result is not None:
            logger.debug(f"Returning cached perception result for request: {request}")
            return cached_result

        # Cache miss - get fresh observation
        logger.debug(f"Cache miss for request: {request}, getting fresh observation")
        fresh_result = await self._underlying.observe(request)

        # Only cache successful results
        if fresh_result.status == PerceptionStatus.SUCCESS:
            await self._cache.put(cache_key, fresh_result)
            logger.debug(f"Cached fresh perception result for request: {request}")
        else:
            logger.debug(f"Not caching unsuccessful result with status: {fresh_result.status}")

        return fresh_result

    def _request_to_key(self, request: PerceptionRequest) -> PerceptionCacheKey:
        """
        Convert a PerceptionRequest to a PerceptionCacheKey.

        Args:
            request: The perception request to convert

        Returns:
            PerceptionCacheKey representing the request
        """
        # Extract coordinate environment from request or use defaults
        coordinate_env = None
        if hasattr(request, 'coordinate_environment') and request.coordinate_environment:
            coordinate_env = request.coordinate_environment
        elif hasattr(request, 'screen_info') and request.screen_info:
            # Convert ScreenInfo to dict for hashing
            screen_info = request.screen_info
            coordinate_env = {
                "width": screen_info.width,
                "height": screen_info.height,
                "scale_factor": getattr(screen_info, 'scale_factor', 1.0),
                "rotation": getattr(screen_info, 'rotation', 0)
            }

        return PerceptionCacheKey(
            include_screenshot=request.include_screenshot,
            include_vision=request.include_vision,
            include_ocr=request.include_ocr,
            include_ui_elements=request.include_ui_elements,
            include_window_context=request.include_window_context,
            region=getattr(request, 'region', None),
            coordinate_environment=coordinate_env
        )

    # Delegate other PerceptionProvider methods if needed
    # For now, we only need to implement observe() as that's what the contract uses

    async def get_available_sources(self) -> Tuple[PerceptionSource, ...]:
        """Delegate to underlying provider."""
        if hasattr(self._underlying, 'get_available_sources'):
            return await self._underlying.get_available_sources()
        # Fallback - return common sources
        return (
            PerceptionSource.SCREENSHOT,
            PerceptionSource.VISION,
            PerceptionSource.OCR,
            PerceptionSource.UI_AUTOMATION,
            PerceptionSource.WINDOW_MANAGER,
            PerceptionSource.COORDINATE
        )


# Factory functions for easy creation
def create_cached_perception_provider(
    underlying_provider: PerceptionProvider,
    max_cache_entries: int = 100,
    default_max_age_ms: int = 5000
) -> CachedPerceptionProvider:
    """
    Factory function to create a cached perception provider.

    Args:
        underlying_provider: The perception provider to wrap
        max_cache_entries: Maximum number of cache entries
        default_max_age_ms: Default cache TTL in milliseconds

    Returns:
        CachedPerceptionProvider instance
    """
    cache = LRUPerceptionCache(max_entries=max_cache_entries)
    return CachedPerceptionProvider(
        underlying_provider=underlying_provider,
        cache=cache,
        default_max_age_ms=default_max_age_ms
    )


# Global cache instance for shared usage (optional)
_global_cache: LRUPerceptionCache | None = None


def get_global_perception_cache() -> LRUPerceptionCache:
    """Get or create the global perception cache instance."""
    global _global_cache
    if _global_cache is None:
        _global_cache = LRUPerceptionCache()
    return _global_cache


def create_global_cached_provider(
    underlying_provider: PerceptionProvider,
    default_max_age_ms: int = 5000
) -> CachedPerceptionProvider:
    """
    Create a cached provider using the global cache instance.

    Args:
        underlying_provider: The perception provider to wrap
        default_max_age_ms: Default cache TTL in milliseconds

    Returns:
        CachedPerceptionProvider using global cache
    """
    return CachedPerceptionProvider(
        underlying_provider=underlying_provider,
        cache=get_global_perception_cache(),
        default_max_age_ms=default_max_age_ms
    )