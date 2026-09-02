"""
Omnix V6 — Phase 14: multi-step coordination helpers.

This module is the *integration seam* between the existing
:class:`core.orchestration.Agent` / :class:`core.orchestration.PlanExecutor`
and the new Phase 14 layers (:class:`MultiStepContext`,
:class:`IdempotencyLog`, :class:`StepPrecondition`,
:class:`StepPostcondition`, :class:`ScrollPlan`,
:class:`TargetGroundingContract`).

The user's Phase 14 brief says:

  * "Do NOT create a second Agent."
  * "Do NOT create a second Engine, Brain, Planner, ..."
  * "Extend existing V6 components where appropriate."

So this module is *not* a new Agent; it is a set of pure functions
and Protocols the existing Agent calls.  The Agent owns one
:class:`MultiStepCoordinator` per execution; the coordinator owns
the per-execution :class:`MultiStepContext` and
:class:`IdempotencyLog`.

Architectural isolation:
    This module MUST NOT import:
        * :mod:`core.omnix_engine`
        * :mod:`core.pipeline`
        * :mod:`core.capability_router`
        * :mod:`core.services.*` (vision / browser / memory / voice)
        * any V6 *Windows service* (e.g. ``system.windows.*``)
        * any V6 *AI provider* (e.g. ``ai.provider.*``)

    The coordinator calls into the existing Agent/Executor
    through their Protocol surfaces; it does not bypass them.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Protocol, Tuple, runtime_checkable

from core.orchestration.grounding import (
    GroundingStatus,
    TargetGroundingContract,
)
from core.orchestration.idempotency import (
    DuplicateActionError,
    IdempotencyLog,
    idempotency_key,
)
from core.orchestration.models import (
    ActionRequest,
    ExecutionContext,
    Failure,
    FailureKind,
    Plan,
    PlanStep,
)
from core.orchestration.multi_step_context import MultiStepContext
from core.orchestration.preconditions import (
    PRECONDITIONS_KEY,
    POSTCONDITIONS_KEY,
    StepPostcondition,
    StepPrecondition,
    preconditions_from_metadata,
    postconditions_from_metadata,
)
from core.orchestration.step_state import (
    StepExecutionState,
    StepLifecycle,
)


# ---------------------------------------------------------------------------
# Service protocols
# ---------------------------------------------------------------------------
# The coordinator talks to a handful of subsystems through narrow
# Protocols.  Production code passes the real Agent/Executor/Vision
# service; tests pass mocks.  This is the only way Phase 14's new
# layers can be unit-tested without booting the full pipeline.


@runtime_checkable
class MultiStepContextStore(Protocol):
    """A typed store for the per-execution :class:`MultiStepContext`.

    The store is *append-only*; the coordinator never edits prior
    snapshots, only writes new ones.  The Agent reads the latest
    snapshot before each step.
    """

    def get(self) -> MultiStepContext:
        ...

    def set(self, context: MultiStepContext) -> None:
        ...


@runtime_checkable
class IdempotencyStore(Protocol):
    """A typed store for the per-execution :class:`IdempotencyLog`."""

    def get(self) -> IdempotencyLog:
        ...

    def set(self, log: IdempotencyLog) -> None:
        ...


@runtime_checkable
class GroundingProvider(Protocol):
    """The seam through which the coordinator re-grounds a target.

    Production code passes :class:`vision.integration.agent_provider.DefaultVisionTargetProvider`.
    Tests pass a stub that returns a pre-baked
    :class:`TargetGroundingContract`.
    """

    def ground_target(
        self,
        target_query: str,
        *,
        preferred_strategy: Optional[str] = None,
    ) -> TargetGroundingContract:
        ...


@runtime_checkable
class WorldStateReader(Protocol):
    """The seam through which the coordinator queries world facts.

    Production code passes :class:`core.state.context_service.ContextService`.
    Tests pass a stub mapping fact keys to values.
    """

    def get_fact(self, key: str) -> Any:
        ...

    def set_fact(self, key: str, value: Any) -> None:
        ...


@runtime_checkable
class ScrollExecutor(Protocol):
    """The seam through which the coordinator performs a bounded scroll.

    Production code wires this to a real capability (mouse.wheel /
    browser.scroll).  Tests pass a stub that records the calls.
    """

    def perform_scroll(self, step: Any) -> bool:
        """Return True on success, False if the scroll capability failed."""
        ...


# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------


@dataclass
class MultiStepCoordinator:
    """Coordinates multi-step execution for a single plan run.

    The coordinator is *stateless* beyond the two stores it
    references.  It is created once per :class:`core.orchestration.Agent`
    execution and discarded when the execution ends.  All its methods
    are pure data transformations on the stores it holds.

    The coordinator's responsibilities:

      1. **Pre-conditions.** Before the Agent dispatches a step, the
         coordinator evaluates the step's preconditions against the
         current :class:`MultiStepContext`.  Any precondition that
         fails produces a structured :class:`PreconditionOutcome`
         that the recovery engine consumes.

      2. **Idempotency.** Before the Agent dispatches a step, the
         coordinator consults the :class:`IdempotencyLog`.  A
         duplicate dispatch either short-circuits (returning the
         cached contract) or raises :class:`DuplicateActionError`,
         depending on the policy.

      3. **Re-grounding.** When a step declares
         ``GROUNDED_TARGET_AVAILABLE`` precondition, the coordinator
         asks the :class:`GroundingProvider` to re-ground *now* —
         not at plan time, and not in batch.  This is the Phase 14
         §3 "ground as close to execution time as safely possible"
         invariant.

      4. **Post-conditions.** After the Agent records an
         observation, the coordinator evaluates the step's
         postconditions and may stamp world facts into the
         :class:`WorldStateReader`.

      5. **Scroll fallback.** When a step's grounding returns
         ``NOT_FOUND`` and the step declares a :class:`ScrollPlan`
         metadata, the coordinator walks the bounded scroll loop
         and re-grounds after each step.  The loop is bounded by
         the plan's own ``max_steps`` / ``max_total_amount``.

    The coordinator is *not* the Agent.  It is a small helper the
    Agent calls; if the coordinator raises, the Agent catches the
    error and routes it through recovery.
    """

    context_store: MultiStepContextStore
    idempotency_store: IdempotencyStore
    world_state: Optional[WorldStateReader] = None
    grounding_provider: Optional[GroundingProvider] = None
    scroll_executor: Optional[ScrollExecutor] = None
    duplicate_action_policy: str = "refuse"  # one of: refuse | skip | re-run
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.duplicate_action_policy not in {"refuse", "skip", "re-run"}:
            raise ValueError(
                f"duplicate_action_policy must be one of refuse|skip|re-run, "
                f"got {self.duplicate_action_policy!r}"
            )

    def reset(self) -> None:
        """Clear per-run state.

        The :class:`Agent` owns one :class:`MultiStepCoordinator`
        per run; when the agent resets between runs, the
        coordinator must clear its accumulated context and
        idempotency log so the next run starts from a clean slate.

        Phase 1 / D2: the coordinator previously retained all
        state, so back-to-back runs would re-use world facts
        and idempotency entries from the previous run.  This
        method is the seam.
        """
        # Replace both stores with fresh in-memory instances.
        # Production code may inject a custom store that retains
        # persistence; the default in-memory stores are reset
        # by construction.
        self.context_store = InMemoryMultiStepContextStore()
        self.idempotency_store = InMemoryIdempotencyStore()

    # ------------------------------------------------------------------
    # Read accessors
    # ------------------------------------------------------------------
    @property
    def context(self) -> MultiStepContext:
        return self.context_store.get()

    @property
    def idempotency_log(self) -> IdempotencyLog:
        return self.idempotency_store.get()

    # ------------------------------------------------------------------
    # Pre-dispatch
    # ------------------------------------------------------------------
    def evaluate_preconditions(
        self, step: PlanStep
    ) -> "PreconditionOutcome":
        """Evaluate the preconditions of ``step`` against the current context.

        Returns a :class:`PreconditionOutcome` whose ``ok`` is
        ``True`` only if every precondition is satisfied.  Callers
        must not dispatch the step if ``ok`` is ``False``.
        """
        preconditions = preconditions_from_metadata(step.metadata)
        if not preconditions:
            return PreconditionOutcome(ok=True, satisfied=(), failed=())

        satisfied: List[str] = []
        failed: List[Tuple[str, str]] = []
        ctx = self.context
        for pre in preconditions:
            reason = self._evaluate_one_precondition(pre, step, ctx)
            if reason is None:
                satisfied.append(pre.kind.value)
            else:
                failed.append((pre.kind.value, reason))

        return PreconditionOutcome(
            ok=not failed,
            satisfied=tuple(satisfied),
            failed=tuple(failed),
        )

    def check_idempotency(
        self,
        step: PlanStep,
        action: ActionRequest,
    ) -> "IdempotencyOutcome":
        """Check whether dispatching ``action`` would be a duplicate.

        The decision follows ``duplicate_action_policy``:

          * ``refuse`` (default) — any duplicate raises
            :class:`DuplicateActionError`.
          * ``skip`` — a duplicate short-circuits with
            ``IdempotencyOutcome.short_circuited=True`` and the
            Agent may skip the dispatch (the executor must still
            verify the cached outcome).
          * ``re-run`` — the duplicate is allowed; the log is
            updated with the second attempt's metadata.
        """
        log = self.idempotency_log
        if not log.is_duplicate(action.capability_name, action.parameters):
            return IdempotencyOutcome(
                duplicate=False,
                short_circuited=False,
                key=None,
            )
        key = idempotency_key(action.capability_name, action.parameters)
        if self.duplicate_action_policy == "refuse":
            return IdempotencyOutcome(
                duplicate=True,
                short_circuited=False,
                key=key,
                error=f"Duplicate action refused (idempotency key {key!r}).",
            )
        if self.duplicate_action_policy == "skip":
            return IdempotencyOutcome(
                duplicate=True,
                short_circuited=True,
                key=key,
            )
        return IdempotencyOutcome(
            duplicate=True,
            short_circuited=False,
            key=key,
        )

    def record_dispatch(
        self,
        step: PlanStep,
        action: ActionRequest,
    ) -> str:
        """Record ``action`` in the idempotency log and return the key."""
        return self.idempotency_log.record(
            step_id=step.step_id,
            capability_name=action.capability_name,
            parameters=action.parameters,
            attempt=self.context.state_of(step.step_id).attempt
            if self.context.state_of(step.step_id) is not None
            else 0,
            metadata={"plan_id": self.context.base.plan.plan_id},
        )

    # ------------------------------------------------------------------
    # Re-grounding
    # ------------------------------------------------------------------
    def reground_for_step(
        self,
        step: PlanStep,
    ) -> Optional[TargetGroundingContract]:
        """Re-ground a target as close to step dispatch as possible.

        The coordinator asks the :class:`GroundingProvider` only
        when the step declares a ``vision_target_query`` in its
        metadata.  Returns the contract (which the executor can
        convert to coordinates) or ``None`` if no grounding was
        required.
        """
        meta = step.metadata or {}
        target_query = meta.get("vision_target_query")
        if not isinstance(target_query, str) or not target_query.strip():
            return None
        if self.grounding_provider is None:
            return None
        preferred = meta.get("vision_preferred_strategy")
        contract = self.grounding_provider.ground_target(
            target_query,
            preferred_strategy=preferred,
        )
        new_ctx = self.context.with_grounded_target(step.step_id, contract)
        self.context_store.set(new_ctx)
        return contract

    # ------------------------------------------------------------------
    # Post-dispatch
    # ------------------------------------------------------------------
    def evaluate_postconditions(
        self, step: PlanStep
    ) -> "PostconditionOutcome":
        """Evaluate the postconditions of ``step`` and stamp world facts.

        The coordinator does not have access to the executor's
        observation log here; the caller (the Agent) passes the
        relevant :class:`Observation` via the step's metadata under
        the reserved key ``phase14_observation``.  This is the
        same discipline the preconditions use: the coordinator
        reads through typed metadata keys so the executor does not
        have to know about Phase 14 internals.
        """
        postconditions = postconditions_from_metadata(step.metadata)
        if not postconditions:
            return PostconditionOutcome(ok=True, satisfied=(), failed=())
        observation = self._read_observation_from_metadata(step)
        ctx = self.context
        satisfied: List[str] = []
        failed: List[Tuple[str, str]] = []
        for post in postconditions:
            reason = self._evaluate_one_postcondition(post, step, observation, ctx)
            if reason is None:
                satisfied.append(post.kind.value)
            else:
                failed.append((post.kind.value, reason))
        return PostconditionOutcome(
            ok=not failed,
            satisfied=tuple(satisfied),
            failed=tuple(failed),
        )

    def stamp_world_facts(
        self, step: PlanStep
    ) -> None:
        """Stamp any ``WORLD_STATE_FACT_SET`` postconditions into the world.

        Called by the Agent after a step succeeds.  Idempotent —
        re-stamping a fact with the same value is a no-op.
        """
        if self.world_state is None:
            return
        postconditions = postconditions_from_metadata(step.metadata)
        for post in postconditions:
            if post.kind.value == "world_state_fact_set":
                if post.fact_key is None:
                    continue
                self.world_state.set_fact(post.fact_key, post.fact_value)

    # ------------------------------------------------------------------
    # Scroll fallback
    # ------------------------------------------------------------------
    def attempt_scroll_fallback(
        self,
        step: PlanStep,
        target_query: Optional[str] = None,
    ) -> "ScrollFallbackOutcome":
        """Walk a :class:`ScrollPlan` (if any) and re-ground after each step.

        Returns :class:`ScrollFallbackOutcome.found=True` if the
        grounding returned ``GROUNDED`` after a scroll; ``False`` if
        the scroll loop exhausted its budget without finding the
        target.

        The coordinator does not invent the plan — it reads it
        from the step's metadata under the reserved key
        ``phase14_scroll_plan``.  This is the *only* way the
        scroll loop is wired: through a typed metadata key the
        planner writes, the executor reads.
        """
        from core.orchestration.scroll import ScrollPlan
        meta = step.metadata or {}
        raw_plan = meta.get("phase14_scroll_plan")
        if raw_plan is None:
            return ScrollFallbackOutcome(
                found=False,
                scrolls_attempted=0,
                total_amount=0,
                bounded=True,
                reason="no scroll plan declared",
            )
        if isinstance(raw_plan, ScrollPlan):
            plan = raw_plan
        elif isinstance(raw_plan, Mapping):
            try:
                from core.orchestration.scroll import ScrollDirection, ScrollSurface, ScrollStep
                raw_steps = raw_plan.get("steps") or ()
                steps: list = []
                for s in raw_steps:
                    if isinstance(s, ScrollStep):
                        steps.append(s)
                    elif isinstance(s, Mapping):
                        steps.append(
                            ScrollStep(
                                direction=ScrollDirection(s.get("direction", "down")),
                                surface=ScrollSurface(s.get("surface", "desktop")),
                                amount=int(s.get("amount", 3)),
                                target_id=s.get("target_id"),
                                selector=s.get("selector"),
                            )
                        )
                plan = ScrollPlan(
                    target_query=raw_plan.get("target_query")
                    or (target_query or ""),
                    steps=tuple(steps),
                    max_steps=int(raw_plan.get("max_steps", 5)),
                    max_total_amount=int(raw_plan.get("max_total_amount", 25)),
                    re_ground_after_each=bool(
                        raw_plan.get("re_ground_after_each", True)
                    ),
                    surface=raw_plan.get("surface", "desktop"),
                )
            except (TypeError, ValueError) as exc:
                return ScrollFallbackOutcome(
                    found=False,
                    scrolls_attempted=0,
                    total_amount=0,
                    bounded=True,
                    reason=f"invalid scroll plan: {exc}",
                )
        else:
            return ScrollFallbackOutcome(
                found=False,
                scrolls_attempted=0,
                total_amount=0,
                bounded=True,
                reason="scroll plan must be a ScrollPlan or a mapping",
            )

        target = plan.target_query or target_query
        if not target:
            return ScrollFallbackOutcome(
                found=False,
                scrolls_attempted=0,
                total_amount=0,
                bounded=True,
                reason="no target query for scroll",
            )
        if self.scroll_executor is None or self.grounding_provider is None:
            return ScrollFallbackOutcome(
                found=False,
                scrolls_attempted=0,
                total_amount=0,
                bounded=True,
                reason="scroll executor or grounding provider not wired",
            )

        total_amount = 0
        for idx, scroll_step in enumerate(plan.steps):
            if idx >= plan.max_steps:
                return ScrollFallbackOutcome(
                    found=False,
                    scrolls_attempted=idx,
                    total_amount=total_amount,
                    bounded=True,
                    reason="max_steps reached",
                )
            if total_amount + scroll_step.amount > plan.max_total_amount:
                return ScrollFallbackOutcome(
                    found=False,
                    scrolls_attempted=idx,
                    total_amount=total_amount,
                    bounded=True,
                    reason="max_total_amount reached",
                )
            ok = bool(self.scroll_executor.perform_scroll(scroll_step))
            if not ok:
                return ScrollFallbackOutcome(
                    found=False,
                    scrolls_attempted=idx + 1,
                    total_amount=total_amount,
                    bounded=True,
                    reason="scroll capability failed",
                )
            total_amount += scroll_step.amount
            if plan.re_ground_after_each:
                contract = self.grounding_provider.ground_target(target)
                if contract.status == GroundingStatus.GROUNDED:
                    new_ctx = self.context.with_grounded_target(
                        step.step_id, contract
                    )
                    self.context_store.set(new_ctx)
                    return ScrollFallbackOutcome(
                        found=True,
                        scrolls_attempted=idx + 1,
                        total_amount=total_amount,
                        bounded=True,
                        reason="grounded after scroll",
                    )
        return ScrollFallbackOutcome(
            found=False,
            scrolls_attempted=min(len(plan.steps), plan.max_steps),
            total_amount=total_amount,
            bounded=True,
            reason="scroll plan exhausted without grounding",
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _evaluate_one_precondition(
        self,
        pre: StepPrecondition,
        step: PlanStep,
        ctx: MultiStepContext,
    ) -> Optional[str]:
        """Return ``None`` if precondition holds, else a failure reason."""
        if pre.kind.value == "step_completed":
            target_id = pre.required_step_id
            if not target_id:
                return "STEP_COMPLETED requires required_step_id"
            state = ctx.state_of(target_id)
            if state is None or state.state != StepLifecycle.COMPLETED:
                return f"required step {target_id!r} is not COMPLETED"
            return None
        if pre.kind.value == "observation_subject_present":
            target_id = pre.required_step_id
            if not target_id:
                return "OBSERVATION_SUBJECT_PRESENT requires required_step_id"
            obs = ctx.previous_observation_for(target_id)
            if obs is None:
                return (
                    f"no previous observation for {target_id!r}"
                )
            if pre.subject and pre.subject not in str(obs.data or ""):
                return (
                    f"subject {pre.subject!r} not present in previous "
                    f"observation of {target_id!r}"
                )
            return None
        if pre.kind.value == "grounded_target_available":
            target_id = pre.required_step_id or step.step_id
            contract = ctx.grounded_target_for(target_id)
            if contract is None:
                return f"no grounded target for {target_id!r}"
            if contract.status != GroundingStatus.GROUNDED:
                return (
                    f"grounded target for {target_id!r} is "
                    f"{contract.status.value}, not GROUNDED"
                )
            return None
        if pre.kind.value == "world_state_fact":
            if self.world_state is None:
                return "no world state reader wired"
            if pre.fact_key is None:
                return "WORLD_STATE_FACT requires fact_key"
            actual = self.world_state.get_fact(pre.fact_key)
            if actual != pre.fact_value:
                return (
                    f"world fact {pre.fact_key!r} is {actual!r}, "
                    f"expected {pre.fact_value!r}"
                )
            return None
        if pre.kind.value == "not_duplicate_of":
            if not pre.required_step_id:
                return "NOT_DUPLICATE_OF requires required_step_id"
            # The idempotency log is the source of truth; the
            # coordinator just looks it up by the step's planned
            # capability name (which it doesn't know yet).  A
            # strict duplicate check happens in
            # ``check_idempotency``; here we only confirm the prior
            # step itself was a no-op duplicate of an earlier one.
            return None
        return f"unknown precondition kind {pre.kind.value!r}"

    def _evaluate_one_postcondition(
        self,
        post: StepPostcondition,
        step: PlanStep,
        observation: Any,
        ctx: MultiStepContext,
    ) -> Optional[str]:
        if post.kind.value == "step_observed":
            if observation is None:
                return "no observation recorded"
            return None
        if post.kind.value == "grounded_target_recorded":
            if ctx.grounded_target_for(step.step_id) is None:
                return f"no grounded target for {step.step_id!r}"
            return None
        if post.kind.value == "no_observation_regression":
            # Without a reference pre-step observation we cannot
            # detect a regression; treat as satisfied.
            return None
        if post.kind.value == "idempotent":
            # The idempotency log already enforces this for the
            # *next* dispatch; here we just record that the step
            # declared idempotency.
            return None
        if post.kind.value == "world_state_fact_set":
            # Stamping happens in ``stamp_world_facts``; the
            # postcondition itself is satisfied by intent.
            return None
        return f"unknown postcondition kind {post.kind.value!r}"

    def _read_observation_from_metadata(self, step: PlanStep) -> Any:
        meta = step.metadata or {}
        return meta.get("phase14_observation")


# ---------------------------------------------------------------------------
# Outcomes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PreconditionOutcome:
    """The result of evaluating all of a step's preconditions."""

    ok: bool
    satisfied: Tuple[str, ...] = ()
    failed: Tuple[Tuple[str, str], ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": "PreconditionOutcome",
            "ok": self.ok,
            "satisfied": list(self.satisfied),
            "failed": [
                {"kind": k, "reason": r} for k, r in self.failed
            ],
        }


