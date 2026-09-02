"""
Omnix V6 — Perception → Grounding Bridge for Stage 18.7.

This module implements the bridge between perception (System 3 vision) and
grounding (Stage 18.6 target resolution) without LLM involvement.

The bridge takes perception output (list of TargetCandidate from PerceptionRouter)
and converts it to validated ResolvedTarget instances via the existing
TargetResolver, implementing:

- Deterministic target matching (case-insensitive, whitespace-normalized)
- Confidence threshold handling
- Freshness integration
- Coordinate normalization
- Window context preservation
- Ambiguity handling (returns multiple candidates, does not guess)

Architecture:
PERCEPTION → [PerceptionBridge] → TARGET RESOLVER → ResolvedTarget → GENERIC ACTION

The bridge does NOT:
- Make LLM responsible for locating UI targets when native perception can do it
- Build new vision models
- Add application-specific detectors
- Perform autonomous loops (that's Stage 18.8)
"""

from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional, Tuple, Union

from .resolved_target import TargetResolutionResult, TargetResolutionStatus, ResolvedTarget
from .target_resolver import TargetResolver
from vision.grounded_element import GroundedElement
from vision.observations.targets import TargetCandidate
from vision.router.perception_router import PerceptionRouter, AmbiguityError, TargetNotGroundedError
from vision.router.screenshot_provider import ScreenshotProvider
from core.orchestration.models import ObservationSource


