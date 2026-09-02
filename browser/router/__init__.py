"""Browser router (Phase 8).

A closed-set dispatch layer: it takes a :class:`BrowserRequest` and
routes it to the right method on a :class:`BrowserSession`.  It
returns a :class:`BrowserResult`; it never mutates a session's
state in unexpected ways.

The router is the only place that *executes* a browser action.
The :class:`BrowserService` (in ``core/services/``) owns the
session registry; the router is invoked once per request.
"""

from browser.router.dispatcher import BrowserRouter, BrowserRouterError

__all__ = ["BrowserRouter", "BrowserRouterError"]
