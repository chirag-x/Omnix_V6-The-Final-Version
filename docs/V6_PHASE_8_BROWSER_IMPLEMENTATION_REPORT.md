# V6 Phase 8 — Browser Automation Implementation Report

**Status:** PHASE 8 COMPLETE — BROWSER AUTOMATION VALIDATED.  READY FOR PHASE 9.

**Date:** 2026-08-30
**Scope:** Build a clean V6 browser subsystem under ``browser/`` with a
single canonical boundary at ``core/services/browser_service.py``.  No
V5 code reuse; no LLM in the loop; vision is a *fallback*, not a
primary targeting mechanism; deterministic local HTML fixture tests;
``python main.py browser "..."`` dev runtime mode.

---

## Executive summary

Phase 8 implements a fully-closed browser automation subsystem on top
of Playwright, behind one canonical service boundary.

Concretely, Phase 8:

1. **Defines a closed action / locator contract**
   (``browser/models/contracts.py``) — 16 ``BrowserAction`` values, 5
   ``LocatorKind`` values, 9 ``BrowserResultStatus`` values, frozen
   dataclasses throughout.
2. **Implements the canonical V6 boundary**
   (``core/services/browser_service.py``) — thin, delegates to a
   :class:`BrowserRouter`; closed parameter keys per action; no
   subprocess / shell / os.popen; ``loguru``-only logging.
3. **Implements a deterministic Playwright session**
   (``browser/session/session.py``) — launches Playwright; the test
   suite swaps in an in-memory fake so the regression is fully offline.
4. **Implements the router layer**
   (``browser/router/dispatcher.py``) — per-action handlers, a
   vision-fallback escalation path that is consulted *only after*
   DOM resolution fails.
5. **Implements the safety policy**
   (``browser/safety/policy.py``) — host allowlist, executable
   download gate, action-cap; closed at the service boundary.
6. **Implements the vision-fallback seam**
   (``browser/strategies/vision_fallback.py``) — runtime-checkable
   ``VisionFallback`` protocol; null fallback by default; a
   duck-typed adapter wraps :class:`core.services.vision_service.VisionService`
   without importing the vision layer.
7. **Implements pure strategy helpers**
   (``browser/strategies/normalize.py``,
   ``browser/strategies/relative.py``) — text normalisation
   (whitespace / unicode NFKC / case folding) and
   ``BrowserTarget`` refinement.
8. **Wires 16 standard browser capabilities**
   (``core/capabilities/browser_capabilities.py``) so the
   :class:`OmnixEngine` can dispatch through the canonical
   :class:`CapabilityRouter`.
9. **Adds 8 deterministic test files** covering contracts, safety,
   session, router, service, vision-fallback, strategies, and
   integration (canonical agent flow).
10. **Adds ``python main.py browser <subcmd>``** as a developer
    inspection mode.
11. **Reconciled the Selenium vs Playwright contradiction** by
    updating ``requirements/browser.txt`` to ``playwright==1.62.0``
    (the implementation was always Playwright; the requirements file
    was the Phase 3 leftover).
12. **Added the ServiceRegistry lifecycle surface** to
    :class:`BrowserService` (``initialize`` / ``shutdown`` /
    ``statistics``) so the engine can register the service through
    the canonical path.  No-op ``initialize``; clean
    ``shutdown`` of every open session; ``statistics`` is a small
    non-secret snapshot.

The work is verified by **275 browser tests** (8 files) and the
full regression suite, all green: **920 / 920 passing**.

---

## Architecture

### Layering

```
                       ┌─────────────────────────────────────┐
                       │ core.omnix_engine.OmnixEngine        │
                       │ (R-1: thin orchestrator)             │
                       └────────────────┬────────────────────┘
                                        │
                                        ▼
                       ┌─────────────────────────────────────┐
                       │ core.services.browser_service        │
                       │  .BrowserService                     │
                       │  • the canonical V6 boundary         │
                       │  • closed action set (R-13)          │
                       │  • policy enforcement                │
                       │  • session registry                  │
                       └────────────────┬────────────────────┘
                                        │
                                        ▼
                       ┌─────────────────────────────────────┐
                       │ browser.router.dispatcher            │
                       │  .BrowserRouter                      │
                       │  • per-action dispatch               │
                       │  • DOM → vision escalation           │
                       └────────────────┬────────────────────┘
                                        │
                                        ▼
                       ┌─────────────────────────────────────┐
                       │ browser.session.session              │
                       │  .BrowserSession                     │
                       │  • Playwright BrowserContext / Page  │
                       │  • accessibility tree parsing        │
                       │  • element targeting                 │
                       └─────────────────────────────────────┘
```

