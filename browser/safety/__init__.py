"""Browser safety policy (Phase 8).

Closed-set policy: not a singleton, configured per BrowserService
instance.  The policy is *advisory* — it never invents behaviour
the service doesn't have, and it never executes shell commands.
"""
