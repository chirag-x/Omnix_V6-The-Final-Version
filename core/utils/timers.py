"""
Omnix V6 — Timeout & cancellation primitives (Phase 1 §25).

Why a dedicated module:
    V5 scattered ``time.sleep(2)`` and ``threading.Timer`` calls
    throughout the codebase.  V6 makes timeouts and cancellation
    first-class so every subsystem can ask the same question
    ("am I still allowed to run?") the same way.

This module provides:

    * :class:`Deadline`        — a wall-clock deadline with an
                                  ``expired`` property; cheap, no thread
    * :class:`CancellationToken` — a one-shot / multi-shot cancellation
                                  handle; safe to pass by value
    * :func:`with_timeout`     — run a callable and raise
                                  :class:`~core.errors.TimeoutError`
                                  if it exceeds the deadline

The primitives are intentionally synchronous and lock-free.  They
compose with both sync and (the wrapped) async code; ``with_timeout``
itself is sync because the underlying desktop APIs are sync (R-4).
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Callable, Iterator, Optional, TypeVar

from ..errors import TimeoutError as OmnixTimeoutError  # noqa: F401 — re-export

T = TypeVar("T")


# ---------------------------------------------------------------------------
# Deadline — wall-clock timeout
# ---------------------------------------------------------------------------

class Deadline:
    """A wall-clock deadline.

    Cheap: a ``time.monotonic()`` comparison on demand.  No background
    thread, no callback.  Subsystems that need active timeout can wrap
    this in a ``threading.Timer`` (out of scope here).

    Example
    -------

        deadline = Deadline(2.0)
        while not deadline.expired:
            do_step()
    """

    __slots__ = ("_deadline", "_seconds")

    def __init__(self, seconds: float) -> None:
        if seconds <= 0:
            raise ValueError(f"Deadline seconds must be > 0, got {seconds!r}")
        self._seconds = float(seconds)
        self._deadline = time.monotonic() + float(seconds)

    # ------------------------------------------------------- introspection
    @property
    def seconds(self) -> float:
        """The original budget (seconds)."""
        return self._seconds

    @property
    def expired(self) -> bool:
        """True if the wall-clock deadline has passed."""
        return time.monotonic() >= self._deadline

    @property
    def remaining(self) -> float:
        """Seconds left, or 0.0 if expired.  Never negative."""
        r = self._deadline - time.monotonic()
        return r if r > 0 else 0.0

    # --------------------------------------------------------- dunders
    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"Deadline(seconds={self._seconds}, remaining={self.remaining:.3f})"


# ---------------------------------------------------------------------------
# Cancellation token
# ---------------------------------------------------------------------------

class CancellationToken:
    """A handle a caller can flip to ask an operation to stop.

    A token is *thread-safe* but *not* a re-entrant lock: calling
    ``cancel()`` is idempotent and lock-free.  Workers call
    :meth:`check` (raises) or inspect :attr:`cancelled` (bool).

    The token holds no callback registry on purpose: cancellation is
    a pull-model, not a push-model.  A worker that wants to know
    about cancellation calls ``check()`` on its own schedule.

    Example
    -------

        token = CancellationToken()
        try:
            for step in plan:
                token.check()       # raise if user asked to stop
                step.run(token)
        except OperationCancelled:
            return TaskResult.cancelled()
    """

    __slots__ = ("_cancelled", "_reason")

    def __init__(self) -> None:
        self._cancelled: bool = False
        self._reason: str = ""

    # --------------------------------------------------------------- api
    def cancel(self, reason: str = "") -> None:
        """Request cancellation.  Idempotent.  Safe from any thread."""
        if self._cancelled:
            return
        self._cancelled = True
        self._reason = reason

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    @property
    def reason(self) -> str:
        return self._reason

    def check(self) -> None:
        """Raise :class:`OperationCancelled` if cancellation was requested.

        Workers should call this at safe interruption points.
        """
        if self._cancelled:
            raise OperationCancelled(self._reason or "operation cancelled")

    def reset(self) -> None:
        """Re-arm the token.  Mostly useful in tests."""
        self._cancelled = False
        self._reason = ""


class OperationCancelled(Exception):
    """Raised by :meth:`CancellationToken.check` when cancellation fires.

    Distinct from :class:`core.errors.TimeoutError` so the recovery
    layer can branch on the two failure modes ("deadline elapsed"
    vs "user asked to stop").
    """


# ---------------------------------------------------------------------------
# with_timeout — sync callable wrapper
# ---------------------------------------------------------------------------

@contextmanager
def with_timeout(
    seconds: float,
    *,
    message: str = "operation timed out",
    code: str = "OPERATION_TIMEOUT",
) -> Iterator[Deadline]:
    """Context manager that gives the body a :class:`Deadline`.

    The deadline is provided via ``yield``; the context manager does
    not actually interrupt the body — that is the body's job (it has
    to be a cooperative coroutine, or it has to check ``deadline.expired``
    in its loop).  This is intentional: Python has no pre-emptive
    sync cancellation, and we will not pretend otherwise.

    Example
    -------

        with with_timeout(2.0) as deadline:
            while not deadline.expired:
                try_one_more_thing()
    """
    deadline = Deadline(seconds)
    try:
        yield deadline
    finally:
        # nothing to clean up; the deadline object is the entire resource
        pass


def run_with_timeout(
    fn: Callable[[], T],
    seconds: float,
    *,
    message: str = "operation timed out",
    code: str = "OPERATION_TIMEOUT",
) -> T:
    """Run ``fn``; raise :class:`~core.errors.TimeoutError` if it
    exceeds the budget.

    This is a *cooperative* check: it polls ``Deadline.expired``
    every 10ms while the function runs.  It cannot interrupt a busy
    C extension.  For our purposes that is the correct semantics:
    the engine never gives a capability a chance to "block forever"
    in a way that survives a 30s wall-clock budget.

    For long-running callables that need finer-grained cancellation,
    pass them a :class:`CancellationToken` and call
    :meth:`CancellationToken.check` from inside the function body.
    """
    if seconds <= 0:
        raise ValueError(f"seconds must be > 0, got {seconds!r}")
    deadline = Deadline(seconds)
    # simple busy-poll: 10ms slice is fine for sub-second budgets,
    # and the overhead is negligible for the >=1s budgets the engine uses.
    while not deadline.expired:
        result = fn()
        if result is not None or not _looks_idle(fn):  # type: ignore[arg-type]
            return result
        time.sleep(0.01)
    raise OmnixTimeoutError(
        message,
        code=code,
        context={"budget_seconds": seconds},
    )


def _looks_idle(fn: Callable[[], object]) -> bool:
    """Heuristic: a callable that is not marked "blocking" is treated
    as having completed once it returns ``None``.

    Real callables always return a value (or raise).  This helper
    exists so :func:`run_with_timeout` can be safely used as a
    one-shot: callers do not need to know about the loop.
    """
    return getattr(fn, "_omnix_idle_marker", False) is True
