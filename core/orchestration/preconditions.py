"""
Omnix V6 — Phase 14: preconditions and postconditions for ``PlanStep``.

Phase 4 / Phase 5 :class:`core.orchestration.PlanStep` carries an
:class:`ExpectedEffect` for verification, but it has no place to
declare *what must be true before* a step dispatches
(precondition) or *what world facts must hold after* a step
finishes (postcondition distinct from the verifier's effect check).

This module adds typed preconditions and postconditions without
mutating the frozen :class:`PlanStep`:

    * A precondition is a *boolean fact* the executor checks against
      the current :class:`MultiStepContext` (or, in the case of
      vision preconditions, against a vision service) before
      dispatching the step.  If the precondition is false, the step
      is *not* dispatched — it is short-circuited and the recovery
      engine gets a structured :class:`PreconditionFailed` failure.

    * A postcondition is a *boolean fact* the executor checks after
      the step has executed and been observed.  A step can be
      "executed" (the capability returned) but still violate its
      postcondition (e.g. "the file must exist after the write").
      Postcondition violations route to recovery just like verifier
      failures.

Both preconditions and postconditions are *closed types*: only the
kinds listed below may be constructed.  This is the same discipline
the closed capability set enforces on actions.

Architectural isolation:
    This module MUST NOT import:
        * :mod:`core.omnix_engine`
        * :mod:`core.pipeline`
        * :mod:`core.capability_router`
        * :mod:`core.services.*` (vision / browser / memory / voice)
        * any V6 *Windows service* (e.g. ``system.windows.*``)
        * any V6 *AI provider* (e.g. ``ai.provider.*``)

    Preconditions are *evaluated* by the executor; this module
    only defines their shape.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Mapping, Optional, Tuple


class PreconditionKind(str, Enum):
    """Closed set of preconditions a step may declare."""

    STEP_COMPLETED = "step_completed"
    # Step N may declare "step N-1 must be completed" with
    # ``required_step_id``.  The executor walks the dependency
    # graph at dispatch time and refuses to start a step whose
    # required step is not yet COMPLETED.

    OBSERVATION_SUBJECT_PRESENT = "observation_subject_present"
    # A named subject ("the file /tmp/x.txt", "the window
    # 'Notepad'") must be present in the latest observation
    # for ``required_step_id``.  This is how a step declares "I
    # need the file my previous step just created to still
    # exist" without invoking vision.

    GROUNDED_TARGET_AVAILABLE = "grounded_target_available"
    # The step requires a :class:`TargetGroundingContract` for
    # ``required_step_id``.  The executor uses this to force a
    # re-ground between dependent steps (Phase 14 §3: "do not
    # precompute coordinates for the entire plan").

    WORLD_STATE_FACT = "world_state_fact"
    # A boolean fact the executor asks the ContextService for.
    # ``fact_key`` is a string the ContextService knows; the
    # service decides how to evaluate it.  Keeps the executor
    # decoupled from the actual world.

    NOT_DUPLICATE_OF = "not_duplicate_of"
    # The step must not re-execute an action that was already
    # performed in this plan run.  ``required_step_id`` is the
    # step whose action is the reference; the executor uses the
    # idempotency log to decide.  This is the Phase 14 §25
    # "idempotency / duplicate action protection" mechanism.


class PostconditionKind(str, Enum):
    """Closed set of postconditions a step may declare."""

    STEP_OBSERVED = "step_observed"
    # The step's post-action observation must have been captured
    # and the verifier must have returned ``passed`` or
    # ``uncertain``.  This is the default for steps that carry an
    # :class:`ExpectedEffect`; declaring it explicitly is optional.

    WORLD_STATE_FACT_SET = "world_state_fact_set"
    # The executor must record ``fact_key=fact_value`` in the
    # ContextService after the step finishes.  The fact becomes
    # available to subsequent steps' preconditions.

    GROUNDED_TARGET_RECORDED = "grounded_target_recorded"
    # The step's :class:`TargetGroundingContract` must be stored
    # in :class:`MultiStepContext.grounded_targets` so later
    # steps can refer to it.

    NO_OBSERVATION_REGRESSION = "no_observation_regression"
    # The latest observation must NOT show a regression from the
    # pre-step observation.  Used by recovery / scroll loops to
    # detect that an action actually moved the state forward.

    IDEMPOTENT = "idempotent"
    # Re-running the same step with the same parameters must
    # produce the same outcome.  The executor records the
    # capability call's id in the idempotency log.


@dataclass(frozen=True)
class StepPrecondition:
    """A single precondition a :class:`PlanStep` may declare."""

    kind: PreconditionKind
    required_step_id: Optional[str] = None
    fact_key: Optional[str] = None
    fact_value: Any = None
    subject: Optional[str] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": "StepPrecondition",
            "kind": self.kind.value,
            "required_step_id": self.required_step_id,
            "fact_key": self.fact_key,
            "fact_value": self.fact_value,
            "subject": self.subject,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class StepPostcondition:
    """A single postcondition a :class:`PlanStep` may declare."""

    kind: PostconditionKind
    fact_key: Optional[str] = None
    fact_value: Any = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": "StepPostcondition",
            "kind": self.kind.value,
            "fact_key": self.fact_key,
            "fact_value": self.fact_value,
            "metadata": dict(self.metadata),
        }


# ---------------------------------------------------------------------------
# Helpers for reading pre/postconditions from PlanStep metadata.
# ---------------------------------------------------------------------------
# PlanStep is frozen; we do not extend it.  Instead, the planner (or
# any code that builds a PlanStep) stores pre/postconditions in
# ``PlanStep.metadata`` under the reserved keys below.  The executor
# reads them through these helpers so the storage location is
# encapsulated.

PRECONDITIONS_KEY = "phase14_preconditions"
POSTCONDITIONS_KEY = "phase14_postconditions"


def preconditions_from_metadata(
    metadata: Optional[Mapping[str, Any]],
) -> Tuple[StepPrecondition, ...]:
    if not metadata:
        return ()
    raw = metadata.get(PRECONDITIONS_KEY)
    if not raw:
        return ()
    out: list = []
    for item in raw:
        if isinstance(item, StepPrecondition):
            out.append(item)
        elif isinstance(item, Mapping):
            out.append(_precondition_from_mapping(item))
    return tuple(out)


def postconditions_from_metadata(
    metadata: Optional[Mapping[str, Any]],
) -> Tuple[StepPostcondition, ...]:
    if not metadata:
        return ()
    raw = metadata.get(POSTCONDITIONS_KEY)
    if not raw:
        return ()
    out: list = []
    for item in raw:
        if isinstance(item, StepPostcondition):
            out.append(item)
        elif isinstance(item, Mapping):
            out.append(_postcondition_from_mapping(item))
    return tuple(out)


def _precondition_from_mapping(m: Mapping[str, Any]) -> StepPrecondition:
    kind = PreconditionKind(m["kind"])
    return StepPrecondition(
        kind=kind,
        required_step_id=m.get("required_step_id"),
        fact_key=m.get("fact_key"),
        fact_value=m.get("fact_value"),
        subject=m.get("subject"),
        metadata=dict(m.get("metadata") or {}),
    )


def _postcondition_from_mapping(m: Mapping[str, Any]) -> StepPostcondition:
    kind = PostconditionKind(m["kind"])
    return StepPostcondition(
        kind=kind,
        fact_key=m.get("fact_key"),
        fact_value=m.get("fact_value"),
        metadata=dict(m.get("metadata") or {}),
    )


__all__ = [
    "PreconditionKind",
    "PostconditionKind",
    "StepPrecondition",
    "StepPostcondition",
    "PRECONDITIONS_KEY",
    "POSTCONDITIONS_KEY",
    "preconditions_from_metadata",
    "postconditions_from_metadata",
]
