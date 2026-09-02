"""
Omnix V6 — ContextService (R-23 / AD-10).

A *thin* coordinator over the five typed context containers:

    1. :class:`TaskState`             — :mod:`core.state.domain`
    2. :class:`WorldState`            — :mod:`core.state.domain`
    3. :class:`ConversationContext`   — :mod:`core.state.contexts`
    4. :class:`EntityContext`         — :mod:`core.state.contexts`
    5. :class:`UserContext`           — :mod:`core.state.contexts`

The service is intentionally a *façade*, not a god-object.  Each
container is a frozen dataclass; the service just owns the references,
makes atomic swap-on-update easy, and exposes a single ``to_dict``
for logging.

It is **not**:
    - a mutable global
    - a place to add ad-hoc business logic
    - a place to hold capability / subsystem state (those live in
      the :class:`ServiceRegistry`)

R-9: implements the same lifecycle shape as every other subsystem.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

from .contexts import ConversationContext, ConversationTurn, EntityContext, UserContext
from .domain import TaskState, WorldState, WindowState
from ..results import TaskStatus
from ..lifecycle import LifecycleMixin, LifecycleState


# ---------------------------------------------------------------------------
# Default factories
# ---------------------------------------------------------------------------

def _default_task_state() -> TaskState:
    return TaskState(
        task_id="",
        status=TaskStatus.CREATED,
        goal="",
    )


def _default_world_state() -> WorldState:
    return WorldState(timestamp=time.time(), window=WindowState())


def _default_conversation() -> ConversationContext:
    return ConversationContext(session_id="default")


def _default_user() -> UserContext:
    return UserContext(user_id="default")


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

@dataclass
class ContextService(LifecycleMixin):
    """Owns the five typed context containers.

    Thread-safety: updates are guarded by a single ``RLock`` so the
    executor and the recovery layer can read/write concurrently.
    """

    # ---- state ----------------------------------------------------------
    _task: TaskState
    _world: WorldState
    _conversation: ConversationContext
    _entities: EntityContext
    _user: UserContext
    _lock: threading.RLock

    # ---- lifecycle ------------------------------------------------------
    _lifecycle_state: LifecycleState
    _initialization_error: Optional[str]

    # ------------------------------------------------------------ factory
    @classmethod
    def create(cls) -> "ContextService":
        return cls(
            _task=_default_task_state(),
            _world=_default_world_state(),
            _conversation=_default_conversation(),
            _entities=EntityContext(),
            _user=_default_user(),
            _lock=threading.RLock(),
            _lifecycle_state=LifecycleState.CREATED,
            _initialization_error=None,
        )

    # -------------------------------------------------------- initialize
    def initialize(self) -> bool:
        """Idempotent.  No heavy work — the containers start empty."""
        with self._lock:
            if self._lifecycle_state in (LifecycleState.READY, LifecycleState.RUNNING):
                return True
            if self._lifecycle_state is LifecycleState.INITIALIZING:
                return False
            self._lifecycle_state = LifecycleState.INITIALIZING
        # no actual work; just stamp "ready"
        with self._lock:
            self._lifecycle_state = LifecycleState.READY
        return True

    # ---------------------------------------------------------- shutdown
    def shutdown(self) -> None:
        with self._lock:
            if self._lifecycle_state in (LifecycleState.STOPPED, LifecycleState.STOPPING):
                return
            self._lifecycle_state = LifecycleState.STOPPING
        # nothing to flush
        with self._lock:
            self._lifecycle_state = LifecycleState.STOPPED

    # ----------------------------------------------------- lifecycle bit
    @property
    def initialized(self) -> bool:
        return self._lifecycle_state in (LifecycleState.READY, LifecycleState.RUNNING)

    @property
    def lifecycle_state(self) -> LifecycleState:
        return self._lifecycle_state

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "type": "ContextService",
                "lifecycle": self._lifecycle_state.value,
                "task_id": self._task.task_id,
                "task_status": self._task.status.value,
                "conversation_turns": len(self._conversation.turns),
                "entity_count": len(self._entities.entities),
                "world_timestamp": self._world.timestamp,
                "active_application": self._world.active_application,
            }

    def __repr__(self) -> str:
        return (
            f"ContextService(state={self._lifecycle_state.value}, "
            f"task={self._task.status.value}, "
            f"entities={len(self._entities.entities)})"
        )

    # ============================================================ reads
    def task(self) -> TaskState:
        with self._lock:
            return self._task

    def world(self) -> WorldState:
        with self._lock:
            return self._world

    def conversation(self) -> ConversationContext:
        with self._lock:
            return self._conversation

    def entities(self) -> EntityContext:
        with self._lock:
            return self._entities

    def user(self) -> UserContext:
        with self._lock:
            return self._user

    def snapshot(self) -> Dict[str, Any]:
        """Return a JSON-safe snapshot of all five containers."""
        with self._lock:
            return {
                "task": self._task.to_dict(),
                "world": self._world.to_dict(),
                "conversation": self._conversation.to_dict(),
                "entities": self._entities.to_dict(),
                "user": self._user.to_dict(),
            }

    # =========================================================== writes
    def update_task(self, task: TaskState) -> None:
        with self._lock:
            self._task = task

    def update_world(self, world: WorldState) -> None:
        with self._lock:
            self._world = world

    def update_conversation(self, conversation: ConversationContext) -> None:
        with self._lock:
            self._conversation = conversation

    def update_entities(self, entities: EntityContext) -> None:
        with self._lock:
            self._entities = entities

    def update_user(self, user: UserContext) -> None:
        with self._lock:
            self._user = user

    # ============================================== batched / atomic ops
    def begin_task(self, task_id: str, goal: str) -> TaskState:
        ts = TaskState(
            task_id=task_id,
            status=TaskStatus.READY,
            goal=goal,
            started_at=time.time(),
            updated_at=time.time(),
        )
        self.update_task(ts)
        return ts

    def finish_task(self, status: TaskStatus) -> TaskState:
        with self._lock:
            ts = self._task.with_status(status).with_step(
                current=self._task.total_steps or self._task.current_step,
            )
            self._task = replace_done(ts, time.time())
        return self._task

    def append_conversation_turn(self, role: str, content: str) -> ConversationContext:
        with self._lock:
            conv = self._conversation.append_turn(
                ConversationTurn(role=role, content=content, timestamp=time.time())
            )
            self._conversation = conv
        return conv

    def set_active_application(self, name: str) -> None:
        with self._lock:
            self._world = self._world.with_active_application(name)

    def set_window(self, window: WindowState) -> None:
        with self._lock:
            self._world = self._world.with_window(window)

    def set_intent(self, intent: str, confidence: float = 0.0) -> None:
        with self._lock:
            self._conversation = self._conversation.with_intent(intent, confidence)


# ---------------------------------------------------------------------------
# Helpers (kept out of the class to keep the class body small)
# ---------------------------------------------------------------------------

def replace_done(ts: TaskState, finished_at: float) -> TaskState:
    """Internal: stamp ``updated_at`` and ``finished_at`` on a task."""
    from dataclasses import replace as _r
    return _r(ts, updated_at=finished_at)
