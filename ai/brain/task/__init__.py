"""
Omnix V6 — System 2 Brain structured task model (Phase 17).

Public surface:

    * :class:`Task`           — a single user-facing task
    * :class:`TaskStep`       — a single step in a task
    * :class:`TaskPriority`   — priority enum
    * :class:`TaskStatus`     — the task state machine
    * :class:`TaskKind`       — conversational | computer_use | hybrid | unknown
    * :class:`StepStatus`     — per-step state
    * :class:`TaskFactory`    — the single builder for tasks
    * :class:`LLMCallRecord`  — one LLM invocation
    * :class:`StepTrace`      — one step's run history
    * :class:`VerificationRecord` — a verification outcome

This module is **pure data**.  It never imports a Windows service, a
capability, or an LLM provider.  It is the canonical, observable
shape of "a thing the user asked us to do".
"""
from __future__ import annotations

from .models import (
    LLMCallRecord,
    StepStatus,
    StepTrace,
    Task,
    TaskFactory,
    TaskKind,
    TaskPriority,
    TaskStatus,
    VerificationRecord,
    now,
)

__all__ = [
    "LLMCallRecord",
    "StepStatus",
    "StepTrace",
    "Task",
    "TaskFactory",
    "TaskKind",
    "TaskPriority",
    "TaskStatus",
    "VerificationRecord",
    "now",
]
