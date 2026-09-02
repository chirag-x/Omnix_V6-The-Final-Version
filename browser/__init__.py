"""
Omnix V6 — Browser subsystem (Phase 8).

A small, closed, V6-native browser automation subsystem.  The package
layout is::

    browser/
        models/        # frozen dataclasses, enums (the contract)
        session/       # session/context/page lifecycle (Playwright wrapper)
        strategies/    # DOM / accessibility / text targeting
        router/        # dispatches BrowserRequest to a closed action set
        safety/        # safety policy + URL allowlist (advisory)

The subsystem is designed so the *only* path from a Plan into a browser
is through :class:`core.services.browser_service.BrowserService`.  The
service is the canonical boundary; every action that mutates the
browser is invoked there.  There is no ``BrowserManager``, no
``BrowserController`` singleton, no parallel capability registry.

Hard prohibitions (Phase 8 spec, verbatim):

    * The browser subsystem must NOT import or execute ``os.system``,
      ``os.popen``, ``subprocess`` shell commands, arbitrary shell
      execution, or arbitrary Python execution.
    * Do not allow arbitrary browser JavaScript execution from
      LLM-generated plans.
    * Do not expose session cookies to the LLM.
    * Do not expose passwords in logs.
    * Do not persist secrets in browser observations.

This package is the *only* public surface of the browser subsystem.
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
