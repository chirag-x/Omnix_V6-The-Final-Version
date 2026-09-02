"""
Omnix V6 — Orchestration interface contracts (Phase 4).

Defines the :class:`typing.Protocol` contracts that the four major
orchestration components must satisfy:

    * :class:`IntentInterpreter`  — text → :class:`Intent`
    * :class:`Planner`             — :class:`Goal` → :class:`Plan`
    * :class:`PlanExecutor`        — :class:`Plan` → terminal state
    * :class:`Orchestrator`        — the loop that wires the others

These are **interfaces only**.  No LLM call, no shell, no real
subsystem is invoked from this module.  Concrete implementations
land in later phases.

R-1: this module MUST NOT import :mod:`core.omnix_engine`.  The
engine is the consumer of these interfaces, not a peer of them.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Protocol, runtime_checkable

from .models import (
    ActionRequest,
    ExecutionContext,
    Failure,
    Goal,
    Intent,
    Observation,
    Plan,
    PlanStep,
    RecoveryDecision,
)


# ===========================================================================
# IntentInterpreter
# ===========================================================================

@runtime_checkable
class IntentInterpreter(Protocol):
    """Turn a user utterance (or a system trigger) into a structured :class:`Intent`.

    The interpreter never sees a shell.  It is given a string and the
    :class:`ContextService` snapshot (read-only) and returns an
    :class:`Intent`.  Concrete interpreters (regex, LLM, hybrid) live
    in later phases.
    """

    name: str

    def interpret(
        self,
        text: str,
        *,
        context_snapshot: Optional[Dict[str, Any]] = None,
    ) -> Intent:
        """Return an :class:`Intent` for ``text``.

        ``context_snapshot`` is the read-only projection the
        interpreter may consult (e.g. the prior intent, the
        referenced entities, the user's preferred language).  It
        must NOT be mutated.
        """
        ...


# ===========================================================================
# Planner
# ===========================================================================

@runtime_checkable
class Planner(Protocol):
    """Turn a :class:`Goal` (and supporting context) into a :class:`Plan`.

    The planner is the only place where the *structure* of a plan
    lives.  It receives a goal and returns a plan that the executor
    will dispatch.  The planner is forbidden from returning a step
    whose ``action`` is not in :class:`ActionKind`; the model layer
    enforces that at construction time.
    """

    name: str

    def plan(
        self,
        goal: Goal,
        *,
        intent: Optional[Intent] = None,
        context_snapshot: Optional[Dict[str, Any]] = None,
        prior_plan: Optional[Plan] = None,
        failure: Optional[Failure] = None,
    ) -> Plan:
        """Return a :class:`Plan` that aims to achieve ``goal``.

        ``prior_plan`` and ``failure`` are populated when the
        orchestrator is replanning after a step failed; the planner
        can return a fresh plan or a one-step fix.
        """
        ...


# ===========================================================================
# PlanExecutor
# ===========================================================================

@runtime_checkable
class PlanExecutor(Protocol):
    """Run a :class:`Plan` step by step, with verification and recovery.

    The executor never talks to the OS directly.  It dispatches
    :class:`ActionRequest` objects to the CapabilityRouter, calls
    :class:`Verifier` on every step's :class:`ExpectedEffect`, and
    asks a :class:`RecoveryEngine` to make a decision when a step
    fails.

    The executor is **the only** component that calls
    ``context_service.update_*``.  Everything else holds a read-only
    snapshot.
    """

    name: str

    def execute(self, context: ExecutionContext) -> ExecutionContext:
        """Run the plan in ``context`` and return a *terminal* context.

        The returned context is the final state — the executor has
        updated the ``ContextService`` as it went.  The orchestrator
        inspects the returned context to decide what to do next.
        """
        ...

    def execute_step(
        self,
        context: ExecutionContext,
        step: PlanStep,
    ) -> ExecutionContext:
        """Run a single step and return a new context snapshot.

        Exposed for testing and for replan-skip-one paths.  Does NOT
        update the global :class:`ContextService`; the caller is
        responsible for committing the snapshot.
        """
        ...


# ===========================================================================
# RecoveryEngine
# ===========================================================================

@runtime_checkable
class RecoveryEngine(Protocol):
    """Decide what to do when a step (or a whole plan) fails.

    The engine receives a :class:`Failure`, the current
    :class:`ExecutionContext`, and a small history of recent
    decisions; it returns a :class:`RecoveryDecision`.  The decision
    is data; the executor applies it.
    """

    name: str

    def decide(
        self,
        failure: Failure,
        context: ExecutionContext,
        *,
        history: Optional[List[RecoveryDecision]] = None,
    ) -> RecoveryDecision:
        """Return a :class:`RecoveryDecision` for ``failure``."""
        ...


# ===========================================================================
# Orchestrator (foundation)
# ===========================================================================

@runtime_checkable
class Orchestrator(Protocol):
    """The outer loop that wires the other interfaces together.

    The orchestrator is the only component that:
        * constructs an :class:`ExecutionContext`;
        * calls the :class:`IntentInterpreter`, the :class:`Planner`,
          the :class:`PlanExecutor`, and the :class:`RecoveryEngine`;
        * owns the high-level state transitions (PLANNING → EXECUTING
          → VERIFYING → COMPLETED / FAILED).

    This Protocol is a *foundation*, not the full engine.  It is
    here so Phase 5+ can implement it without changing the call
    surface; the full orchestrator lives in :mod:`core.omnix_engine`.
    """

    name: str

    def handle_user_input(
        self,
        text: str,
        *,
        context_snapshot: Optional[Dict[str, Any]] = None,
    ) -> ExecutionContext:
        """End-to-end: text → intent → goal → plan → executed context."""
        ...

    def step(self, context: ExecutionContext) -> ExecutionContext:
        """Run one step of the plan and return a new context.

        Used by long-running orchestrators that want to expose
        step-by-step progress (e.g. debug panels).
        """
        ...

    def replan(
        self,
        context: ExecutionContext,
        failure: Failure,
    ) -> ExecutionContext:
        """Ask the planner for a new plan and return the new context."""
        ...

    def cancel(self, context: ExecutionContext, *, reason: str = "") -> ExecutionContext:
        """Cancel the plan in ``context`` and return a terminal context."""
        ...
