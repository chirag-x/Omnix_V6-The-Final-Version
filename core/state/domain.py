"""
Omnix V6 — Domain state (TaskState, WorldState).

Per R-23 / AD-23, state is split into *typed* containers owned by
the :class:`ContextService`; this module defines the two domain
containers that change at task-execution time:

    * :class:`TaskState`   — what the engine is doing right now
    * :class:`WorldState`  — what the world looks like right now

Both are *immutable snapshots* (frozen dataclasses).  Mutation is
expressed as ``with_*`` methods returning new instances, mirroring
:class:`ActionResult` in :mod:`core.results`.

Why snapshots, not live objects:
    - The recovery layer must be able to *compare* the world before
      and after an action.  A live object can be mutated in place,
      losing the before-image.
    - Logs and audit trails need a stable, JSON-serializable view.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Dict, Optional, Tuple

from ..results import TaskStatus


# ---------------------------------------------------------------------------
# TaskState
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TaskState:
    """A snapshot of the engine's current task.

    One instance is created when a task begins and updated by the
    executor via ``with_*`` methods.  The :class:`ContextService`
    stores the *latest* snapshot and a bounded history of past
    snapshots for recovery.
    """

    task_id: str
    status: TaskStatus
    goal: str = ""
    current_step: int = 0
    total_steps: int = 0
    plan: Tuple[Any, ...] = ()
    pending_capabilities: Tuple[str, ...] = ()
    completed_capabilities: Tuple[str, ...] = ()
    failed_capability: Optional[str] = None
    last_error: Optional[str] = None
    started_at: Optional[float] = None
    updated_at: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------ derived
    @property
    def progress(self) -> float:
        """0.0–1.0 fraction of the plan that has been completed."""
        if self.total_steps <= 0:
            return 0.0
        return min(1.0, max(0.0, self.current_step / self.total_steps))

    @property
    def is_terminal(self) -> bool:
        return self.status in (
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        )

    # ------------------------------------------------------ with_* updates
    def with_status(self, status: TaskStatus) -> "TaskState":
        return replace(self, status=status)

    def with_step(self, *, current: int, total: Optional[int] = None) -> "TaskState":
        return replace(
            self,
            current_step=current,
            total_steps=total if total is not None else self.total_steps,
        )

    def with_capability_done(self, capability_name: str) -> "TaskState":
        return replace(
            self,
            completed_capabilities=tuple([*self.completed_capabilities, capability_name]),
            pending_capabilities=tuple(
                c for c in self.pending_capabilities if c != capability_name
            ),
        )

    def with_capability_failed(self, capability_name: str, error: str) -> "TaskState":
        return replace(
            self,
            failed_capability=capability_name,
            last_error=error,
            status=TaskStatus.FAILED,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": "TaskState",
            "task_id": self.task_id,
            "status": self.status.value,
            "goal": self.goal,
            "current_step": self.current_step,
            "total_steps": self.total_steps,
            "progress": self.progress,
            "plan": list(self.plan),
            "pending_capabilities": list(self.pending_capabilities),
            "completed_capabilities": list(self.completed_capabilities),
            "failed_capability": self.failed_capability,
            "last_error": self.last_error,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "metadata": dict(self.metadata),
        }


# ---------------------------------------------------------------------------
# WindowState — sub-record inside WorldState
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class WindowState:
    """The focused / targeted window from the OS's perspective."""

    title: str = ""
    process: str = ""
    hwnd: Optional[int] = None
    pid: Optional[int] = None
    bounds: Optional[Tuple[int, int, int, int]] = None  # (x, y, w, h)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "title": self.title,
            "process": self.process,
            "hwnd": self.hwnd,
            "pid": self.pid,
            "bounds": list(self.bounds) if self.bounds is not None else None,
        }


# ---------------------------------------------------------------------------
# WorldState
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class WorldState:
    """A snapshot of the OS / desktop the engine is interacting with.

    This is intentionally *minimal* in Phase 1: the values that are
    cheap to obtain without running any ML model.  Vision-derived
    fields (e.g. ``ui_elements``) will be added in Phase 2 once the
    vision subsystem is wired.
    """

    timestamp: float
    window: WindowState = field(default_factory=WindowState)
    active_application: str = ""
    clipboard_text: str = ""
    cursor_position: Tuple[int, int] = (0, 0)
    monitors: Tuple[Dict[str, Any], ...] = ()
    metadata: Dict[str, Any] = field(default_factory=dict)

    # ----------------------------------------------------- with_* updates
    def with_window(self, window: WindowState) -> "WorldState":
        return replace(self, window=window)

    def with_active_application(self, name: str) -> "WorldState":
        return replace(self, active_application=name)

    def with_clipboard(self, text: str) -> "WorldState":
        return replace(self, clipboard_text=text)

    def with_cursor(self, x: int, y: int) -> "WorldState":
        return replace(self, cursor_position=(x, y))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": "WorldState",
            "timestamp": self.timestamp,
            "window": self.window.to_dict(),
            "active_application": self.active_application,
            "clipboard_text": self.clipboard_text,
            "cursor_position": list(self.cursor_position),
            "monitors": [dict(m) for m in self.monitors],
            "metadata": dict(self.metadata),
        }