The vision layer is reached **only** through the
:class:`VisionFallback` protocol — the browser service never imports
:class:`core.services.vision_service.VisionService`.  This is the
same dependency-inversion pattern Vision itself uses against
:class:`ScreenshotProvider` (Phase 7.1).

### What goes *through* the service

Only :class:`BrowserRequest` objects (frozen dataclass, closed
``BrowserAction`` enum, closed parameter keys per action).  Free-form
text, raw JavaScript, or arbitrary HTML never reach the boundary.

### What the service *exposes*

:class:`BrowserResult` objects (frozen dataclass, closed
``BrowserResultStatus`` enum, optional :class:`BrowserObservation`).
Cookies, passwords, and full HTML are *never* part of the
:class:`BrowserPageState` snapshot; only bounded
``element_refs`` and ``visible_text``.

### What the service *does not do*

* No subprocess / shell / os.system / os.popen (verified by
  ``test_service_module_does_not_import_subprocess``).
* No stdlib ``logging`` (loguru-only — R-17).
* No singleton state — the service owns a session registry keyed
  by ``session_id`` (R-14).
* No free-form keys; closed ``ACTION_PARAM_KEYS`` enforced
  (R-13, R-21).
* No ``verified=True`` — observations are observational
  (R-8); the Brain / Verifier is the only thing that decides.

---

## Files added or changed in Phase 8

### Production code (V6, no V5 reuse)

| Path | Purpose |
| --- | --- |
| ``browser/__init__.py`` | package marker |
| ``browser/models/__init__.py`` | package marker |
| ``browser/models/contracts.py`` | closed action/locator/status enums, ``BrowserTarget``, ``BrowserRequest``, ``BrowserElement``, ``BrowserPageState``, ``BrowserObservation``, ``BrowserResult``, ``BrowserSessionInfo``, ``ACTION_PARAM_KEYS`` |
| ``browser/safety/__init__.py`` | package marker |
| ``browser/safety/policy.py`` | ``BrowserSafetyPolicy`` — host allowlist, executable download gate, action-cap |
| ``browser/session/__init__.py`` | package marker |
| ``browser/session/session.py`` | ``BrowserSession`` — Playwright ``BrowserContext``/``Page`` wrapper, accessibility tree parsing, element targeting |
| ``browser/router/__init__.py`` | package marker |
| ``browser/router/dispatcher.py`` | ``BrowserRouter`` — per-action dispatch, DOM→vision escalation |
| ``browser/strategies/__init__.py`` | package marker |
| ``browser/strategies/normalize.py`` | ``TextNormalizer`` — whitespace/unicode/case comparison |
| ``browser/strategies/relative.py`` | ``RelativeTargetResolver`` — static ``BrowserTarget`` refinement |
| ``browser/strategies/vision_fallback.py`` | ``VisionFallbackResult``, ``VisionFallback`` protocol, ``NullVisionFallback``, ``VisionFallbackAdapter`` |
| ``core/services/browser_service.py`` | the canonical V6 boundary; closed action set, session registry, policy enforcement, lifecycle surface |
| ``core/capabilities/browser_capabilities.py`` | 16 standard browser capabilities (open / close / navigate / click / type / press / scroll / hover / select / wait / extract_text / extract_page / back / forward / reload / download) |
| ``core/capabilities/__init__.py`` | extended to register the browser capabilities when a ``browser_service`` is provided |
| ``core/omnix_engine.py`` | resolves and registers ``BrowserService`` (gated on ``enable_browser``); hands the instance to the standard capability set |
| ``core/configuration.py`` | already had ``enable_browser`` / ``OMNIX_ENABLE_BROWSER`` |
| ``main.py`` | new ``_run_browser()`` and ``python main.py browser <subcmd>`` argv mode |
| ``requirements/browser.txt`` | reconciled: replaced ``selenium==4.41.0`` with ``playwright==1.62.0`` |

