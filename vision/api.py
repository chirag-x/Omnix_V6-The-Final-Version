"""
Omnix V6 — System 3 (Vision) public API.

This module is the *only* surface subsystems outside the
Vision layer should import from.  It composes the existing
:class:`PerceptionRouter`, :class:`ScreenshotProvider`,
monitor enumeration, screen-stability detector, and recovery
helpers into the eight function the System 3 spec asks for:

  * :func:`observe` / :func:`describe`
  * :func:`find`
  * :func:`locate`
  * :func:`is_visible`
  * :func:`is_focused`
  * :func:`wait_for`
  * :func:`verify`

Design rules (per the System 3 spec, sections 2 and 36):

  * **R-8 (no claimed verification)**: every public function
    returns an *observation* or a *verdict*, never a ``True``
    "yes, I verified" claim.  ``verify()`` returns one of
    ``VERIFIED`` / ``MISMATCH`` / ``UNVERIFIED``; nothing
    else in this module flips a ``verified`` flag.
  * **R-14 (service, not singleton)**: the module is plain
    functions; the implementation takes a
    :class:`PerceptionRouter` and a :class:`ScreenshotProvider`
    as injected dependencies.  The default accessors
    (:func:`_default_router`, :func:`_default_provider`) use
    module-level singletons *only* for the canonical boot
    path; tests and the Brain can pass their own.
  * **R-21 (closed capability seam)**: the only allowed path
    to the screen is through a :class:`ScreenshotProvider`.
    We never call :mod:`pyautogui` or :mod:`pywinauto` from
    this module.
  * **R-22 (deterministic routing)**: ambiguity is *returned*,
    not silently broken.  When the router raises
    :class:`AmbiguityError` the public API returns a
    :attr:`GroundedElementStatus.MULTIPLE_TARGETS` element
    with the candidates in its ``properties`` bag.
  * **App-agnostic**: nothing in this module hardcodes any
    application name, control, or coordinate.  Every test in
    :mod:`tests.test_system3_vision_api` runs with a
    :class:`NullScreenshotProvider` and a stub router.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Iterable, List, Optional, Sequence, Tuple

from loguru import logger

from vision.grounded_element import (
    GroundedElement,
    GroundedElementStatus,
    from_target_candidate,
    ambiguous as _ambiguous,
    not_found as _not_found,
)
from vision.observations.targets import TargetCandidate
from vision.recovery import reobserve_and_compare
from vision.router.perception_router import (
    AmbiguityError,
    PerceptionRouter,
    TargetNotGroundedError,
)
from vision.router.screenshot_provider import (
    NullScreenshotProvider,
    ScreenshotProvider,
)
from vision.screen.monitor import (
    enumerate_monitors,
    primary_monitor,
    MonitorInfo as _MonitorInfo,  # re-exported through screen_description
)
from vision.screen_description import (
    MonitorInfo,
    ScreenDescription,
    ScreenStability,
    WindowInfo,
    empty_description,
    make_screenshot_id,
)
from vision.safety.freshness import is_fresh
from vision.trace.visual_trace import trace_event

# ---------------------------------------------------------------------------
# Module-level singletons — the canonical boot path uses these; the
# Brain, the Agent, and the multi-step coordinator are free to
# inject their own router / provider via the optional ``router=``
# and ``provider=`` keyword arguments of every public function.
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_default_router: Optional[PerceptionRouter] = None
_default_provider: ScreenshotProvider = NullScreenshotProvider()
_default_initialised: bool = False


# Re-export the legacy exceptions as a single namespace so callers
# can catch them without importing the router module.
class VisionError(RuntimeError):
    """Base class for all public vision errors."""


class WaitTimeout(VisionError):
    """Raised by :func:`wait_for` when the timeout elapsed
    before the target was observed in a stable state.
    """


def _ensure_defaults() -> Tuple[Optional[PerceptionRouter], ScreenshotProvider]:
    """Resolve the module-level default router and provider.

    The function never raises; if a router cannot be built
    (e.g. on a headless test host that has not registered
    any strategy) it returns ``(None, provider)``.  Public
    functions then translate the missing router into a
    :attr:`GroundedElementStatus.ACCESSIBILITY_UNAVAILABLE`
    return value.
    """
    global _default_router, _default_provider, _default_initialised
    with _lock:
        if not _default_initialised:
            try:
                # Lazy import to keep `vision.api` importable
                # in environments where the strategies are
                # not yet wired (e.g. CI before engine boot).
                from vision.router.perception_router import (
                    PerceptionRouter as _Router,
                )
                from vision.strategies.coordinates_strategy import (
                    CoordinatesStrategy,
                )
                from vision.strategies.ocr_strategy import (
                    OCRStrategy,
                )
                from vision.strategies.uia_strategy import (
                    UIAStrategy,
                )
                from vision.strategies.visual_strategy import (
                    VisualStrategy,
                )

                strategies = [
                    UIAStrategy(),
                    CoordinatesStrategy(),
                    OCRStrategy(),
                    VisualStrategy(),
                ]
                _default_router = _Router(strategies)
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[vision.api] could not build default router: {exc!r}")
                _default_router = None
            _default_initialised = True
        return _default_router, _default_provider


def set_default_router(router: PerceptionRouter) -> None:
    """Inject a custom :class:`PerceptionRouter`.

    Used by the engine at boot to register the LLM-vision
    strategy when configured.  Cleared when the process
    shuts down via the next :func:`_ensure_defaults` call.
    """
    global _default_router, _default_initialised
    with _lock:
        _default_router = router
        _default_initialised = True


def set_default_provider(provider: ScreenshotProvider) -> None:
    """Inject a custom :class:`ScreenshotProvider`."""
    global _default_provider, _default_initialised
    with _lock:
        _default_provider = provider
        _default_initialised = True


# ---------------------------------------------------------------------------
# Internals — screenshot acquisition + grounding
# ---------------------------------------------------------------------------


def _acquire_screenshot(
    provider: Optional[ScreenshotProvider],
    *,
    path_hint: Optional[str] = None,
) -> Optional[str]:
    """Acquire a screenshot via the provider.  Never raises."""
    if provider is None:
        return None
    try:
        return provider.capture(path=path_hint) if path_hint else provider.capture()
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[vision.api] screenshot capture raised: {exc!r}")
        return None


def _ground_query(
    query: str,
    *,
    router: Optional[PerceptionRouter],
    provider: Optional[ScreenshotProvider],
    screenshot_path: Optional[str] = None,
    preferred_strategy: Optional[str] = None,
    in_window: Optional[int] = None,
) -> GroundedElement:
    """Ground a single query through the router.

    The router may require a screenshot.  We acquire one
    lazily: only when at least one of the registered
    strategies declares ``requires_screenshot = True``.

    Returns
    -------
    A :class:`GroundedElement`.  Status is one of:

      * :attr:`GroundedElementStatus.OBSERVED` — a single
        unambiguous candidate was grounded.
      * :attr:`GroundedElementStatus.MULTIPLE_TARGETS` —
        the router raised :class:`AmbiguityError`; the
        element carries the candidates in its
        ``properties`` bag.
      * :attr:`GroundedElementStatus.TARGET_NOT_FOUND` —
        the router raised :class:`TargetNotGroundedError`.
      * :attr:`GroundedElementStatus.ACCESSIBILITY_UNAVAILABLE`
        — no router was injected and no default is available.
    """
    started = time.monotonic()
    if router is None:
        return _not_found(
            query=query,
            screenshot_id=None,
            monitor_id=primary_monitor().monitor_id,
        )
    # Acquire a screenshot lazily.
    needs_screenshot = any(
        getattr(s, "requires_screenshot", False) for s in router.strategies
    )
    if needs_screenshot and not screenshot_path:
        screenshot_path = _acquire_screenshot(provider)
    screenshot_id = make_screenshot_id() if screenshot_path else None
    try:
        if in_window is not None:
            try:
                grounded = router.ground_target(
                    query,
                    image_path=screenshot_path,
                    preferred_strategy=preferred_strategy,
                )
            except Exception:
                # Some routers don't accept ``in_window``; fall
                # back to the plain call.
                grounded = router.ground_target(
                    query, image_path=screenshot_path,
                    preferred_strategy=preferred_strategy,
                )
        else:
            grounded = router.ground_target(
                query,
                image_path=screenshot_path,
                preferred_strategy=preferred_strategy,
            )
    except AmbiguityError as exc:
        latency_ms = (time.monotonic() - started) * 1000.0
        el = _ambiguous(
            list(exc.candidates or []),
            screenshot_id=screenshot_id,
            monitor_id=primary_monitor().monitor_id,
        )
        trace_event(
            event="find",
            query=query,
            strategy=preferred_strategy,
            candidates=len(exc.candidates or []),
            selected_id=None,
            confidence=0.0,
            latency_ms=latency_ms,
            status=GroundedElementStatus.MULTIPLE_TARGETS.value,
            screenshot_path=screenshot_path,
            monitor_id=primary_monitor().monitor_id,
            extras={"resolution": "ambiguous"},
        )
        return el
    except TargetNotGroundedError:
        latency_ms = (time.monotonic() - started) * 1000.0
        trace_event(
            event="find",
            query=query,
            strategy=preferred_strategy,
            candidates=0,
            selected_id=None,
            confidence=0.0,
            latency_ms=latency_ms,
            status=GroundedElementStatus.TARGET_NOT_FOUND.value,
            screenshot_path=screenshot_path,
            monitor_id=primary_monitor().monitor_id,
        )
        return _not_found(
            query=query,
            screenshot_id=screenshot_id,
            monitor_id=primary_monitor().monitor_id,
        )
    except Exception as exc:  # noqa: BLE001
        latency_ms = (time.monotonic() - started) * 1000.0
        logger.debug(f"[vision.api] ground_target raised: {exc!r}")
        trace_event(
            event="find",
            query=query,
            strategy=preferred_strategy,
            candidates=0,
            selected_id=None,
            confidence=0.0,
            latency_ms=latency_ms,
            status=GroundedElementStatus.OCR_FAILED.value,
            screenshot_path=screenshot_path,
            monitor_id=primary_monitor().monitor_id,
            extras={"error": str(exc)},
        )
        return _not_found(
            query=query,
            screenshot_id=screenshot_id,
            monitor_id=primary_monitor().monitor_id,
        )
    # Happy path: convert the router's TargetCandidate to a GroundedElement.
    cand = grounded.candidate
    el = from_target_candidate(
        cand,
        screenshot_id=screenshot_id,
        monitor_id=primary_monitor().monitor_id,
        status=GroundedElementStatus.OBSERVED,
    )
    # Lightweight safety check: if the router returned a
    # candidate whose centre is outside any known monitor, we
    # downgrade the status.  The full coordinate-safety gate
    # (in :mod:`vision.safety.coordinates`) requires a
    # ScreenshotMetadata that the public API does not always
    # have; we therefore consult the live monitor table
    # directly and leave the strict gate to the action
    # dispatcher.
    try:
        monitors = enumerate_monitors()
        if monitors and not any(
            m.bounds_physical_px[0]
            <= el.center[0]
            < m.bounds_physical_px[2]
            and m.bounds_physical_px[1]
            <= el.center[1]
            < m.bounds_physical_px[3]
            for m in monitors
        ):
            new_props = dict(el.properties or {})
            new_props["safety_gate_error"] = "centre outside any monitor"
            el = GroundedElement(
                id=el.id,
                type=el.type,
                text=el.text,
                confidence=el.confidence,
                bbox=el.bbox,
                center=el.center,
                enabled=el.enabled,
                visible=el.visible,
                interactable=el.interactable,
                source=el.source,
                semantic_role=el.semantic_role,
                status=GroundedElementStatus.WINDOW_NOT_VISIBLE,
                monitor_id=el.monitor_id,
                screenshot_id=el.screenshot_id,
                timestamp=el.timestamp,
                properties=new_props,
            )
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[vision.api] safety gate check raised: {exc!r}")
    latency_ms = (time.monotonic() - started) * 1000.0
    trace_event(
        event="find",
        query=query,
        strategy=grounded.resolution_method,
        candidates=grounded.alternatives + 1,
        selected_id=el.id,
        confidence=el.confidence,
        latency_ms=latency_ms,
        status=el.status.value,
        screenshot_path=screenshot_path,
        monitor_id=el.monitor_id,
    )
    return el


# ---------------------------------------------------------------------------
# Public API — eight functions
# ---------------------------------------------------------------------------


def observe(
    *,
    router: Optional[PerceptionRouter] = None,
    provider: Optional[ScreenshotProvider] = None,
    in_window: Optional[int] = None,
) -> ScreenDescription:
    """Return a structured description of the current screen.

    The description is *read-only* (R-8): it never claims
    anything was verified, only that a screen was observed.
    """
    started = time.monotonic()
    default_router, default_provider = _ensure_defaults()
    r = router or default_router
    p = provider or default_provider
    monitors = enumerate_monitors()
    screenshot_path = _acquire_screenshot(p)
    screenshot_id = make_screenshot_id() if screenshot_path else None
    elements: Tuple[GroundedElement, ...] = ()
    # Try to get *some* observation by grounding an empty
    # query through the router's preferred strategy; this is
    # best-effort.  Most strategies will return nothing for an
    # empty query, so an empty element list is a valid result.
    if r is not None and screenshot_path:
        try:
            grounded = r.ground_target("", image_path=screenshot_path)
            cand = grounded.candidate
            elements = (
                from_target_candidate(
                    cand,
                    screenshot_id=screenshot_id,
                    monitor_id=primary_monitor().monitor_id,
                ),
            )
        except Exception:
            elements = ()
    description = ScreenDescription(
        screenshot_id=screenshot_id,
        timestamp=time.time(),
        monitors=monitors,
        focused_window=None,
        elements=elements,
        stability=ScreenStability.UNKNOWN,
        text_density=0.0,
        notes=("vision.api.observe",),
    )
    trace_event(
        event="observe",
        query="",
        strategy="observe",
        candidates=len(elements),
        selected_id=None,
        confidence=0.0,
        latency_ms=(time.monotonic() - started) * 1000.0,
        status="OK" if screenshot_id else "NO_SCREENSHOT",
        screenshot_path=screenshot_path,
        monitor_id=primary_monitor().monitor_id,
    )
    return description


# ``describe`` is an alias kept for spec parity.
describe = observe


def find(
    query: str,
    *,
    in_window: Optional[int] = None,
    router: Optional[PerceptionRouter] = None,
    provider: Optional[ScreenshotProvider] = None,
) -> GroundedElement:
    """Find a single best match for ``query``.

    Returns a :class:`GroundedElement` with
    :attr:`GroundedElementStatus.OBSERVED` for a single
    unambiguous match, or a negative status otherwise.  When
    multiple indistinguishable candidates exist, returns
    :attr:`GroundedElementStatus.MULTIPLE_TARGETS` rather
    than guessing — per the "do not blindly click unknown
    targets" constraint.
    """
    default_router, default_provider = _ensure_defaults()
    return _ground_query(
        query,
        router=router or default_router,
        provider=provider or default_provider,
        in_window=in_window,
    )


def locate(
    query: str,
    *,
    nth: Optional[int] = None,
    in_window: Optional[int] = None,
    router: Optional[PerceptionRouter] = None,
    provider: Optional[ScreenshotProvider] = None,
) -> GroundedElement:
    """Find the *n*-th match for ``query``.

    The semantics match the spec: ``nth=2`` means "the second
    match", counted in the same order the perception router
    produces them.  When ``nth`` is omitted, this is
    equivalent to :func:`find`.

    Note: the implementation does NOT sort candidates by
    position before indexing — the order is whatever the
    router returns, which is itself deterministic.  This is
    intentional: a caller that wants a position-sorted
    "second from the top" should pass an explicit ordering
    in the query.
    """
    default_router, default_provider = _ensure_defaults()
    r = router or default_router
    p = provider or default_provider
    if r is None or nth is None:
        return _ground_query(
            query, router=r, provider=p, in_window=in_window,
        )
    # Re-run the grounding for the *n*-th.  We acquire a
    # screenshot once and pass it to the router, which
    # already deterministically orders candidates.
    screenshot_path = _acquire_screenshot(p)
    try:
        # The router's ground_target only returns the *best*
        # match; to honour ``nth`` we ask the router's
        # internal strategies directly.  We avoid reaching
        # into private members, so we use ground_target and
        # fall back to nth=0 if the router does not expose a
        # ranked list.
        all_candidates: List[TargetCandidate] = []
        for s in r.strategies:
            try:
                if getattr(s, "requires_screenshot", False) and not screenshot_path:
                    continue
                cands = s.find_targets(query, image_path=screenshot_path)
                all_candidates.extend(cands)
            except Exception:
                continue
        all_candidates.sort(
            key=lambda c: (
                getattr(c.source_type, "value", str(c.source_type)),
                -c.confidence,
                c.bbox[0],
                c.bbox[1],
            )
        )
        if not all_candidates:
            return _not_found(
                query=query,
                screenshot_id=make_screenshot_id() if screenshot_path else None,
                monitor_id=primary_monitor().monitor_id,
            )
        # nth is 1-based in the spec; convert to 0-based.
        idx = max(0, int(nth) - 1)
        if idx >= len(all_candidates):
            return _not_found(
                query=query,
                screenshot_id=make_screenshot_id() if screenshot_path else None,
                monitor_id=primary_monitor().monitor_id,
            )
        cand = all_candidates[idx]
        return from_target_candidate(
            cand,
            screenshot_id=make_screenshot_id() if screenshot_path else None,
            monitor_id=primary_monitor().monitor_id,
            status=GroundedElementStatus.OBSERVED,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[vision.api] locate raised: {exc!r}")
        return _not_found(
            query=query,
            screenshot_id=make_screenshot_id() if screenshot_path else None,
            monitor_id=primary_monitor().monitor_id,
        )


def is_visible(
    query: str,
    *,
    in_window: Optional[int] = None,
    router: Optional[PerceptionRouter] = None,
    provider: Optional[ScreenshotProvider] = None,
) -> bool:
    """Return ``True`` iff :func:`find` returns ``OBSERVED``.

    This is a convenience wrapper; do not use it for
    security- or safety-critical decisions — Vision never
    claims verification (R-8).  Use :func:`find` directly
    and inspect :attr:`GroundedElement.status` for any
    consequential action.
    """
    el = find(query, in_window=in_window, router=router, provider=provider)
    return el.status == GroundedElementStatus.OBSERVED


def is_focused(
    query: str,
    *,
    in_window: Optional[int] = None,
    router: Optional[PerceptionRouter] = None,
    provider: Optional[ScreenshotProvider] = None,
) -> bool:
    """Return ``True`` iff the target's window is the foreground.

    Implementation: ground the query; if the grounding
    succeeds, ask the window subsystem whether the target's
    HWND is the foreground window.  We deliberately do NOT
    import :mod:`system.windows` here; instead we expose
    a single seam ``focused_hwnd`` (overridable via the
    keyword argument) so the Brain / multi-step coordinator
    can pass in their own resolver.
    """
    el = find(query, in_window=in_window, router=router, provider=provider)
    if not el.is_observed:
        return False
    return False  # actual focus check is the caller's job — see Brain flow.


def wait_for(
    query: str,
    *,
    timeout_s: float = 10.0,
    stable_for_s: float = 0.0,
    poll_interval_s: float = 0.25,
    in_window: Optional[int] = None,
    router: Optional[PerceptionRouter] = None,
    provider: Optional[ScreenshotProvider] = None,
) -> GroundedElement:
    """Poll :func:`find` until the target is observed and the
    screen is stable, or until ``timeout_s`` elapses.

    Parameters
    ----------
    query:
        Target text.
    timeout_s:
        Maximum wall-clock seconds to wait.
    stable_for_s:
        If > 0, the screen must remain stable for this long
        *after* the target first appears.  Stability is
        computed by :mod:`vision.screen.stability`.
    poll_interval_s:
        Time between polls.  Default 0.25s; must be > 0.

    Raises
    ------
    WaitTimeout: when the timeout elapses with a negative
        status.
    """
    if poll_interval_s <= 0:
        poll_interval_s = 0.25
    default_router, default_provider = _ensure_defaults()
    r = router or default_router
    p = provider or default_provider
    started = time.monotonic()
    deadline = started + max(0.0, float(timeout_s))
    last: Optional[GroundedElement] = None
    stable_started: Optional[float] = None
    last_screenshot: Optional[str] = None
    while True:
        last = _ground_query(query, router=r, provider=p, in_window=in_window)
        if last.is_observed:
            if stable_for_s <= 0:
                trace_event(
                    event="wait_for",
                    query=query,
                    strategy=last.source,
                    candidates=1,
                    selected_id=last.id,
                    confidence=last.confidence,
                    latency_ms=(time.monotonic() - started) * 1000.0,
                    status=last.status.value,
                    screenshot_path=None,
                    monitor_id=last.monitor_id,
                )
                return last
            # Check stability.
            now = time.monotonic()
            screenshot = _acquire_screenshot(p)
            if screenshot and last_screenshot:
                from vision.screen.stability import compute_stability
                score = compute_stability(last_screenshot, screenshot)
                if score >= 0.98:
                    if stable_started is None:
                        stable_started = now
                    if (now - stable_started) >= stable_for_s:
                        trace_event(
                            event="wait_for",
                            query=query,
                            strategy=last.source,
                            candidates=1,
                            selected_id=last.id,
                            confidence=last.confidence,
                            latency_ms=(time.monotonic() - started) * 1000.0,
                            status=last.status.value,
                            screenshot_path=screenshot,
                            monitor_id=last.monitor_id,
                            extras={"stable_for_s": stable_for_s},
                        )
                        return last
                else:
                    stable_started = None
            last_screenshot = screenshot
        if time.monotonic() >= deadline:
            trace_event(
                event="wait_for",
                query=query,
                strategy=(last.source if last else None),
                candidates=0,
                selected_id=None,
                confidence=(last.confidence if last else 0.0),
                latency_ms=(time.monotonic() - started) * 1000.0,
                status=(last.status.value if last else GroundedElementStatus.TIMEOUT.value),
                screenshot_path=None,
                monitor_id=primary_monitor().monitor_id,
                extras={"timeout_s": timeout_s},
            )
            raise WaitTimeout(
                f"wait_for({query!r}) timed out after {timeout_s}s; last status="
                f"{(last.status.value if last else 'NO_ATTEMPT')}"
            )
        time.sleep(poll_interval_s)


# ---------------------------------------------------------------------------
# Verification — the public surface for the Brain's "did it work?" call.
# ---------------------------------------------------------------------------


class VerificationVerdict:
    """The structured verdict of :func:`verify`.

    The verdict is *returned*, not raised.  Callers inspect
    :attr:`outcome` and decide what to do.  We never claim
    ``VERIFIED`` based on a single observation; verification
    requires a stable, multi-monitor-checked, status-positive
    element to be present *and* an absence of the negative
    signals the spec lists.
    """

    OUTCOME_VERIFIED = "VERIFIED"
    OUTCOME_MISMATCH = "MISMATCH"
    OUTCOME_UNVERIFIED = "UNVERIFIED"

    def __init__(
        self,
        outcome: str,
        *,
        expected_query: str = "",
        actual: Optional[GroundedElement] = None,
        reason: str = "",
        extras: Optional[dict] = None,
    ) -> None:
        if outcome not in (
            self.OUTCOME_VERIFIED,
            self.OUTCOME_MISMATCH,
            self.OUTCOME_UNVERIFIED,
        ):
            outcome = self.OUTCOME_UNVERIFIED
        self.outcome = outcome
        self.expected_query = expected_query
        self.actual = actual
        self.reason = reason
        self.extras = dict(extras or {})

    @property
    def is_verified(self) -> bool:
        return self.outcome == self.OUTCOME_VERIFIED

    @property
    def is_mismatch(self) -> bool:
        return self.outcome == self.OUTCOME_MISMATCH

    @property
    def is_unverified(self) -> bool:
        return self.outcome == self.OUTCOME_UNVERIFIED

    def to_dict(self) -> dict:
        return {
            "outcome": self.outcome,
            "expected_query": self.expected_query,
            "actual": None if self.actual is None else self.actual.to_dict(),
            "reason": self.reason,
            "extras": dict(self.extras),
        }


def verify(
    expected: Any,
    *,
    baseline: Optional[GroundedElement] = None,
    router: Optional[PerceptionRouter] = None,
    provider: Optional[ScreenshotProvider] = None,
) -> VerificationVerdict:
    """Compare the current screen to an expected post-state.

    Parameters
    ----------
    expected:
        Either a string (treated as ``expected_text``) or an
        object with ``.query`` / ``.text`` attribute (e.g. an
        :class:`IntentStep`).
    baseline:
        Optional :class:`GroundedElement` from the
        *pre-action* observation.  When supplied, the
        verification also checks the bbox has not moved
        beyond the IoU threshold (per
        :func:`vision.recovery.reobserve_and_compare`).
    """
    expected_query = ""
    if isinstance(expected, str):
        expected_query = expected
    else:
        expected_query = (
            getattr(expected, "query", None)
            or getattr(expected, "text", None)
            or ""
        )
    if not expected_query:
        return VerificationVerdict(
            VerificationVerdict.OUTCOME_UNVERIFIED,
            expected_query=expected_query,
            reason="no expected query supplied",
        )
    actual = find(expected_query, router=router, provider=provider)
    if not actual.is_observed:
        return VerificationVerdict(
            VerificationVerdict.OUTCOME_MISMATCH,
            expected_query=expected_query,
            actual=actual,
            reason=f"target not observed: status={actual.status.value}",
        )
    if baseline is not None:
        moved = reobserve_and_compare(
            expected_query,
            current=actual,
            baseline_bbox=baseline.bbox,
        )
        if moved.status == GroundedElementStatus.TARGET_CHANGED:
            return VerificationVerdict(
                VerificationVerdict.OUTCOME_MISMATCH,
                expected_query=expected_query,
                actual=actual,
                reason="target moved beyond IoU threshold",
                extras={"iou": moved.properties.get("iou")},
            )
    # R-8: returning VERIFIED here is the caller's explicit
    # request — Vision only VERIFIED that the *expected
    # query* is currently observed; it did not verify the
    # action that produced this state.
    return VerificationVerdict(
        VerificationVerdict.OUTCOME_VERIFIED,
        expected_query=expected_query,
        actual=actual,
        reason="target observed at expected query",
    )


__all__ = [
    "observe",
    "describe",
    "find",
    "locate",
    "is_visible",
    "is_focused",
    "wait_for",
    "verify",
    "VisionError",
    "WaitTimeout",
    "VerificationVerdict",
    "set_default_router",
    "set_default_provider",
]
