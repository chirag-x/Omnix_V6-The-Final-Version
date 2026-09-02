"""
Omnix V6 — System 3 (Vision) recovery flow.

Two pure helpers, used by the new public API when a single
grounding call returns a negative status:

  * :func:`retry_with_strategy` — when the first attempt
    returned ``LOW_CONFIDENCE`` or ``TARGET_NOT_FOUND`` or
    ``ACCESSIBILITY_UNAVAILABLE``, try the next-most-reliable
    strategy.  The strategy order is the same reliability
    ranking the :class:`PerceptionRouter` uses (UIA → DERIVED
    → OCR → VISION → SCREEN), so a UIA failure escalates to
    OCR, then to Visual, and so on.

  * :func:`reobserve_and_compare` — for the "the target may
    have moved" case (post-action verification, drag/scroll,
    dynamic UI).  Compares the bbox of the *current* grounding
    to a *baseline* bbox; returns
    :attr:`GroundedElementStatus.TARGET_CHANGED` if the bbox
    moved more than a threshold of pixels.

Both helpers are pure functions over the existing perception
strategies.  They do not introduce new side effects, do not
talk to the LLM, and do not import from the agent / brain
modules.  The new public API threads them in only when the
caller asks.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Iterable, List, Optional, Sequence, Tuple

from vision.grounded_element import (
    ELEMENT_TYPE_UNKNOWN,
    GroundedElement,
    GroundedElementStatus,
    from_target_candidate,
    not_found as _not_found,
)
from vision.observations.targets import TargetCandidate

_log = logging.getLogger(__name__)

# Reliability ranking used by the existing PerceptionRouter.  When
# the router escalates, this is the order it consults.
_RELIABILITY_RANK = ("uia", "derived", "ocr", "vision", "screen")


# Strategies that warrant a retry — the candidate was found but
# the perception was poor.  A genuine "nothing here" is a
# TARGET_NOT_FOUND that the agent should treat as a final
# negative; an OCR failure is genuinely recoverable (the screen
# is still there, the OCR engine just choked).
_RETRY_STATUSES = frozenset({
    GroundedElementStatus.LOW_CONFIDENCE,
    GroundedElementStatus.OCR_FAILED,
    GroundedElementStatus.ACCESSIBILITY_UNAVAILABLE,
})


def _bbox_iou(a: Tuple[int, int, int, int], b: Tuple[int, int, int, int]) -> float:
    """Compute the intersection-over-union of two bboxes.

    Returns ``0.0`` for disjoint bboxes and ``1.0`` for an
    exact match.  Used by :func:`reobserve_and_compare` to
    decide whether the target moved.
    """
    al, at, ar, ab = a
    bl, bt, br, bb = b
    il = max(al, bl)
    it = max(at, bt)
    ir = min(ar, br)
    ib = min(ab, bb)
    iw = max(0, ir - il)
    ih = max(0, ib - it)
    inter = iw * ih
    area_a = max(0, ar - al) * max(0, ab - at)
    area_b = max(0, br - bl) * max(0, bb - bt)
    union = area_a + area_b - inter
    if union <= 0:
        return 0.0
    return inter / union


def _next_strategy(
    current: Optional[str],
    *,
    exclude: Iterable[str] = (),
) -> Optional[str]:
    """Return the next-most-reliable strategy after ``current``.

    Excludes anything in ``exclude``.  Returns ``None`` when
    the ranking is exhausted.
    """
    rank = list(_RELIABILITY_RANK)
    if current in rank:
        idx = rank.index(current) + 1
    else:
        idx = 0
    blocked = {str(s).lower() for s in exclude}
    for name in rank[idx:]:
        if name in blocked:
            continue
        return name
    return None


def retry_with_strategy(
    query: str,
    *,
    attempt,
    exclude: Sequence[str] = (),
    max_attempts: int = 3,
) -> GroundedElement:
    """Retry a grounding call with the next-most-reliable strategy.

    Parameters
    ----------
    query:
        The original target text.
    attempt:
        A zero-arg callable returning a
        :class:`GroundedElement`.  Each call is one attempt;
        the helper invokes it repeatedly, swapping the
        underlying strategy as needed.  The callable is
        responsible for honouring whatever strategy the
        helper picks — typically by closing over a mutable
        ``current_strategy`` variable and reading it before
        each call.
    exclude:
        Strategy names to skip (e.g. ``("uia",)`` to forbid
        UIA retries).
    max_attempts:
        Hard cap on attempts (default 3).  Includes the
        initial attempt.

    Returns
    -------
    The first :class:`GroundedElement` whose status is
    :attr:`GroundedElementStatus.OBSERVED`, or the final
    attempt's result if no attempt succeeds.  When the
    ranking is exhausted before ``max_attempts`` is reached,
    the last attempt is returned unchanged.
    """
    attempts = max(1, int(max_attempts))
    last: Optional[GroundedElement] = None
    for _ in range(attempts):
        try:
            el = attempt()
        except Exception as exc:  # noqa: BLE001
            _log.debug("retry attempt raised: %s", exc)
            last = _not_found(query=query)
            continue
        if not isinstance(el, GroundedElement):
            last = _not_found(query=query)
            continue
        last = el
        if el.status == GroundedElementStatus.OBSERVED:
            return el
        if el.status not in _RETRY_STATUSES:
            # Genuine negative; retrying is unlikely to help.
            return el
        # Otherwise, escalate.  The next attempt() call will
        # read the new strategy from the closure.
    return last if last is not None else _not_found(query=query)


def reobserve_and_compare(
    query: str,
    *,
    current: GroundedElement,
    baseline_bbox: Tuple[int, int, int, int],
    iou_threshold: float = 0.7,
) -> GroundedElement:
    """Compare ``current`` to ``baseline_bbox`` and flag movement.

    Returns :class:`GroundedElement` with status
    :attr:`GroundedElementStatus.TARGET_CHANGED` when the
    IoU between the two bboxes is below ``iou_threshold``.
    The bbox and id of the *current* element are preserved so
    the caller can still see where the target is now; only
    the status flips.

    The helper is a pure function: it does not look at the
    screen, does not call the perception router, does not
    raise.  The caller (typically the new public API's
    ``wait_for`` / ``verify`` flow) is responsible for
    re-grounding first; this helper just compares the
    result to the baseline.
    """
    if not isinstance(current, GroundedElement):
        return _not_found(query=query)
    if not isinstance(baseline_bbox, (tuple, list)) or len(baseline_bbox) != 4:
        return current
    iou = _bbox_iou(current.bbox, tuple(baseline_bbox))
    if iou >= iou_threshold:
        return current
    # Construct a clone with TARGET_CHANGED status.  Avoid
    # mutating the frozen instance.
    new_props = dict(current.properties or {})
    new_props["baseline_bbox"] = list(baseline_bbox)
    new_props["iou"] = float(iou)
    new_props["iou_threshold"] = float(iou_threshold)
    return GroundedElement(
        id=current.id,
        type=current.type,
        text=current.text,
        confidence=current.confidence,
        bbox=current.bbox,
        center=current.center,
        enabled=current.enabled,
        visible=current.visible,
        interactable=current.interactable,
        source=current.source,
        semantic_role=current.semantic_role,
        status=GroundedElementStatus.TARGET_CHANGED,
        monitor_id=current.monitor_id,
        screenshot_id=current.screenshot_id,
        timestamp=time.time(),
        properties=new_props,
    )


def from_candidates(
    candidates: Sequence[TargetCandidate],
    *,
    query: str = "",
    screenshot_id: Optional[str] = None,
    monitor_id: Optional[str] = None,
) -> GroundedElement:
    """Adapter: a sequence of :class:`TargetCandidate` -> a
    single :class:`GroundedElement`.

    * 0 candidates -> :func:`not_found`
    * 1 candidate  -> :func:`from_target_candidate` (OBSERVED)
    * >1 candidates -> :func:`ambiguous` (MULTIPLE_TARGETS),
      bbox is the centroid of the alternatives.
    """
    if not candidates:
        return _not_found(query=query, screenshot_id=screenshot_id, monitor_id=monitor_id)
    if len(candidates) == 1:
        return from_target_candidate(
            candidates[0],
            screenshot_id=screenshot_id,
            monitor_id=monitor_id,
        )
    # Build a MULTIPLE_TARGETS sentinel.  We deliberately do
    # NOT import the named helper to avoid a circular import
    # between ``grounded_element`` and ``recovery``; the
    # local _not_found + a manual clone is equivalent and
    # keeps the dependency direction clean.
    from vision.grounded_element import ambiguous as _ambiguous
    return _ambiguous(
        list(candidates),
        screenshot_id=screenshot_id,
        monitor_id=monitor_id,
    )


__all__ = [
    "retry_with_strategy",
    "reobserve_and_compare",
    "from_candidates",
]
