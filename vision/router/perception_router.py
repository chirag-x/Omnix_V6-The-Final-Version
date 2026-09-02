"""
Adaptive Perception Router for V6 Phase 7.

The router is the *only* code that decides which strategies to
invoke and how to break ambiguity between multiple candidates.

R-22 boundary
-------------
The router is **adaptive** but **deterministic** in this design:

  * The strategy *order* is chosen by a stable, documented rule
    (source reliability first, then text-driven hints).  It is
    NOT random.
  * The ambiguity-breaking rule is *exactly* one of:
        1. The single candidate (no choice to make).
        2. A clear winner on the deterministic ranking key
           (source_reliability, then -confidence, then
           top-left-most bbox).
        3. An :class:`AmbiguityError` (the router refuses to
           guess -- the agent must reformulate the query or
           ask the user).

There is **no** "first max wins on a tie".  A tie is a tie -- we
*raise* :class:`AmbiguityError` so the agent handles it.  This
is the "No nondeterminism" rule from the Phase 7.1 spec.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from core.orchestration.models import ObservationSource
from vision.observations.targets import TargetCandidate, GroundedTarget
from vision.router.perception_strategy import PerceptionStrategy


class TargetNotGroundedError(Exception):
    """Raised when no strategy produced any candidate."""

    def __init__(self, message: str, *, query: str) -> None:
        super().__init__(message)
        self.query = query


class AmbiguityError(Exception):
    """Raised when multiple candidates are indistinguishable.

    Phase 7.1 hardening: ties are NOT silently broken.  The
    router raises this so the Agent can ask the user to
    disambiguate, or reformulate the target query.

    The ``candidates`` field is the full list the router could
    not distinguish.
    """

    def __init__(
        self,
        message: str,
        *,
        candidates: List[TargetCandidate],
    ) -> None:
        super().__init__(message)
        self.candidates = list(candidates)


# A higher reliability value beats a lower one.
# Lower number in the list is the *tie-breaker* (so 0 wins if
# everything else is equal).  Currently we use:
#   UIA > Coordinates > OCR > YOLO
# UIA walks the live tree; coordinates are user-supplied; OCR
# reads pixels; YOLO is a coarse object detector.
_RELIABILITY_RANK: Dict[ObservationSource, int] = {
    ObservationSource.UIA: 0,
    ObservationSource.DERIVED: 1,
    ObservationSource.OCR: 2,
    ObservationSource.VISION: 3,
    ObservationSource.SCREEN: 4,
}


def _rank_key(c: TargetCandidate) -> Tuple[int, float, int, int, int, int]:
    """The single deterministic ranking key used for every candidate.

    Ordering (lower is better):
      1. Reliability rank (UIA < DERIVED < OCR < VISION).
      2. Negative confidence (higher confidence wins).
      3. Top-left-most bbox (left first, then top).
      4. Source enum value, then class id, to keep the
         ordering total even on identical bboxes.
    """
    rel = _RELIABILITY_RANK.get(c.source_type, 99)
    left, top, _right, _bottom = c.bbox
    src_value = c.source_type.value if hasattr(c.source_type, "value") else str(c.source_type)
    # NOTE: id(c) is intentionally NOT used in the key, so two
    # semantically identical candidates are treated as a tie and
    # surface as AmbiguityError instead of an arbitrary pick.
    return (rel, -c.confidence, left, top, src_value, 0)


class PerceptionRouter:
    """Adaptive but deterministic router for Phase 7 perception."""

    def __init__(self, strategies: List[PerceptionStrategy]) -> None:
        # Use a dict so we can also look up by name.
        self._strategies: Dict[str, PerceptionStrategy] = {
            s.name: s for s in strategies
        }

    # ------------------------------------------------------------------ API
    @property
    def strategies(self) -> List[PerceptionStrategy]:
        return list(self._strategies.values())

    def add_strategy(self, strategy: PerceptionStrategy) -> None:
        self._strategies[strategy.name] = strategy

    def ground_target(
        self,
        target_query: str,
        image_path: Optional[str] = None,
        preferred_strategy: Optional[str] = None,
    ) -> GroundedTarget:
        """Ground ``target_query`` into a single :class:`GroundedTarget`.

        :param target_query: User-facing target description.
        :param image_path: Optional screenshot; the router will
            lazily acquire one via the :class:`ScreenshotProvider`
            *only* if the chosen strategy requires it.
        :param preferred_strategy: If set and known, the router
            will try that strategy first.

        :raises TargetNotGroundedError: when no candidate was
            produced by any strategy.
        :raises AmbiguityError: when multiple candidates are
            indistinguishable on the deterministic ranking key.
        """
        order = self.strategy_order(target_query, preferred_strategy)
        all_candidates: List[Tuple[str, TargetCandidate]] = []

        for s_name in order:
            strategy = self._strategies.get(s_name)
            if strategy is None:
                continue

            try:
                # If this strategy requires a screenshot but the
                # caller did not provide one, the caller is
                # responsible for wiring a ScreenshotProvider in
                # VisionService.  We never reach into the
                # capability set from the router.
                if strategy.requires_screenshot and not image_path:
                    continue
                found = strategy.find_targets(
                    target_query, image_path=image_path
                )
            except Exception:
                # A strategy that throws must NOT poison the
                # routing.  Skip it.
                continue

            for c in found:
                all_candidates.append((s_name, c))

            # Short-circuit: a single UIA candidate at the
            # default reliability is unambiguous.  This is a
            # *deterministic* optimization, not a guess.
            if (
                len(found) == 1
                and found[0].source_type == ObservationSource.UIA
                and found[0].confidence >= 0.8
            ):
                return GroundedTarget(
                    candidate=found[0],
                    resolution_method=f"single_uia_{s_name}",
                    alternatives=0,
                )

        if not all_candidates:
            raise TargetNotGroundedError(
                f"Could not ground target: {target_query!r}",
                query=target_query,
            )

        return self._resolve_ambiguity(all_candidates)

    # ----------------------------------------------------------- internals
    def strategy_order(
        self,
        query: str,
        preferred: Optional[str],
    ) -> List[str]:
        """The deterministic, query-aware order of strategies to try.

        The order is:
          1. The ``preferred`` strategy, if known.
          2. A small set of hint-driven strategies.  These are
             *biases*, not overrides -- the final ranking is done
             on candidate quality, not on the iteration order.
          3. Everything else in reliability order.
        """
        order: List[str] = []
        if preferred and preferred in self._strategies:
            order.append(preferred)

        lower = (query or "").lower()
        # Deterministic text hints.  These are *biases* only; if
        # the hinted strategy produces nothing, the router still
        # tries the rest in reliability order.
        if any(w in lower for w in ("icon", "image", "picture", "object")):
            order.append("yolo")
        if any(w in lower for w in ("text", "word", "label", "title")):
            order.append("ocr")

        # Then add the rest in reliability order, dedup'd.
        rest = sorted(
            self._strategies.values(),
            key=lambda s: (
                -s.source_reliability,  # higher reliability first
                s.name,                  # stable tie-break
            ),
        )
        for s in rest:
            if s.name not in order:
                order.append(s.name)
        return order

    def _resolve_ambiguity(
        self,
        candidates: List[Tuple[str, TargetCandidate]],
    ) -> GroundedTarget:
        """Pick a single winner or raise :class:`AmbiguityError`.

        Algorithm:

          1. Sort all candidates by the deterministic key.
          2. The first element is the winner.
          3. If the second element has an *equal* key to the
             winner, the candidates are indistinguishable and we
             raise :class:`AmbiguityError`.
        """
        if not candidates:
            raise TargetNotGroundedError("No candidates to resolve.", query="")

        sorted_cands = sorted(candidates, key=lambda t: _rank_key(t[1]))
        winner_name, winner = sorted_cands[0]

        # Compare to the rest on the *exact* ranking key.
        winner_key = _rank_key(winner)
        for _other_name, other in sorted_cands[1:]:
            if _rank_key(other) == winner_key:
                # The full set is indistinguishable.
                all_cands = [c for _n, c in sorted_cands]
                raise AmbiguityError(
                    "Multiple indistinguishable candidates; "
                    "the router refuses to guess.",
                    candidates=all_cands,
                )

        return GroundedTarget(
            candidate=winner,
            resolution_method=f"deterministic_rank_{winner_name}",
            alternatives=len(sorted_cands) - 1,
        )


__all__ = [
    "PerceptionRouter",
    "TargetNotGroundedError",
    "AmbiguityError",
]