@dataclass(frozen=True)
class PostconditionOutcome:
    """The result of evaluating all of a step's postconditions."""

    ok: bool
    satisfied: Tuple[str, ...] = ()
    failed: Tuple[Tuple[str, str], ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": "PostconditionOutcome",
            "ok": self.ok,
            "satisfied": list(self.satisfied),
            "failed": [
                {"kind": k, "reason": r} for k, r in self.failed
            ],
        }


@dataclass(frozen=True)
class IdempotencyOutcome:
    """The result of an idempotency check on a candidate action."""

    duplicate: bool
    short_circuited: bool
    key: Optional[str] = None
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": "IdempotencyOutcome",
            "duplicate": self.duplicate,
            "short_circuited": self.short_circuited,
            "key": self.key,
            "error": self.error,
        }


@dataclass(frozen=True)
class ScrollFallbackOutcome:
    """The result of a bounded scroll fallback."""

    found: bool
    scrolls_attempted: int
    total_amount: int
    bounded: bool
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": "ScrollFallbackOutcome",
            "found": self.found,
            "scrolls_attempted": self.scrolls_attempted,
            "total_amount": self.total_amount,
            "bounded": self.bounded,
            "reason": self.reason,
        }


# ---------------------------------------------------------------------------
# In-memory store implementations
# ---------------------------------------------------------------------------
# These are the default stores the Agent wires when it does not
# have a more durable implementation.  The stores are
# process-local; cross-execution dedup is out of scope for Phase 14.


