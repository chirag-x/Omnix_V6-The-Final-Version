"""Browser typed contracts (Phase 8).

Frozen dataclasses only — no business logic.  These are the
shape that crosses the boundary between the planner/agent and
:class:`core.services.browser_service.BrowserService`.
"""

from browser.models.contracts import (
    BrowserAction,
    BrowserElement,
    BrowserObservation,
    BrowserPageState,
    BrowserRequest,
    BrowserResult,
    BrowserSessionInfo,
    BrowserTarget,
    LocatorKind,
    TargetResolutionMethod,
    BROWSER_OBSERVATION_SOURCES,
)

__all__ = [
    "BrowserAction",
    "BrowserElement",
    "BrowserObservation",
    "BrowserPageState",
    "BrowserRequest",
    "BrowserResult",
    "BrowserSessionInfo",
    "BrowserTarget",
    "LocatorKind",
    "TargetResolutionMethod",
    "BROWSER_OBSERVATION_SOURCES",
]
