"""
Omnix V6 — System 2 Brain natural progress narration (Phase 17).

The narration module turns task / step state into a short, natural
sentence the TTS layer can speak.  It is intentionally simple: a
small dictionary of templates, plus a per-step description the
planner already provided.

Hard rules:

  * No hardcoded app names.  The narration always uses the
    ``original_request`` or the step's ``description`` — never
    "Notepad" or "Chrome" specifically.  When the user said
    "Notepad", the narration says "Notepad" because *the user* said
    it.  We never invent an app name.
  * No shell tokens, no coordinates.
  * Idempotent — re-narrating the same state must produce the same
    sentence.

The module is pure data + a single function.  It must never
import:

    * :mod:`subprocess`
    * :mod:`pyautogui`
    * :mod:`win32gui` / :mod:`win32api`
    * :mod:`core.capability_router`
    * :mod:`core.omnix_engine`
    * :mod:`ai.provider.*`
    * any V6 *Windows service* (e.g. ``system.windows.*``)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional

from .task.models import Task, TaskStatus, StepStatus


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------


_INTRO_TEMPLATES = (
    "Working on it.",
    "On it.",
    "Let me handle that.",
)

_PLAN_TEMPLATES = (
    "Let me think about how to do that.",
    "Planning that out.",
    "Working out the steps.",
)

_READY_TEMPLATES = (
    "Ready to go.",
    "I've got a plan.",
)

_EXECUTE_TEMPLATES = (
    "Starting now.",
    "Going.",
    "On it.",
)

_WAIT_TEMPLATES = (
    "Just a moment.",
    "Waiting for that to settle.",
)

_VERIFY_TEMPLATES = (
    "Checking the result.",
    "Verifying.",
)

_RECOVER_TEMPLATES = (
    "Trying another way.",
    "Recovering.",
)

_BLOCKED_TEMPLATES = (
    "I need a bit more information to continue.",
    "I'm stuck for a moment.",
)

_NEEDS_USER_TEMPLATES = (
    "Could you clarify?",
    "Quick question.",
)


def _first_non_empty(*candidates: Optional[str]) -> str:
    for c in candidates:
        if isinstance(c, str) and c.strip():
            return c.strip()
    return ""


# ---------------------------------------------------------------------------
# Per-step description formatter
# ---------------------------------------------------------------------------


_STEP_VERB_BY_CAPABILITY_PREFIX = (
    ("desktop.application.open", "Opening"),
    ("desktop.application.close", "Closing"),
    ("desktop.application.focus", "Switching to"),
    ("desktop.keyboard.type", "Typing"),
    ("desktop.keyboard.press", "Pressing a key"),
    ("desktop.keyboard.hotkey", "Pressing a shortcut"),
    ("desktop.mouse.click", "Clicking"),
    ("desktop.mouse.move", "Moving the pointer"),
    ("desktop.mouse.scroll", "Scrolling"),
    ("desktop.window.list", "Looking at open windows"),
    ("file.read", "Reading"),
    ("file.write", "Writing"),
    ("file.delete", "Removing"),
    ("file.find", "Finding"),
    ("file.copy", "Copying"),
    ("file.move", "Moving"),
    ("directory.list", "Listing"),
    ("browser.navigate", "Navigating to"),
    ("browser.click", "Clicking"),
    ("browser.type", "Typing"),
    ("browser.extract", "Extracting text"),
    ("process.list", "Listing processes"),
    ("process.kill", "Stopping a process"),
)


def _describe_step(capability_name: str, params: Mapping[str, Any]) -> str:
    """Return a short, app-agnostic description of a step.

    The function uses the *verb* (open / type / navigate) and a
    *target* from the parameters.  The target is whatever the user
    or the planner set: the app name, the URL, the text.  We never
    invent a target.
    """
    for prefix, verb in _STEP_VERB_BY_CAPABILITY_PREFIX:
        if capability_name.startswith(prefix):
            target = _pick_target(capability_name, params)
            if target:
                return f"{verb} {target}."
            return f"{verb}."
    # Fallback: a generic description.  We never invent a verb for
    # a capability we do not recognise.
    return "Working on the next step."


def _pick_target(capability_name: str, params: Mapping[str, Any]) -> str:
    for key in (
        "app_name",
        "path",
        "url",
        "text",
        "pattern",
        "selector",
        "target_window_title",
    ):
        val = params.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


# ---------------------------------------------------------------------------
# Public function
# ---------------------------------------------------------------------------


def narrate(
    task: Task,
    *,
    stage: Optional[str] = None,
    step_index: int = -1,
) -> str:
    """Return a one-sentence narration for the current state.

    Parameters
    ----------
    task:
        The :class:`Task` to narrate.
    stage:
        Optional explicit stage name (``"task_started"``,
        ``"step_started"``, ...).  When omitted, the narration is
        derived from ``task.status``.
    step_index:
        The step index to highlight.  When ``-1`` the function
        uses :attr:`Task.current_step_index`.

    The function is **deterministic and safe to call from any
    thread**.  It never raises.
    """
    try:
        return _narrate_safe(task, stage=stage, step_index=step_index)
    except Exception:  # noqa: BLE001
        return "Working on it."


def _narrate_safe(
    task: Task,
    *,
    stage: Optional[str],
    step_index: int,
) -> str:
    if stage is None:
        stage = _stage_from_status(task.status)
    if stage == "task_completed":
        if task.kind.value == "conversational":
            return task.clarifying_question or "Done."
        return "Done."
    if stage == "task_failed":
        if task.error_message:
            return task.error_message
        return "I couldn't finish that."
    if stage == "task_cancelled":
        return "Okay, I stopped."
    if stage == "task_needs_user":
        q = task.clarifying_question.strip() if task.clarifying_question else ""
        if q:
            return q
        return "Could you clarify?"
    if stage == "task_blocked":
        return _first_non_empty(_BLOCKED_TEMPLATES) or "I'm stuck."

    # Per-stage intro / status lines.
    if stage in ("task_created", "task_started"):
        return _first_non_empty(_INTRO_TEMPLATES) or "On it."
    if stage == "task_understanding":
        return _first_non_empty(_INTRO_TEMPLATES) or "On it."
    if stage == "task_planning":
        return _first_non_empty(_PLAN_TEMPLATES) or "Planning."
    if stage == "task_ready":
        return _first_non_empty(_READY_TEMPLATES) or "Ready."
    if stage == "task_executing":
        return _first_non_empty(_EXECUTE_TEMPLATES) or "Starting now."
    if stage == "task_waiting":
        return _first_non_empty(_WAIT_TEMPLATES) or "Just a moment."
    if stage == "task_verifying":
        return _first_non_empty(_VERIFY_TEMPLATES) or "Verifying."
    if stage == "task_recovering":
        return _first_non_empty(_RECOVER_TEMPLATES) or "Recovering."

    # Per-step stages.
    if stage in (
        "step_started",
        "step_progress",
        "step_completed",
        "step_failed",
        "step_skipped",
    ):
        idx = step_index if step_index >= 0 else task.current_step_index
        if 0 <= idx < len(task.steps):
            step = task.steps[idx]
            desc = _describe_step(step.capability_name, step.params_dict())
            if stage == "step_completed":
                return f"{desc} Done with that one."
            if stage == "step_failed":
                return f"{desc} That one had a problem."
            if stage == "step_skipped":
                return f"Skipped that one."
            return desc
        return "Working on the next step."

    return _first_non_empty(_INTRO_TEMPLATES) or "On it."


def _stage_from_status(status: TaskStatus) -> str:
    return {
        TaskStatus.CREATED: "task_created",
        TaskStatus.UNDERSTANDING: "task_understanding",
        TaskStatus.PLANNING: "task_planning",
        TaskStatus.READY: "task_ready",
        TaskStatus.EXECUTING: "task_executing",
        TaskStatus.WAITING: "task_waiting",
        TaskStatus.VERIFYING: "task_verifying",
        TaskStatus.RECOVERING: "task_recovering",
        TaskStatus.BLOCKED: "task_blocked",
        TaskStatus.NEEDS_USER: "task_needs_user",
        TaskStatus.COMPLETED: "task_completed",
        TaskStatus.FAILED: "task_failed",
        TaskStatus.CANCELLED: "task_cancelled",
    }.get(status, "task_executing")


# ---------------------------------------------------------------------------
# Progress events — typed records the Brain emits
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TaskProgressEvent:
    """A single progress event the Brain emits.

    The event bus in :mod:`core.events.event_bus` consumes these.
    """

    task_id: str
    stage: str
    status: str
    step_index: int
    total_steps: int
    message: str
    timestamp: float


__all__ = ["TaskProgressEvent", "narrate"]