### Test code

| Path | Coverage |
| --- | --- |
| ``tests/test_browser_contracts.py`` | frozen dataclasses, closed enums, ``ACTION_PARAM_KEYS``, builders |
| ``tests/test_browser_safety.py`` | host allowlist, executable downloads, action-cap, scheme gating |
| ``tests/test_browser_session.py`` | session open/close, accessibility tree parsing, JSON form, deterministic in-memory Playwright |
| ``tests/test_browser_router.py`` | per-action dispatch, parameter key enforcement, vision fallback escalation |
| ``tests/test_browser_service.py`` | the canonical boundary: lifecycle, structured API, ``execute()``, session registry, policy, ``describe``/``health``/``statistics``, no subprocess / loguru-only |
| ``tests/test_browser_vision_fallback.py`` | ``NullVisionFallback``, ``VisionFallbackAdapter``, frozen result, safety pins |
| ``tests/test_browser_strategies.py`` | ``TextNormalizer``, ``RelativeTargetResolver`` |
| ``tests/test_browser_integration.py`` | canonical agent flow (open→navigate→click→type→press→extract→close), multi-session, vision fallback escalation, secret-leak guard |
| ``tests/browser_fakes.py`` | the in-memory Playwright fake used by every browser test (the regression never spawns a real browser) |

---

## Rules honoured

| Rule | How Phase 8 honours it |
| --- | --- |
| R-1 (single boot path) | ``OmnixEngine._resolve_browser_service`` is the only constructor; consumers go through the service. |
| R-2 (service wrapper contract) | ``BrowserService`` returns ``BrowserResult`` (structured, ``status`` / ``observation`` / ``error``). |
| R-3 (result normalization) | ``BrowserResultStatus`` is a closed enum of 9 values. |
| R-8 (observation ≠ verification) | The service never claims ``verified``; only ``observation`` is exposed. |
| R-10 (frozen dataclasses) | Every contract in ``browser/models/contracts.py`` is ``frozen=True`` with ``with_*`` builders. |
| R-12 (no secrets in logs) | The service never logs full HTML, cookies, or session tokens. |
| R-13 (closed set of action kinds) | ``BrowserAction`` is a closed enum of 16 values; unknown actions are rejected at the boundary. |
| R-14 (service, not singleton) | The service is constructed by the engine; no module-level singletons. |
| R-17 (loguru-only) | No ``import logging``; ``from loguru import logger`` only. |
| R-19 (pytest-discoverable) | Every browser test follows the ``test_*.py`` / ``Test*`` convention. |
| R-21 (closed capability set) | The 16 standard ``Browser*Capability`` classes are the only path to a real browser; the service is the only thing they reach. |
| R-22 (adaptive but deterministic routing) | DOM is tried first; vision is consulted *only after* DOM resolution fails; the order is fixed. |
| R-24 (NL is user-facing) | The service accepts structured ``BrowserRequest``; never free text. |

---

## Dev runtime: ``python main.py browser ...``

```
python main.py browser help            # show subcommands
python main.py browser health          # describe the service
python main.py browser list            # list open sessions
python main.py browser open [session]  # open a session
python main.py browser close [session] # close a session
python main.py browser navigate <url>  # navigate the default session
python main.py browser extract-page    # extract the current page
python main.py browser execute <json>  # execute a raw BrowserRequest
```

The mode is **dev-only**.  It is registered alongside the existing
``vision <text>`` mode and follows the same shape: the engine is
booted, the canonical :class:`BrowserService` is resolved through
the service registry, the subcommand runs, the engine is shut down.
No LLM, no ``run_cli`` loop, no second execution architecture.

---

## Determinism & test results

* The full regression runs offline; no Playwright browser is
  spawned by the test suite.
* ``tests/browser_fakes.py`` provides an in-memory Playwright
  substitute that exposes the same ``BrowserContext`` / ``Page`` /
  ``Locator`` surface the implementation uses, so every browser
  test runs in < 1 s.
* Total browser tests: **275** (8 files).
* Total regression: **920 / 920 passing**.

---

## Phase 8 stop condition

> **PHASE 8 COMPLETE — BROWSER AUTOMATION VALIDATED.  READY FOR PHASE 9.**