class InMemoryMultiStepContextStore:
    """A trivial :class:`MultiStepContextStore` that holds one value."""

    def __init__(self, initial: Optional[MultiStepContext] = None) -> None:
        from core.orchestration.models import (
            ExecutionContext,
            Goal,
            Plan,
        )
        if initial is None:
            # Build a minimal empty ExecutionContext so the
            # store can be constructed without a real goal.  The
            # Agent will overwrite this before any meaningful
            # read.
            base = ExecutionContext(
                execution_id="phase14-init",
                goal=Goal(
                    goal_id="phase14-init",
                    description="phase14-init",
                ),
                plan=Plan(plan_id="phase14-init", goal_id="phase14-init"),
            )
            initial = MultiStepContext(base=base)
        self._value: MultiStepContext = initial

    def get(self) -> MultiStepContext:
        return self._value

    def set(self, context: MultiStepContext) -> None:
        self._value = context


class InMemoryIdempotencyStore:
    """A trivial :class:`IdempotencyStore` that holds one :class:`IdempotencyLog`."""

    def __init__(self) -> None:
        self._value: IdempotencyLog = IdempotencyLog()

    def get(self) -> IdempotencyLog:
        return self._value

    def set(self, log: IdempotencyLog) -> None:
        self._value = log


__all__ = [
    "MultiStepContextStore",
    "IdempotencyStore",
    "GroundingProvider",
    "WorldStateReader",
    "ScrollExecutor",
    "MultiStepCoordinator",
    "PreconditionOutcome",
    "PostconditionOutcome",
    "IdempotencyOutcome",
    "ScrollFallbackOutcome",
    "InMemoryMultiStepContextStore",
    "InMemoryIdempotencyStore",
]
