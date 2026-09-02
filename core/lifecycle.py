"""
Omnix V6 — Lifecycle primitives (R-9).

R-9 mandates a uniform lifecycle shape for *every* subsystem:

    initialize() -> bool          # idempotent, side-effect-once
    shutdown()   -> None          # idempotent, safe to call twice
    initialized  -> bool          # property
    statistics() -> Dict[str,Any] # always callable
    __repr__()                    # always callable, debuggable string

This module provides:

    * :class:`LifecycleState`    — the canonical enum
    * :class:`LifecycleMixin`     — a mixin that supplies the uniform
                                    ``initialize`` / ``shutdown`` /
                                    ``initialized`` semantics; subclasses
                                    only need to implement
                                    ``_do_initialize()`` and
                                    ``_do_shutdown()``.
    * :class:`BaseSubsystem`      — a convenience base class that combines
                                    the mixin with the ``OmnixSubsystem``
                                    contract (see :mod:`core.service_registry`).

The mixin intentionally uses *plain attribute* storage (not
``@property``) so it composes cleanly with frozen dataclasses.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict


class LifecycleState(str, Enum):
    """Canonical lifecycle states (R-9).

    Transitions are:

        CREATED ──initialize()──▶ INITIALIZING ──ok──▶ READY
                                                            │
                                                            ▼ run() (optional)
                                                         RUNNING
                                                            │
                                                            ▼
                                                        STOPPING ──ok──▶ STOPPED

    Failure transitions are also allowed (INITIALIZING → STOPPED with
    an ``initialization_error``).  The engine never runs code on a
    subsystem whose state is not ``READY`` or ``RUNNING``.
    """

    CREATED = "created"
    INITIALIZING = "initializing"
    READY = "ready"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"

    def is_terminal(self) -> bool:
        return self is LifecycleState.STOPPED

    def is_alive(self) -> bool:
        return self in (
            LifecycleState.READY,
            LifecycleState.RUNNING,
        )


class LifecycleMixin:
    """Mixin providing the R-9 lifecycle surface.

    Subclasses MUST:
        - set ``self._lifecycle_state = LifecycleState.CREATED`` and
          ``self._initialization_error: Optional[str] = None`` in
          their ``__init__``.
        - implement ``_do_initialize() -> bool`` (called once on the
          CREATED→INITIALIZING transition; return ``True`` on success,
          ``False`` to abort).
        - implement ``_do_shutdown() -> None`` (called once on the
          READY/RUNNING→STOPPING transition; must be safe to call when
          partially initialized).

    The mixin handles:
        - idempotency of ``initialize`` and ``shutdown``
        - the state machine
        - a thread-unsafe lock-free guard (single-threaded use is the
          common case; subsystems that need concurrency add their own
          ``threading.RLock`` like :class:`ContextService` does).
    """

    # subclasses must initialize these in __init__:
    _lifecycle_state: LifecycleState
    _initialization_error: Any  # Optional[str]

    # ========================================================== initialize
    def initialize(self) -> bool:
        """Idempotent subsystem initialization.

        Returns ``True`` on success, ``False`` if the subsystem is in
        the middle of initializing or has already been shut down.  The
        engine treats ``False`` as a *non-fatal* warning (R-8).
        """
        if self._lifecycle_state in (LifecycleState.READY, LifecycleState.RUNNING):
            return True
        if self._lifecycle_state is LifecycleState.INITIALIZING:
            return False
        if self._lifecycle_state in (LifecycleState.STOPPING, LifecycleState.STOPPED):
            return False

        self._lifecycle_state = LifecycleState.INITIALIZING
        self._initialization_error = None
        try:
            ok = bool(self._do_initialize())
        except Exception as exc:  # noqa: BLE001 — record and recover
            self._initialization_error = repr(exc)
            self._lifecycle_state = LifecycleState.STOPPED
            return False
        if not ok:
            self._initialization_error = "initialize() returned False"
            self._lifecycle_state = LifecycleState.STOPPED
            return False
        self._lifecycle_state = LifecycleState.READY
        return True

    # ============================================================ shutdown
    def shutdown(self) -> None:
        """Idempotent subsystem shutdown.  Safe to call twice."""
        if self._lifecycle_state in (LifecycleState.STOPPED, LifecycleState.STOPPING):
            return
        self._lifecycle_state = LifecycleState.STOPPING
        try:
            self._do_shutdown()
        finally:
            self._lifecycle_state = LifecycleState.STOPPED

    # ============================================================= ready?
    @property
    def initialized(self) -> bool:
        return self._lifecycle_state.is_alive()

    @property
    def lifecycle_state(self) -> LifecycleState:
        return self._lifecycle_state

    @property
    def initialization_error(self) -> Any:
        return self._initialization_error

    # =========================================================== running
    def mark_running(self) -> None:
        if self._lifecycle_state is LifecycleState.READY:
            self._lifecycle_state = LifecycleState.RUNNING

    def mark_ready(self) -> None:
        if self._lifecycle_state is LifecycleState.RUNNING:
            self._lifecycle_state = LifecycleState.READY

    # ==================================================== subclass hooks
    def _do_initialize(self) -> bool:  # pragma: no cover - abstract
        raise NotImplementedError

    def _do_shutdown(self) -> None:  # pragma: no cover - abstract
        raise NotImplementedError