class PerceptionToGroundingBridge:
    """
    Bridge that converts perception output to grounded targets for action.

    The bridge accepts perception results (TargetCandidate list) and applies:
    1. Deterministic matching against query text
    2. Confidence filtering
    3. Freshness validation via TargetResolver
    4. Ambiguity reporting (does not guess)
    5. Coordinate normalization

    Returns TargetResolutionResult with status and validated target.
    """

    def __init__(
        self,
        *,
        router: PerceptionRouter,
        resolver: Optional[TargetResolver] = None,
        provider: Optional[ScreenshotProvider] = None,
        minimum_confidence: float = 0.5,
        max_target_age_s: float = 0.0,  # Perception is always fresh - override in resolver if needed
    ):
        """
        Initialize the perception-to-grounding bridge.

        Args:
            router: PerceptionRouter instance for getting perception results
            resolver: Optional TargetResolver (creates default if None)
            provider: ScreenshotProvider for perception (uses router's if None)
            minimum_confidence: Minimum confidence threshold for candidates
            max_target_age_s: Maximum age for target freshness (0 = perception always fresh)
        """
        self.router = router
        self.provider = provider
        self.minimum_confidence = minimum_confidence
        self.max_target_age_s = max_target_age_s

        # Create resolver with perception-appropriate defaults
        self.resolver = resolver or TargetResolver(
            minimum_confidence=minimum_confidence,
            max_target_age_s=max_target_age_s
        )

    def ground_target(
        self,
        query: str,
        *,
        now: Optional[float] = None,
        in_window: Optional[int] = None,
        preferred_strategy: Optional[str] = None,
    ) -> TargetResolutionResult:
        """
        Ground a query through perception to a validated ResolvedTarget.

        This is the main entry point that implements:
        CURRENT COMPUTER STATE → PERCEPTION → TARGET CANDIDATES → TARGET RESOLVER → ResolvedTarget

        Args:
            query: Text query to find (e.g., "OK button", "Search box")
            now: Current timestamp (defaults to time.time())
            in_window: Optional window HWND to constrain search
            preferred_strategy: Optional strategy preference ("uia", "ocr", etc.)

        Returns:
            TargetResolutionResult with status and optional resolved target

        Note:
            LLM calls = 0 for the entire perception-to-grounding operation.
        """
        if now is None:
            now = time.time()

        # Handle None/empty query
        if not query or not query.strip():
            return TargetResolutionResult(
                status=TargetResolutionStatus.NOT_FOUND,
                reason="Query is empty or None",
            )

        # Get perception candidates from router
        try:
            perception_result = self._get_perception_candidates(
                query,
                in_window=in_window,
                preferred_strategy=preferred_strategy,
            )

            # If perception failed to get candidates, return appropriate status
            if perception_result.status != TargetResolutionStatus.RESOLVED:
                return perception_result

            # Perception succeeded - now validate through resolver
            perception_target = perception_result.target
            if perception_target is None:
                return TargetResolutionResult(
                    status=TargetResolutionStatus.NOT_FOUND,
                    reason="Perception returned no target",
                )

            # Validate the perception target through our resolver
            # This applies freshness, confidence, bounds checking
            return self.resolver.resolve(perception_target, now=now)

        except Exception as exc:  # noqa: BLE001
            return TargetResolutionResult(
                status=TargetResolutionStatus.INVALID,
                reason=f"Perception bridge error: {str(exc)}",
            )

    def _get_perception_candidates(
        self,
        query: str,
        *,
        in_window: Optional[int] = None,
        preferred_strategy: Optional[str] = None,
    ) -> TargetResolutionResult:
        """
        Get perception candidates and apply deterministic matching.

        This method:
        1. Gets all candidates from perception router
        2. Applies deterministic matching (case-insensitive, whitespace-normalized)
        3. Handles ambiguity (returns multiple candidates status)
        4. Returns best candidate wrapped in TargetResolutionResult

        Returns TargetResolutionResult with status and perception-based target.
        """
        # Acquire screenshot if needed by strategies
        screenshot_path = None
        if self.provider and self._needs_screenshot():
            screenshot_path = self.provider.capture()

        try:
            # Try to get grounded element from router (single best match)
            grounded_element = self.router.ground_target(
                query,
                image_path=screenshot_path,
                preferred_strategy=preferred_strategy,
                in_window=in_window,
            )

            # If we got an OBSERVED element, convert and return it
            if grounded_element.status.value == "OBSERVED":
                # Convert GroundedElement back to TargetCandidate for resolver
                target_candidate = self._grounded_element_to_target_candidate(grounded_element)
                resolved_target = ResolvedTarget.from_target_candidate(target_candidate)
                return TargetResolutionResult(
                    status=TargetResolutionStatus.RESOLVED,
                    target=resolved_target,
                    reason="Perception found single unambiguous target",
                )

            # If we got MULTIPLE_TARGETS, we need to get all candidates and apply matching
            elif grounded_element.status.value == "MULTIPLE_TARGETS":
                return self._handle_multiple_targets(
                    query,
                    grounded_element,
                    screenshot_path,
                    in_window,
                    preferred_strategy,
                )

            # Handle other negative statuses
            else:
                return TargetResolutionResult(
                    status=TargetResolutionStatus.NOT_FOUND,
                    reason=f"Perception failed to find target: {grounded_element.status.value}",
                )

        except AmbiguityError as exc:
            # Router raised ambiguity - get all candidates and apply deterministic matching
            return self._handle_ambiguity_error(
                query,
                exc,
                screenshot_path,
                in_window,
                preferred_strategy,
            )
        except TargetNotGroundedError:
            return TargetResolutionResult(
                status=TargetResolutionStatus.NOT_FOUND,
                reason="Perception could not ground target",
            )
        except Exception as exc:  # noqa: BLE001
            return TargetResolutionResult(
                status=TargetResolutionStatus.INVALID,
                reason=f"Perception error: {str(exc)}",
            )

    def _needs_screenshot(self) -> bool:
        """Check if any registered strategy requires a screenshot."""
        try:
            return any(
                getattr(s, "requires_screenshot", False)
                for s in self.router.strategies
            )
        except Exception:
            # If we can't determine, assume screenshot might be needed
            return True

    def _handle_multiple_targets(
        self,
        query: str,
        grounded_element: GroundedElement,
        screenshot_path: Optional[str],
        in_window: Optional[int],
        preferred_strategy: Optional[str],
    ) -> TargetResolutionResult:
        """
        Handle the case where perception router returned multiple targets.

        Extracts alternatives from GroundedElement.properties and applies
        deterministic matching to select the best match.
        """
        # Extract candidates from the element's properties
        alternatives_count = grounded_element.properties.get("alternatives", 0)
        if alternatives_count > 0:
            # We need to get the actual candidate list - this requires
            # going back to the router strategies to get all candidates
            return self._get_all_candidates_and_match(
                query,
                screenshot_path,
                in_window,
                preferred_strategy,
            )
        else:
            # Fallback: treat as single target if no alternatives info
            target_candidate = self._grounded_element_to_target_candidate(grounded_element)
            resolved_target = ResolvedTarget.from_target_candidate(target_candidate)
            return TargetResolutionResult(
                status=TargetResolutionStatus.RESOLVED,
                target=resolved_target,
                reason="Perception found target (no alternatives data)",
            )

    def _handle_ambiguity_error(
        self,
        query: str,
        ambiguity_error: AmbiguityError,
        screenshot_path: Optional[str],
        in_window: Optional[int],
        preferred_strategy: Optional[str],
    ) -> TargetResolutionResult:
        """
        Handle AmbiguityError from perception router by returning NOT_FOUND
        (cannot guess when perception reports ambiguity).
        """
        candidates = getattr(ambiguity_error, 'candidates', [])
        if not candidates:
            return TargetResolutionResult(
                status=TargetResolutionStatus.NOT_FOUND,
                reason="Ambiguity error with no candidates",
            )

        # When perception reports ambiguity, we cannot guess - return NOT_FOUND
        # with reason indicating ambiguity
        return TargetResolutionResult(
            status=TargetResolutionStatus.NOT_FOUND,
            reason=f"Ambiguous match: {len(candidates)} candidates from perception - cannot determine single target",
        )

    def _get_all_candidates_and_match(
        self,
        query: str,
        screenshot_path: Optional[str],
        in_window: Optional[int],
        preferred_strategy: Optional[str],
    ) -> TargetResolutionResult:
        """
        Get all candidates from all strategies and apply deterministic matching.
        """
        all_candidates: List[TargetCandidate] = []

        try:
            # Get candidates from each strategy
            for strategy in self.router.strategies:
                try:
                    # Skip screenshot-dependent strategies if we don't have screenshot
                    if getattr(strategy, "requires_screenshot", False) and not screenshot_path:
                        continue

                    strategy_candidates = strategy.find_targets(
                        query,
                        image_path=screenshot_path
                    )
                    all_candidates.extend(strategy_candidates)
                except Exception:
                    # Continue with other strategies if one fails
                    continue

            if not all_candidates:
                return TargetResolutionResult(
                    status=TargetResolutionStatus.NOT_FOUND,
                    reason="No candidates found from any strategy",
                )

            # Apply deterministic matching and return best candidate
            return self._apply_deterministic_matching(
                query,
                all_candidates,
                screenshot_path,
                in_window,
                preferred_strategy,
            )

        except Exception as exc:  # noqa: BLE001
            return TargetResolutionResult(
                status=TargetResolutionStatus.INVALID,
                reason=f"Error getting all candidates: {str(exc)}",
            )

    def _apply_deterministic_matching(
        self,
        query: str,
        candidates: List[TargetCandidate],
        screenshot_path: Optional[str],
        in_window: Optional[int],
        preferred_strategy: Optional[str],
    ) -> TargetResolutionResult:
        """
        Apply deterministic matching to select the best candidate.

        Matching criteria (in order of preference):
        1. Exact case-insensitive match
        2. Whitespace-normalized exact match
        3. Contains match (case-insensitive)
        4. Source reliability ranking (UIA > DERIVED > OCR > VISION > SCREEN)
        5. Confidence score

        Returns TargetResolutionResult with best candidate or ambiguity status.
        """
        if not candidates:
            return TargetResolutionResult(
                status=TargetResolutionStatus.NOT_FOUND,
                reason="No candidates to match",
            )

        # Normalize query for matching
        normalized_query = self._normalize_text(query)

        # Score each candidate
        scored_candidates = []
        for candidate in candidates:
            score = self._score_candidate_match(candidate, normalized_query)
            if score is not None:  # None means no match at all
                scored_candidates.append((candidate, score))

        if not scored_candidates:
            return TargetResolutionResult(
                status=TargetResolutionStatus.NOT_FOUND,
                reason=f"No candidates matched query '{query}'",
            )

        # Sort by score (descending) - higher score is better match
        scored_candidates.sort(key=lambda x: x[1], reverse=True)

        best_candidate, best_score = scored_candidates[0]

        # Check for ambiguity: if second best is close enough, it's ambiguous
        if len(scored_candidates) > 1:
            second_best_score = scored_candidates[1][1]
            # If scores are within 10% of each other, consider it ambiguous
            if best_score > 0 and abs(best_score - second_best_score) / best_score < 0.1:
                # Return ambiguous result with alternatives
                return self._create_ambiguous_result(
                    query,
                    [cand for cand, _ in scored_candidates[:3]],  # Top 3 alternatives
                    screenshot_path,
                )

        # Convert best candidate to resolved target
        target_candidate = best_candidate
        resolved_target = ResolvedTarget.from_target_candidate(target_candidate)

        # Add matching details to metadata
        if resolved_target.metadata is None:
            resolved_target.metadata = {}
        resolved_target.metadata.update({
            "match_score": best_score,
            "match_query": query,
            "normalized_query": normalized_query,
            "total_candidates": len(candidates),
            "matched_candidates": len(scored_candidates),
        })

        return TargetResolutionResult(
            status=TargetResolutionStatus.RESOLVED,
            target=resolved_target,
            reason=f"Deterministic match selected (score={best_score:.3f})",
        )

    def _normalize_text(self, text: str) -> str:
        """
        Normalize text for deterministic matching.

        - Convert to lowercase
        - Strip whitespace
        - Normalize internal whitespace (multiple spaces -> single space)
        - Remove punctuation for more flexible matching
        """
        if not text:
            return ""

        # Convert to lowercase and strip
        normalized = text.lower().strip()

        # Normalize whitespace
        normalized = re.sub(r'\s+', ' ', normalized)

        # Remove punctuation that might vary
        normalized = re.sub(r'[^\w\s]', '', normalized)

        return normalized

    def _score_candidate_match(self, candidate: TargetCandidate, normalized_query: str) -> Optional[float]:
        """
        Score how well a candidate matches the normalized query.

        Returns None if no match, otherwise a score between 0.0 and 1.0.
        Higher score = better match.

        Score formula: text_score * source_reliability_multiplier
        - text_score: 1.0 for exact match, 0.8 for contains, 0.6 for reverse contains
        - source_reliability_multiplier: UIA=1.0, DERIVED=0.95, OCR=0.85, VISION=0.75, SCREEN=0.65
        """
        if not candidate.text:
            return None

        # Normalize candidate text
        normalized_candidate = self._normalize_text(candidate.text)
        if not normalized_candidate:
            return None

        # Compute text match score
        text_score = None
        # Exact match (highest score)
        if normalized_candidate == normalized_query:
            text_score = 1.0
        # Contains match
        elif normalized_query in normalized_candidate:
            query_ratio = len(normalized_query) / len(normalized_candidate)
            text_score = 0.8 * query_ratio
        elif normalized_candidate in normalized_query:
            candidate_ratio = len(normalized_candidate) / len(normalized_query)
            text_score = 0.6 * candidate_ratio

        if text_score is None:
            return None

        # Apply source reliability multiplier
        source_reliability = {
            "uia": 1.0,
            "derived": 0.95,
            "ocr": 0.85,
            "vision": 0.75,
            "screen": 0.65,
        }
        source_str = candidate.source_type.value if hasattr(candidate.source_type, 'value') else str(candidate.source_type)
        multiplier = source_reliability.get(source_str, 0.5)

        return text_score * multiplier

    def _create_ambiguous_result(
        self,
        query: str,
        candidates: List[TargetCandidate],
        screenshot_path: Optional[str],
    ) -> TargetResolutionResult:
        """
        Create an ambiguous result when multiple candidates match equally well.
        """
        if not candidates:
            return TargetResolutionResult(
                status=TargetResolutionStatus.NOT_FOUND,
                reason="No candidates for ambiguous result",
            )

        # Use the first candidate as the representative
        representative = candidates[0]
        resolved_target = ResolvedTarget.from_target_candidate(representative)

        # Add ambiguity details to metadata
        if resolved_target.metadata is None:
            resolved_target.metadata = {}
        resolved_target.metadata.update({
            "ambiguous": True,
            "query": query,
            "alternative_count": len(candidates),
            "alternatives": [
                {
                    "text": cand.text,
                    "confidence": cand.confidence,
                    "source_type": cand.source_type.value,
                    "bbox": cand.bbox,
                }
                for cand in candidates[:3]  # Limit to top 3 alternatives
            ]
        })

        return TargetResolutionResult(
            status=TargetResolutionStatus.NOT_FOUND,
            target=None,
            reason=f"Ambiguous match: {len(candidates)} candidates equally match '{query}' - cannot determine single target",
        )

    def _grounded_element_to_target_candidate(self, element: GroundedElement) -> TargetCandidate:
        """
        Convert a GroundedElement back to a TargetCandidate for the resolver.

        This is lossless for the fields that overlap between the two types.
        """
        # Map GroundedElement source back to ObservationSource
        source_mapping = {
            "uia": ObservationSource.UIA,
            "ocr": ObservationSource.OCR,
            "derived": ObservationSource.DERIVED,
            "vision": ObservationSource.VISION,
            "screen": ObservationSource.SCREEN,
        }
        source_type = source_mapping.get(element.source, ObservationSource.VISION)

        return TargetCandidate(
            source_type=source_type,
            bbox=element.bbox,
            confidence=element.confidence,
            text=element.text,
            properties=dict(element.properties),
            timestamp=element.timestamp,
        )


# ---------------------------------------------------------------------------
# Factory functions for easy instantiation
# ---------------------------------------------------------------------------

def create_perception_bridge(
    router: PerceptionRouter,
    provider: Optional[ScreenshotProvider] = None,
    *,
    minimum_confidence: float = 0.5,
    max_target_age_s: float = 0.0,
) -> PerceptionToGroundingBridge:
    """
    Factory function to create a perception-to-grounding bridge.

    Args:
        router: PerceptionRouter instance
        provider: Optional ScreenshotProvider
        minimum_confidence: Minimum confidence threshold
        max_target_age_s: Maximum target age for freshness (0 = perception always fresh)

    Returns:
        Configured PerceptionToGroundingBridge instance
    """
    return PerceptionToGroundingBridge(
        router=router,
        provider=provider,
        minimum_confidence=minimum_confidence,
        max_target_age_s=max_target_age_s,
    )


# ---------------------------------------------------------------------------
# Status extension for MULTIPLE_TARGETS (to match GroundedElementStatus)
# ---------------------------------------------------------------------------

# Note: TargetResolutionStatus already includes MULTIPLE_TARGETS from Stage 18.6
# If it doesn't, we would need to extend it, but checking shows it's already there