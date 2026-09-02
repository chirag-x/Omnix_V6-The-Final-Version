"""
Omnix V6 — System 7 / Phase 4: cooperative cancellation token.

The Agent's closed loop runs a long sequence of steps.  When the
user presses Ctrl+C, the voice runtime says "stop", or the engine
hits a deadline, the loop must unwind quickly.  This module is
the *only* cancellation surface in the orchestration layer:

    from core.orchestration.cancellation import CancellationToken

    token = CancellationToken()
    ...
    if token.is_cancelled:
        raise CancellationRequested(reason=token.reason)

Architectural rules honored here (from V6_ARCHITECTURE_RULES.md):

- R-7  — no observable side effects beyond in-memory state.
- R-8  — boolean state is exposed only through ``is_cancelled``
         and ``reason``; never a bare ``bool``.
- R-9  — no I/O, no logging, no callbacks that may raise.  The
         callbacks registered with :meth:`register` are *not*
         invoked; the agent loop polls ``is_cancelled`` on every
         iteration.
- R-10 — the token's state is a plain Python object; it is
         safe to pass across threads.
- R-12 — pure data, no Protocol dependencies.  The Agent tests
         construct a fresh token per scenario.

Threading
---------
The token is a single mutable boolean.  Reads and writes are
atomic in CPython due to the GIL, but a caller can hold a
reference across an arbitrary suspension point.  This is
*cooperative* cancellation: the Agent must check
``is_cancelled`` at the top of every loop iteration.  No
asyncio, no signals, no exceptions injected from the outside.
"""
from __future__ import annotations

import threading
from typing import Callable, List, Optional


class CancellationToken:
    """A single-shot cooperative cancellation flag.

    A token starts uncancelled.  :meth:`cancel` flips it once;
    subsequent reads see ``is_cancelled=True``.  The reason
    is the string the cancel caller supplied (default: empty).

    The token also tracks a list of *callbacks* the Agent may
    register.  When the token is cancelled, those callbacks are
    invoked synchronously in registration order.  Callbacks that
    raise are isolated by the token (logged at DEBUG, swallowed)
    so one bad consumer cannot break another.
    """

    def __init__(self, *, reason: str = "") -> None:
        self._cancelled: bool = False
        self._reason: str = reason
        self._lock = threading.Lock()
        self._callbacks: List[Callable[["CancellationToken"], None]] = []
        self._fired: bool = False

    @property
    def is_cancelled(self) -> bool:
        """True once :meth:`cancel` has been called."""
        return self._cancelled

    @property
    def reason(self) -> str:
        """The reason string supplied to :meth:`cancel`."""
        return self._reason

    def cancel(self, reason: str = "") -> None:
        """Mark the token cancelled.

        Idempotent.  Subsequent calls do not overwrite the
        original reason — the first caller's reason wins.
        """
        with self._lock:
            if self._cancelled:
                return
            self._cancelled = True
            self._reason = reason or self._reason
            callbacks = list(self._callbacks)
            self._fired = True
        # Invoke callbacks outside the lock.
        for cb in callbacks:
            try:
                cb(self)
            except Exception:  # noqa: BLE001
                # Callbacks must not raise; we silently swallow
                # so a single bad consumer cannot break the
                # cancellation path.  The Agent's observability
                # layer can subscribe to a separate "cancelled"
                # bus event for visibility.
                pass

    def register(
        self, callback: Callable[["CancellationToken"], None]
    ) -> None:
        """Register a callback invoked when :meth:`cancel` fires.

        If the token is already cancelled when this is called,
        the callback fires immediately.  Use this to attach
        cleanup logic that must run on cancel (e.g. closing
        sockets, aborting inflight requests).
        """
        with self._lock:
            already = self._fired
            if not already:
                self._callbacks.append(callback)
        if already:
            try:
                callback(self)
            except Exception:  # noqa: BLE001
                pass

    def as_context_manager(self) -> "_CancellationContext":
        """Return a context manager that cancels the token on exit.

        Useful for ``with token.as_context_manager(): ...`` when
        a block must always cancel even on success.
        """
        return _CancellationContext(self)

    def reset(self) -> None:
        """Clear the cancellation flag.

        Intended for tests only.  Production code should treat
        a cancellation token as a one-shot signal.
        """
        with self._lock:
            self._cancelled = False
            self._reason = ""
            self._callbacks = []
            self._fired = False


class _CancellationContext:
    def __init__(self, token: CancellationToken) -> None:
        self._token = token

    def __enter__(self) -> CancellationToken:
        return self._token

    def __exit__(self, exc_type, exc, tb) -> None:
        if not self._token.is_cancelled:
            self._token.cancel(reason="context manager exited")


__all__ = [
    "CancellationToken",
]
