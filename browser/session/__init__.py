"""Browser session/context/page lifecycle (Phase 8).

The session owns a Playwright ``Browser``, ``BrowserContext`` and one
or more ``Page`` objects.  The lifecycle is explicit:

    BrowserSession
        └── BrowserContext (cookies, storage, viewport)
                └── Page (single page; the closed action set mutates this)

The module isolates the V6 codebase from Playwright's API surface so
the rest of V6 imports only :mod:`browser.models` and the
:class:`core.services.browser_service.BrowserService` boundary.
"""

from browser.session.session import (
    BrowserSession,
    BrowserSessionState,
)

__all__ = ["BrowserSession", "BrowserSessionState"]
