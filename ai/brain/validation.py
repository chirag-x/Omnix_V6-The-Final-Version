"""
Omnix V6 — Plan payload validation (Phase 5C+5D).

The single gate that turns a raw LLM output (or any planner output)
into a trusted :class:`Plan`.  It performs:

    1.  **Schema validation**            — the payload is a dict, has
        ``goal_id``, has a non-empty ``steps`` list, each step is a
        dict, each step has a ``step_id``, ``description``, ``action``
        and (for ``CAPABILITY_CALL``) a non-empty ``capability_name``.

    2.  **Capability existence**         — every
        ``capability_name`` in every step must resolve in the
        canonical :class:`CapabilityRegistry`.  Invented capabilities
        are rejected (R-21 / AD-21).

    3.  **Parameter validation**         — every step's ``parameters``
        are coerced against the matching :class:`CapabilitySpec` using
        the same rules the router applies, but at the *plan boundary*
        rather than the *dispatch boundary*.  Invalid arguments are
        rejected.

    4.  **Safety classification**        — a step may declare a
        ``safety_classification`` (``"safe"`` / ``"reversible"`` /
        ``"dangerous"``).  The LLM cannot downgrade a dangerous
        capability to a non-dangerous classification.  The capability
        set is the source of truth.

    5.  **Step dependencies**            — every ``depends_on`` must
        reference an existing step; self-dependency is rejected;
        cycles are detected; the resulting graph must be a DAG.

    6.  **Timeouts**                     — every ``timeout_s`` must be
        in :data:`MIN_STEP_TIMEOUT_S`..:data:`MAX_STEP_TIMEOUT_S`.
        ``max_retries`` must be in :data:`MIN_RETRIES`..
        :data:`MAX_RETRIES`.

    7.  **Expected effects**             — structurally validated.

    8.  **Plan size**                    — bounded by
        :data:`MAX_PLAN_STEPS`.  A planner producing a 10,000-step
        plan is a planning failure.

    9.  **Plan id / goal id**            — the plan must reference a
        known goal id; the plan id is supplied by the Brain (or, in
        tests, the caller).

   10.  **Vision grounding metadata**    — Phase 7.3.  Every step
        that calls a target-bearing capability (mouse clicks,
        double-clicks, right-clicks) MUST declare its vision
        grounding intent *at the plan boundary* — either by setting
        ``metadata.vision_pre_action`` + ``metadata.vision_target_query``,
        or by opting out with ``metadata.vision_skip_grounding=True``
        plus explicit integer ``x``/``y`` parameters.  Unknown kinds,
        unknown strategies, and contradictory declarations are
        rejected with :class:`InvalidVisionMetadataError`.

   11.  **Browser grounding metadata**    — Phase 8.  Every step
        that calls a target-bearing browser capability
        (``browser.click``, ``browser.type``, ``browser.extract_text``)
        MUST declare its closed-set locator kind and a non-empty
        locator value (these are *capability parameters*; the spec
        already rejects unknown values).  Additionally the step may
        declare optional ``metadata.browser_session_id``,
        ``metadata.browser_fallback_to_vision`` (bool), and
        ``metadata.browser_target_query`` (the original human
        description, for diagnostics).  Mixing browser steps with
        ``vision_pre_action`` is forbidden; arbitrary
        JavaScript execution is never representable in the closed
        capability set, and the planner is forbidden from
        re-introducing it via metadata.  Rejections raise
        :class:`InvalidBrowserMetadataError`.

The module is *pure*: it never imports any V6 *Windows service*, the
``CapabilityRouter``, the engine, the orchestrator, or anything that
imports ``subprocess``/``pyautogui``/``win32gui``/``win32api``/
``ctypes``.  It produces a trusted ``Plan``; the executor (future
phase) is the only thing that turns a step into an
:class:`ActionRequest` and dispatches it.
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from core.capability import CapabilitySpec, ParamType
from core.capability_registry import CapabilityRegistry
from core.errors import ValidationError
from core.orchestration import (
    ActionKind,
    ExpectedEffect,
    Plan,
    PlanStatus,
    PlanStep,
)

from .exceptions import (
    InvalidArgumentError,
    InvalidBrowserMetadataError,
    InvalidDependencyError,
    InvalidExpectedEffectError,
    InvalidTimeoutError,
    InvalidVisionMetadataError,
    MalformedPlanPayload,
    PlanSizeExceeded,
    SafetyClassificationError,
    UnknownCapabilityError,
)


# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------

MAX_PLAN_STEPS: int = 64
MIN_STEP_TIMEOUT_S: float = 0.1
MAX_STEP_TIMEOUT_S: float = 600.0
MIN_RETRIES: int = 0
MAX_RETRIES: int = 5
MAX_STEP_DESCRIPTION_LENGTH: int = 1024
MAX_PAYLOAD_BYTES: int = 64 * 1024  # sanity bound for the raw LLM JSON
MAX_PARAMETER_DEPTH: int = 6
MAX_VISION_TARGET_QUERY_LENGTH: int = 256

#: Safety classifications a plan step may declare.  The mapping
#: translates to: ``"safe"`` = READ_ONLY, ``"reversible"`` = MUTATING,
#: ``"dangerous"`` = DANGEROUS.  The LLM cannot invent a fourth tier.
ALLOWED_SAFETY_CLASSIFICATIONS: Tuple[str, ...] = ("safe", "reversible", "dangerous")

#: The closed set of vision ``pre_action`` kinds a plan step may
#: declare.  These are the *only* values ``metadata.vision_pre_action``
#: may take; anything else is rejected at the validation gate.
#:
#: The kinds correspond 1:1 to :mod:`core.orchestration.vision_adapter`'s
#: adapters.  A planner MUST NOT invent a new kind; the closed set is
#: the only path to execution (R-21 / R-22).
ALLOWED_VISION_PRE_ACTIONS: Tuple[str, ...] = (
    "click",
    "double_click",
    "right_click",
    "focus",
    "type_into",
)

#: The closed set of vision strategies a planner may *prefer*.  The
#: router may still choose another strategy at runtime; ``preferred``
#: is a hint only.
ALLOWED_VISION_PREFERRED_STRATEGIES: Tuple[str, ...] = (
    "uia",
    "ocr",
    "visual",
    "coordinates",
)

#: Capabilities that always require a visual grounding before they
#: can be dispatched.  A step that calls one of these without
#: ``vision_pre_action`` metadata (or ``vision_skip_grounding=True``
#: with explicit coordinates) is rejected.
VISION_GROUNDED_CAPABILITIES: frozenset = frozenset({
    "desktop.mouse.click",
    "desktop.mouse.double_click",
    "desktop.mouse.right_click",
})


# ---------------------------------------------------------------------------
# Browser contract (Phase 8)
# ---------------------------------------------------------------------------
#
# The browser has its OWN grounding path (DOM → accessibility → text →
# xpath → test_id, with vision only as an opt-in fallback).  The plan
# boundary is the place where the planner must commit to a target
# strategy *before* the executor is allowed to act.
#
# Per the Phase 8 spec, the browser subsystem is the *only* path to a
# real browser (R-21).  There is no way for an LLM-generated plan to
# inject raw JavaScript, free-form selectors, or ``eval()`` payloads:
# the capability parameters are a closed ENUM + STRING, and arbitrary
# payload execution is not representable in the spec.
#
# What this validator does:
#
#   * On a target-bearing browser step, require the spec parameters
#     ``locator_kind`` + ``locator_value`` (these are *capability
#     parameters* — the spec already rejects them if missing or
#     invalid, so by the time we get here they are guaranteed to be
#     a closed-set enum value and a non-empty string).  That is the
#     primary contract.
#
#   * Optionally accept ``metadata.browser_session_id`` (string;
#     empty = default session), ``metadata.browser_target_query``
#     (the original human description, for diagnostics; bounded
#     length), and ``metadata.browser_fallback_to_vision`` (bool;
#     explicit opt-in to the vision fallback, which is otherwise
#     never used by browser steps).
#
#   * Reject a browser step that ALSO declares ``vision_pre_action``
#     — mixing the two grounding paths is a contract violation.

#: Capabilities that target a DOM element and therefore MUST carry a
#: closed-set locator (already enforced by the spec) and an optional
#: ``browser_session_id`` / ``browser_fallback_to_vision`` declaration.
#:
#: ``browser.navigate`` is intentionally NOT in this set — it has no
#: target, only a URL.  It still cannot mix with vision metadata, but
#: it does not need a target strategy.
BROWSER_TARGET_BEARING_CAPABILITIES: frozenset = frozenset({
    "browser.click",
    "browser.type",
    "browser.extract_text",
})

#: All capabilities that belong to the browser subsystem.  Used to
#: enforce the "no mixing with vision" rule.
BROWSER_CAPABILITIES: frozenset = frozenset({
    "browser.navigate",
    "browser.click",
    "browser.type",
    "browser.extract_text",
})

#: Maximum length of the optional ``browser_target_query`` metadata
#: field (the human-readable description; not the locator itself).
MAX_BROWSER_TARGET_QUERY_LENGTH: int = 256


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def validate_plan_payload(
    payload: Mapping[str, Any],
    *,
    registry: CapabilityRegistry,
    plan_id: Optional[str] = None,
    goal_id: Optional[str] = None,
    parent_plan_id: Optional[str] = None,
    replan_count: int = 0,
    notes: str = "",
    metadata: Optional[Dict[str, Any]] = None,
    max_steps: int = MAX_PLAN_STEPS,
) -> Plan:
    """Validate a raw planner payload and return a trusted :class:`Plan`.

    Args:
        payload: The raw planner output (e.g. parsed LLM JSON).  Must
            be a mapping with at minimum a ``steps`` list.
        registry: The canonical capability registry used to look up
            each step's ``capability_name`` and its parameter spec.
        plan_id: Optional id for the resulting plan.  When ``None``,
            a fresh short id is minted.
        goal_id: The goal id the plan fulfils.  When ``None``, the
            payload's ``goal_id`` is used; if the payload has none,
            a placeholder is minted.
        parent_plan_id: For replans, the id of the plan being
            replaced; ``None`` for a fresh plan.
        replan_count: The replan count, propagated by the Brain.
        notes: Free-form notes recorded on the plan.
        metadata: Optional metadata to attach to the plan.
        max_steps: Inclusive upper bound on the number of steps.

    Returns:
        A fully-validated, immutable :class:`Plan` with status
        ``PlanStatus.READY``.

    Raises:
        MalformedPlanPayload: The payload is not a valid plan dict.
        PlanSizeExceeded: The plan has more than ``max_steps`` steps.
        UnknownCapabilityError: A step references a capability that
            is not in the registry.
        InvalidArgumentError: A step's parameters do not match its
            capability's spec.
        SafetyClassificationError: A step tries to downgrade the
            safety of a dangerous capability.
        InvalidDependencyError: A step has a self-dep, a missing dep,
            or introduces a cycle.
        InvalidTimeoutError: A step's ``timeout_s`` or ``max_retries``
            is out of range.
        InvalidExpectedEffectError: A step's expected effect is
            structurally invalid.
        InvalidVisionMetadataError: A vision-grounded step has missing
            or contradictory vision metadata (Phase 7.3).
        InvalidBrowserMetadataError: A browser step has malformed or
            contradictory browser metadata, mixes browser with vision
            grounding, or has a target-bearing browser step without a
            closed-set locator (Phase 8).
    """
    if registry is None:
        raise MalformedPlanPayload("A CapabilityRegistry is required for plan validation.")
    if not isinstance(payload, Mapping):
        raise MalformedPlanPayload(
            "Plan payload must be a mapping.",
            context={"actual_type": type(payload).__name__},
        )

    # 0) Rough size bound (catches LLM regurgitation early).
    approx_size = len(str(payload))
    if approx_size > MAX_PAYLOAD_BYTES:
        raise MalformedPlanPayload(
            "Plan payload exceeds the size limit.",
            context={"size": approx_size, "max": MAX_PAYLOAD_BYTES},
        )

    # 1) Goal id: prefer the explicit one; fall back to payload; then mint.
    resolved_goal_id: str
    if goal_id:
        resolved_goal_id = str(goal_id)
    else:
        raw = payload.get("goal_id")
        if isinstance(raw, str) and raw.strip():
            resolved_goal_id = raw
        else:
            resolved_goal_id = f"goal_{_short_id()}"

    # 2) Steps list.
    raw_steps = payload.get("steps")
    if raw_steps is None:
        raise MalformedPlanPayload(
            "Plan payload missing 'steps' list.",
        )
    if not isinstance(raw_steps, list):
        raise MalformedPlanPayload(
            "Plan payload 'steps' must be a list.",
            context={"actual_type": type(raw_steps).__name__},
        )
    if len(raw_steps) == 0:
        raise MalformedPlanPayload(
            "Plan payload has zero steps; the planner must produce at least one.",
        )
    if len(raw_steps) > max_steps:
        raise PlanSizeExceeded(
            f"Plan has {len(raw_steps)} steps; max is {max_steps}.",
            context={"step_count": len(raw_steps), "max": max_steps},
        )

    # 3) Per-step validation.
    validated_steps: List[PlanStep] = []
    seen_step_ids: Set[str] = set()
    spec_cache: Dict[str, CapabilitySpec] = {}
    for idx, raw_step in enumerate(raw_steps):
        step = _validate_one_step(
            raw_step,
            index=idx,
            registry=registry,
            spec_cache=spec_cache,
        )
        if step.step_id in seen_step_ids:
            raise MalformedPlanPayload(
                f"Plan has duplicate step_id {step.step_id!r}.",
                context={"step_id": step.step_id, "index": idx},
            )
        seen_step_ids.add(step.step_id)
        validated_steps.append(step)

    # 4) Dependency validation (DAG) over the validated set.
    _validate_dependencies(validated_steps)

    # 5) Assemble the plan.
    resolved_plan_id = str(plan_id) if plan_id else f"plan_{_short_id()}"
    return Plan(
        plan_id=resolved_plan_id,
        goal_id=resolved_goal_id,
        steps=tuple(validated_steps),
        status=PlanStatus.READY,
        created_at=time.time(),
        replan_count=int(replan_count),
        parent_plan_id=parent_plan_id,
        notes=str(notes or ""),
        metadata=dict(metadata) if metadata else {},
    )


# ---------------------------------------------------------------------------
# Per-step
# ---------------------------------------------------------------------------

def _validate_one_step(
    raw_step: Any,
    *,
    index: int,
    registry: CapabilityRegistry,
    spec_cache: Dict[str, CapabilitySpec],
) -> PlanStep:
    if not isinstance(raw_step, Mapping):
        raise MalformedPlanPayload(
            f"Step #{index} is not a mapping.",
            context={"index": index, "actual_type": type(raw_step).__name__},
        )

    step_id = raw_step.get("step_id")
    if not isinstance(step_id, str) or not step_id.strip():
        raise MalformedPlanPayload(
            f"Step #{index} has no 'step_id'.",
            context={"index": index},
        )

    description = raw_step.get("description", "")
    if not isinstance(description, str):
        raise MalformedPlanPayload(
            f"Step {step_id!r} 'description' must be a string.",
            context={"step_id": step_id, "actual_type": type(description).__name__},
        )
    if len(description) > MAX_STEP_DESCRIPTION_LENGTH:
        raise MalformedPlanPayload(
            f"Step {step_id!r} 'description' too long.",
            context={"step_id": step_id, "length": len(description), "max": MAX_STEP_DESCRIPTION_LENGTH},
        )

    raw_action = raw_step.get("action", ActionKind.CAPABILITY_CALL.value)
    try:
        action = raw_action if isinstance(raw_action, ActionKind) else ActionKind(str(raw_action))
    except ValueError as exc:
        raise MalformedPlanPayload(
            f"Step {step_id!r} has unknown action {raw_action!r}.",
            context={"step_id": step_id, "action": str(raw_action)},
        ) from exc

    # Default fields, populated only for CAPABILITY_CALL.
    capability_name: str = ""
    parameters: Dict[str, Any] = {}
    raw_expected: Optional[ExpectedEffect] = None

    if action is ActionKind.CAPABILITY_CALL:
        cap_name = raw_step.get("capability_name")
        if not isinstance(cap_name, str) or not cap_name.strip():
            raise MalformedPlanPayload(
                f"Step {step_id!r} has no 'capability_name' for CAPABILITY_CALL.",
                context={"step_id": step_id},
            )
        spec = _lookup_spec(cap_name, registry, spec_cache)
        capability_name = spec.name  # canonical, not the LLM's casing

        raw_params = raw_step.get("parameters", {}) or {}
        if not isinstance(raw_params, Mapping):
            raise InvalidArgumentError(
                f"Step {step_id!r} 'parameters' must be a mapping.",
                context={"step_id": step_id, "actual_type": type(raw_params).__name__},
            )
        _check_depth(f"step[{step_id}].parameters", dict(raw_params), depth=0)
        parameters = _coerce_against_spec(step_id, spec, dict(raw_params))

        # Safety classification (optional but bounded).
        _validate_safety_classification(step_id, spec, raw_step)

        # Expected effect (optional).
        if "expected_effect" in raw_step and raw_step["expected_effect"] is not None:
            raw_expected = _validate_expected_effect(step_id, raw_step["expected_effect"])

    elif action is ActionKind.OBSERVE:
        capability_name = ""
        parameters = dict(raw_step.get("parameters", {}) or {})
    elif action is ActionKind.VERIFY:
        if "expected_effect" in raw_step and raw_step["expected_effect"] is not None:
            raw_expected = _validate_expected_effect(step_id, raw_step["expected_effect"])
        capability_name = ""
        parameters = dict(raw_step.get("parameters", {}) or {})
    elif action is ActionKind.WAIT:
        capability_name = ""
        parameters = dict(raw_step.get("parameters", {}) or {})
    elif action is ActionKind.ASK_USER:
        capability_name = ""
        parameters = dict(raw_step.get("parameters", {}) or {})
    else:  # pragma: no cover - the action enum is exhaustively covered above
        raise MalformedPlanPayload(
            f"Step {step_id!r} has unhandled action {action!r}.",
            context={"step_id": step_id, "action": str(action)},
        )

    timeout_s = _validate_timeout(step_id, raw_step.get("timeout_s", 30.0))
    max_retries = _validate_retries(step_id, raw_step.get("max_retries", 0))

    raw_deps = raw_step.get("depends_on", ()) or ()
    if not isinstance(raw_deps, (list, tuple)) or not all(
        isinstance(d, str) for d in raw_deps
    ):
        raise InvalidDependencyError(
            f"Step {step_id!r} 'depends_on' must be a list of strings.",
            context={"step_id": step_id},
        )
    depends_on: Tuple[str, ...] = tuple(raw_deps)
    for d in depends_on:
        if d == step_id:
            raise InvalidDependencyError(
                f"Step {step_id!r} has a self-dependency.",
                context={"step_id": step_id, "depends_on": list(depends_on)},
            )

    raw_metadata = raw_step.get("metadata", {}) or {}
    if not isinstance(raw_metadata, dict):
        raise MalformedPlanPayload(
            f"Step {step_id!r} 'metadata' must be a dict.",
            context={"step_id": step_id, "actual_type": type(raw_metadata).__name__},
        )

    # Vision metadata is the *only* path from planner into vision.
    # Phase 7.3 — every step that targets the screen MUST declare
    # either grounding metadata or an explicit coordinate bypass.
    if action is ActionKind.CAPABILITY_CALL:
        _validate_vision_metadata(
            step_id,
            capability_name,
            parameters,
            dict(raw_metadata),
        )

    # Browser metadata is the *only* path from planner into the
    # browser subsystem.  Phase 8 — every target-bearing browser step
    # MUST carry a closed-set locator (already enforced by the spec
    # parameters) and is FORBIDDEN from mixing with vision metadata.
    if action is ActionKind.CAPABILITY_CALL:
        _validate_browser_metadata(
            step_id,
            capability_name,
            parameters,
            dict(raw_metadata),
        )

    # PlanStep.__post_init__ also enforces the shell-token guard.  We
    # construct via ``PlanStep(...)``; if it raises, convert to the
    # brain-typed error.
    try:
        return PlanStep(
            step_id=step_id,
            description=description,
            action=action,
            capability_name=capability_name,
            parameters=parameters,
            expected_effect=raw_expected,
            depends_on=depends_on,
            timeout_s=timeout_s,
            max_retries=max_retries,
            metadata=dict(raw_metadata),
        )
    except ValueError as exc:
        # Most likely a shell-token rejection.
        raise MalformedPlanPayload(
            f"Step {step_id!r} rejected at construction: {exc}",
            context={"step_id": step_id, "reason": str(exc)},
        ) from exc


def _lookup_spec(
    cap_name: str,
    registry: CapabilityRegistry,
    spec_cache: Dict[str, CapabilitySpec],
) -> CapabilitySpec:
    if cap_name in spec_cache:
        return spec_cache[cap_name]
    cap = registry.get(cap_name)
    if cap is None:
        raise UnknownCapabilityError(
            f"Plan references unknown capability {cap_name!r}.",
            context={"capability": cap_name},
        )
    spec = cap.spec
    spec_cache[cap_name] = spec
    return spec


# ---------------------------------------------------------------------------
# Parameter coercion
# ---------------------------------------------------------------------------

def _coerce_against_spec(
    step_id: str,
    spec: CapabilitySpec,
    raw: Dict[str, Any],
) -> Dict[str, Any]:
    """Coerce ``raw`` against ``spec.parameters`` using the same rules
    the router applies (delegated to :class:`CapabilityParameter.coerce`).

    We do not import the router: the spec is the source of truth and
    its parameters know how to validate themselves.
    """
    if not isinstance(raw, dict):
        raise InvalidArgumentError(
            f"Step {step_id!r} 'parameters' must be a dict.",
            context={"step_id": step_id, "actual_type": type(raw).__name__},
        )
    # Phase 12: ``spec.parameters`` may be expressed as either a tuple
    # of ``CapabilityParameter`` (the canonical dataclass form) or a
    # dict mapping ``name → CapabilityParameter`` (the historical form
    # used by every existing capability).  The validation layer must
    # accept both -- the canonical ``_iter_parameters`` helper from
    # ``core.capability`` is the single source of truth.
    from core.capability import _iter_parameters  # late import to avoid cycles

    params_list = list(_iter_parameters(spec))
    allowed = {p.name for p in params_list}
    unknown = [k for k in raw if k not in allowed]
    if unknown:
        raise InvalidArgumentError(
            f"Step {step_id!r} has unexpected parameters for capability {spec.name!r}.",
            context={
                "step_id": step_id,
                "capability": spec.name,
                "unexpected": sorted(unknown),
            },
        )
    out: Dict[str, Any] = {}
    for p in params_list:
        if p.name in raw:
            value = raw[p.name]
        else:
            if p.required and p.default is None:
                raise InvalidArgumentError(
                    f"Step {step_id!r} missing required parameter {p.name!r}.",
                    context={
                        "step_id": step_id,
                        "capability": spec.name,
                        "parameter": p.name,
                    },
                )
            value = p.default
        try:
            out[p.name] = p.coerce(value)
        except (ValueError, ValidationError) as exc:
            raise InvalidArgumentError(
                f"Step {step_id!r} parameter {p.name!r} invalid: {exc}",
                context={
                    "step_id": step_id,
                    "capability": spec.name,
                    "parameter": p.name,
                    "reason": str(exc),
                },
            ) from exc
    return out


def _check_depth(name: str, value: Any, *, depth: int) -> None:
    """Cap parameter nesting to keep payloads flat and serialisation
    cheap.  Anything deeper is a planning mistake."""
    if depth >= MAX_PARAMETER_DEPTH:
        raise InvalidArgumentError(
            f"Parameter nesting for {name!r} exceeds max depth.",
            context={"max_depth": MAX_PARAMETER_DEPTH},
        )
    if isinstance(value, dict):
        for k, v in value.items():
            _check_depth(f"{name}.{k}", v, depth=depth + 1)
    elif isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            _check_depth(f"{name}[{i}]", v, depth=depth + 1)


# ---------------------------------------------------------------------------
# Safety classification
# ---------------------------------------------------------------------------

def _validate_safety_classification(
    step_id: str,
    spec: CapabilitySpec,
    raw_step: Mapping[str, Any],
) -> None:
    """Reject any attempt to mark a dangerous capability as safe.

    The LLM may declare a ``safety_classification`` field on a step
    for the *executor* to consider (e.g. "ask the user before
    executing").  The LLM MAY NOT declare a dangerous capability as
    non-dangerous.  ``spec.dangerous`` is the source of truth.
    """
    declared = raw_step.get("safety_classification")
    if declared is None:
        return
    if not isinstance(declared, str):
        raise SafetyClassificationError(
            f"Step {step_id!r} 'safety_classification' must be a string.",
            context={"step_id": step_id, "actual_type": type(declared).__name__},
        )
    if declared not in ALLOWED_SAFETY_CLASSIFICATIONS:
        raise SafetyClassificationError(
            f"Step {step_id!r} declared unknown safety classification {declared!r}.",
            context={"step_id": step_id, "declared": declared, "allowed": list(ALLOWED_SAFETY_CLASSIFICATIONS)},
        )
    if spec.dangerous and declared != "dangerous":
        raise SafetyClassificationError(
            f"Step {step_id!r} tries to downgrade dangerous capability {spec.name!r}.",
            context={
                "step_id": step_id,
                "capability": spec.name,
                "declared": declared,
                "required": "dangerous",
            },
        )


# ---------------------------------------------------------------------------
# Timeouts & retries
# ---------------------------------------------------------------------------

def _validate_timeout(step_id: str, value: Any) -> float:
    if value is None:
        return 30.0
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise InvalidTimeoutError(
            f"Step {step_id!r} 'timeout_s' must be a number.",
            context={"step_id": step_id, "actual_type": type(value).__name__},
        )
    f = float(value)
    if f < MIN_STEP_TIMEOUT_S or f > MAX_STEP_TIMEOUT_S:
        raise InvalidTimeoutError(
            f"Step {step_id!r} 'timeout_s' out of range.",
            context={
                "step_id": step_id,
                "value": f,
                "min": MIN_STEP_TIMEOUT_S,
                "max": MAX_STEP_TIMEOUT_S,
            },
        )
    return f


def _validate_retries(step_id: str, value: Any) -> int:
    if value is None:
        return 0
    if not isinstance(value, int) or isinstance(value, bool):
        raise InvalidTimeoutError(
            f"Step {step_id!r} 'max_retries' must be an int.",
            context={"step_id": step_id, "actual_type": type(value).__name__},
        )
    if value < MIN_RETRIES or value > MAX_RETRIES:
        raise InvalidTimeoutError(
            f"Step {step_id!r} 'max_retries' out of range.",
            context={
                "step_id": step_id,
                "value": value,
                "min": MIN_RETRIES,
                "max": MAX_RETRIES,
            },
        )
    return value


# ---------------------------------------------------------------------------
# Vision metadata (Phase 7.3)
# ---------------------------------------------------------------------------

def _validate_vision_metadata(
    step_id: str,
    capability_name: str,
    parameters: Dict[str, Any],
    metadata: Dict[str, Any],
) -> None:
    """Validate the planner → vision contract on a single step.

    The Agent only consults vision when ``metadata["vision_pre_action"]``
    is set, AND it refuses to dispatch a target-bearing capability
    without grounding (see :class:`core.orchestration.agent.Agent`).
    This validator is the *plan-boundary* counterpart: the planner
    must declare its intent before the plan leaves the Brain.

    Rules (R-21 / R-22):

      * If ``vision_pre_action`` is set, it MUST be one of
        :data:`ALLOWED_VISION_PRE_ACTIONS`.
      * If ``vision_pre_action`` is set, ``vision_target_query`` MUST
        be a non-empty string of length at most
        :data:`MAX_VISION_TARGET_QUERY_LENGTH`.
      * If ``vision_preferred_strategy`` is set, it MUST be one of
        :data:`ALLOWED_VISION_PREFERRED_STRATEGIES`.
      * ``vision_skip_grounding`` MUST be a boolean when present.
      * If the capability is in :data:`VISION_GROUNDED_CAPABILITIES`,
        the step MUST either declare ``vision_pre_action`` or opt
        out via ``vision_skip_grounding=True`` *with explicit
        coordinates* (both ``x`` and ``y`` ints present in
        parameters).  An opt-out without coordinates is rejected;
        a missing declaration is rejected.
    """
    if not isinstance(metadata, dict):
        # This should never happen — the caller already typed it.
        raise InvalidVisionMetadataError(
            f"Step {step_id!r} vision metadata must be a dict.",
            context={"step_id": step_id, "actual_type": type(metadata).__name__},
        )

    pre_action = metadata.get("vision_pre_action", None)
    target_query = metadata.get("vision_target_query", None)
    preferred_strategy = metadata.get("vision_preferred_strategy", None)
    skip_grounding = metadata.get("vision_skip_grounding", None)

    if pre_action is None:
        # No declared pre-action; validate only the optional fields and
        # the capability-grounding rule.
        if target_query is not None:
            if not isinstance(target_query, str) or not target_query.strip():
                raise InvalidVisionMetadataError(
                    f"Step {step_id!r} declared 'vision_target_query' without "
                    f"'vision_pre_action'; remove it or declare a pre_action.",
                    context={"step_id": step_id},
                )
        if preferred_strategy is not None:
            if preferred_strategy not in ALLOWED_VISION_PREFERRED_STRATEGIES:
                raise InvalidVisionMetadataError(
                    f"Step {step_id!r} declared unknown "
                    f"vision_preferred_strategy {preferred_strategy!r}.",
                    context={
                        "step_id": step_id,
                        "preferred_strategy": preferred_strategy,
                        "allowed": list(ALLOWED_VISION_PREFERRED_STRATEGIES),
                    },
                )
        if skip_grounding is not None and not isinstance(skip_grounding, bool):
            raise InvalidVisionMetadataError(
                f"Step {step_id!r} 'vision_skip_grounding' must be a bool.",
                context={
                    "step_id": step_id,
                    "actual_type": type(skip_grounding).__name__,
                },
            )

        if capability_name in VISION_GROUNDED_CAPABILITIES:
            if not (isinstance(skip_grounding, bool) and skip_grounding):
                raise InvalidVisionMetadataError(
                    f"Step {step_id!r} calls grounded capability "
                    f"{capability_name!r} without 'vision_pre_action'; "
                    f"either declare a pre_action or set "
                    f"'vision_skip_grounding=True' with explicit "
                    f"coordinates (x, y).",
                    context={
                        "step_id": step_id,
                        "capability": capability_name,
                    },
                )
            if not _has_explicit_coordinates(parameters):
                raise InvalidVisionMetadataError(
                    f"Step {step_id!r} opted out of vision grounding for "
                    f"capability {capability_name!r} but did not supply "
                    f"explicit integer 'x' and 'y' parameters; the bypass "
                    f"is unsafe.",
                    context={
                        "step_id": step_id,
                        "capability": capability_name,
                        "parameters": dict(parameters),
                    },
                )
        return

    # pre_action is set — it must be a known kind.
    if not isinstance(pre_action, str):
        raise InvalidVisionMetadataError(
            f"Step {step_id!r} 'vision_pre_action' must be a string.",
            context={
                "step_id": step_id,
                "actual_type": type(pre_action).__name__,
            },
        )
    if pre_action not in ALLOWED_VISION_PRE_ACTIONS:
        raise InvalidVisionMetadataError(
            f"Step {step_id!r} declared unknown vision_pre_action "
            f"{pre_action!r}; expected one of "
            f"{list(ALLOWED_VISION_PRE_ACTIONS)}.",
            context={
                "step_id": step_id,
                "pre_action": pre_action,
                "allowed": list(ALLOWED_VISION_PRE_ACTIONS),
            },
        )

    # vision_target_query must be a non-empty string of bounded length.
    if not isinstance(target_query, str) or not target_query.strip():
        raise InvalidVisionMetadataError(
            f"Step {step_id!r} declared vision_pre_action {pre_action!r} "
            f"but has no 'vision_target_query'.",
            context={"step_id": step_id, "pre_action": pre_action},
        )
    if len(target_query) > MAX_VISION_TARGET_QUERY_LENGTH:
        raise InvalidVisionMetadataError(
            f"Step {step_id!r} 'vision_target_query' is too long.",
            context={
                "step_id": step_id,
                "length": len(target_query),
                "max": MAX_VISION_TARGET_QUERY_LENGTH,
            },
        )

    # type_into requires a non-empty 'text' parameter — the adapter
    # enforces this too, but we fail at the plan boundary for clarity.
    if pre_action == "type_into":
        text = parameters.get("text")
        if not isinstance(text, str) or not text:
            raise InvalidVisionMetadataError(
                f"Step {step_id!r} declared vision_pre_action 'type_into' "
                f"but has no 'text' parameter; refuse to plan typing "
                f"without a payload.",
                context={"step_id": step_id},
            )

    # Optional preferred strategy.
    if preferred_strategy is not None:
        if preferred_strategy not in ALLOWED_VISION_PREFERRED_STRATEGIES:
            raise InvalidVisionMetadataError(
                f"Step {step_id!r} declared unknown "
                f"vision_preferred_strategy {preferred_strategy!r}.",
                context={
                    "step_id": step_id,
                    "preferred_strategy": preferred_strategy,
                    "allowed": list(ALLOWED_VISION_PREFERRED_STRATEGIES),
                },
            )

    # A skip + pre_action combination is contradictory: pick one.
    if isinstance(skip_grounding, bool) and skip_grounding:
        raise InvalidVisionMetadataError(
            f"Step {step_id!r} declared both 'vision_pre_action' and "
            f"'vision_skip_grounding=True'; the planner must commit to "
            f"one path.",
            context={"step_id": step_id, "pre_action": pre_action},
        )
    if skip_grounding is not None and not isinstance(skip_grounding, bool):
        raise InvalidVisionMetadataError(
            f"Step {step_id!r} 'vision_skip_grounding' must be a bool.",
            context={
                "step_id": step_id,
                "actual_type": type(skip_grounding).__name__,
            },
        )


def _has_explicit_coordinates(parameters: Dict[str, Any]) -> bool:
    """True when ``parameters`` carries explicit integer x/y.

    The bypass is only safe when the caller has independently decided
    the coordinates; the planner must not let vision be silently
    skipped on a click without numbers.
    """
    if not isinstance(parameters, dict):
        return False
    x = parameters.get("x", None)
    y = parameters.get("y", None)
    if not isinstance(x, int) or isinstance(x, bool):
        return False
    if not isinstance(y, int) or isinstance(y, bool):
        return False
    return True


# ---------------------------------------------------------------------------
# Browser metadata (Phase 8)
# ---------------------------------------------------------------------------

def _validate_browser_metadata(
    step_id: str,
    capability_name: str,
    parameters: Dict[str, Any],
    metadata: Dict[str, Any],
) -> None:
    """Validate the planner → browser contract on a single step.

    The browser has its own closed-set grounding path: every
    target-bearing step must carry ``locator_kind`` (an ENUM from the
    :class:`core.services.browser_service.LocatorKind` closed set) and
    ``locator_value`` (a non-empty string).  Both are *capability
    parameters* and have already been coerced by :func:`_coerce_against_spec`
    before this function is called — by the time we land here, those
    parameters are guaranteed to be valid (or the step has already
    been rejected by the spec coercion path).

    This validator's job is therefore narrower than the vision
    validator's:

      1. **Non-interference with vision** — a step that uses a browser
         capability MUST NOT also declare ``vision_pre_action`` or
         ``vision_target_query``.  Mixing the two grounding paths is a
         contract violation; pick one.

      2. **Optional session declaration** — ``metadata.browser_session_id``,
         when present, MUST be a non-empty string.  An empty string is
         legal and means "default session" (the convention used by
         :class:`core.services.browser_service.BrowserService`).

      3. **Optional vision fallback opt-in** —
         ``metadata.browser_fallback_to_vision`` is a bool, defaults
         to ``False`` when absent.  Even when ``True``, the executor
         only consults vision *after* the closed-set locators have
         failed — the closed capability set (R-21) is still the only
         path to the browser.

      4. **Optional diagnostic query** —
         ``metadata.browser_target_query`` is the human-readable
         description the planner received from the user; it is for
         logging and verification only, NEVER for resolution.  Bounded
         length.

    The validator never invents a locator and never tries to resolve
    a target; the *executor* does that through the closed
    :class:`BrowserService` boundary.
    """
    if not isinstance(metadata, dict):
        # The caller already typed it.
        raise InvalidBrowserMetadataError(
            f"Step {step_id!r} browser metadata must be a dict.",
            context={"step_id": step_id, "actual_type": type(metadata).__name__},
        )

    # 1) Non-interference with vision.
    if capability_name in BROWSER_CAPABILITIES:
        if "vision_pre_action" in metadata and metadata["vision_pre_action"] is not None:
            raise InvalidBrowserMetadataError(
                f"Step {step_id!r} uses browser capability {capability_name!r} "
                f"but also declares 'vision_pre_action'; the planner must "
                f"commit to one grounding path (browser has its own).",
                context={"step_id": step_id, "capability": capability_name},
            )
        if "vision_target_query" in metadata and metadata["vision_target_query"] is not None:
            raise InvalidBrowserMetadataError(
                f"Step {step_id!r} uses browser capability {capability_name!r} "
                f"but also declares 'vision_target_query'; vision grounding "
                f"is not used by the browser subsystem.",
                context={"step_id": step_id, "capability": capability_name},
            )

    # 2) Optional session id.
    session_id = metadata.get("browser_session_id", None)
    if session_id is not None:
        if not isinstance(session_id, str):
            raise InvalidBrowserMetadataError(
                f"Step {step_id!r} 'browser_session_id' must be a string.",
                context={
                    "step_id": step_id,
                    "actual_type": type(session_id).__name__,
                },
            )
        if not session_id.strip():
            # Empty string is the canonical "default session" marker;
            # reject only non-string types, not the empty marker.
            pass

    # 3) Optional vision fallback opt-in.
    fallback = metadata.get("browser_fallback_to_vision", None)
    if fallback is not None and not isinstance(fallback, bool):
        raise InvalidBrowserMetadataError(
            f"Step {step_id!r} 'browser_fallback_to_vision' must be a bool.",
            context={
                "step_id": step_id,
                "actual_type": type(fallback).__name__,
            },
        )

    # 4) Optional diagnostic query.
    target_query = metadata.get("browser_target_query", None)
    if target_query is not None:
        if not isinstance(target_query, str) or not target_query.strip():
            raise InvalidBrowserMetadataError(
                f"Step {step_id!r} 'browser_target_query' must be a non-empty "
                f"string when present.",
                context={"step_id": step_id},
            )
        if len(target_query) > MAX_BROWSER_TARGET_QUERY_LENGTH:
            raise InvalidBrowserMetadataError(
                f"Step {step_id!r} 'browser_target_query' is too long.",
                context={
                    "step_id": step_id,
                    "length": len(target_query),
                    "max": MAX_BROWSER_TARGET_QUERY_LENGTH,
                },
            )

    # 5) Target-bearing browser steps must carry the spec-validated
    #    locator parameters.  This is a defense-in-depth check: the
    #    capability spec already rejects missing or invalid
    #    ``locator_kind`` / ``locator_value`` at coercion time, so by
    #    the time we land here they are guaranteed to be a closed-set
    #    enum value and a non-empty string — unless the planner
    #    bypassed validation, in which case we still refuse.
    if capability_name in BROWSER_TARGET_BEARING_CAPABILITIES:
        kind = parameters.get("locator_kind", None)
        value = parameters.get("locator_value", None)
        if not isinstance(kind, str) or not kind.strip():
            # Should never happen — spec coercion already rejected.
            raise InvalidBrowserMetadataError(
                f"Step {step_id!r} target-bearing browser step has no "
                f"'locator_kind' in its parameters; this should have been "
                f"rejected by the capability spec.",
                context={"step_id": step_id, "capability": capability_name},
            )
        if not isinstance(value, str) or not value.strip():
            raise InvalidBrowserMetadataError(
                f"Step {step_id!r} target-bearing browser step has no "
                f"'locator_value' in its parameters; refuse to plan a "
                f"browser action without a closed-set locator.",
                context={"step_id": step_id, "capability": capability_name},
            )


# ---------------------------------------------------------------------------
# Expected effect
# ---------------------------------------------------------------------------

def _validate_expected_effect(step_id: str, raw: Any) -> ExpectedEffect:
    if not isinstance(raw, Mapping):
        raise InvalidExpectedEffectError(
            f"Step {step_id!r} 'expected_effect' must be a mapping.",
            context={"step_id": step_id, "actual_type": type(raw).__name__},
        )
    check_name = raw.get("check_name")
    if not isinstance(check_name, str) or not check_name.strip():
        raise InvalidExpectedEffectError(
            f"Step {step_id!r} 'expected_effect' needs a non-empty 'check_name'.",
            context={"step_id": step_id},
        )
    timeout_s = raw.get("timeout_s", 0.0)
    if timeout_s is None:
        timeout_s = 0.0
    if not isinstance(timeout_s, (int, float)) or isinstance(timeout_s, bool):
        raise InvalidExpectedEffectError(
            f"Step {step_id!r} 'expected_effect.timeout_s' must be a number.",
            context={"step_id": step_id, "actual_type": type(timeout_s).__name__},
        )
    ts = float(timeout_s)
    if ts < 0.0 or ts > MAX_STEP_TIMEOUT_S:
        raise InvalidExpectedEffectError(
            f"Step {step_id!r} 'expected_effect.timeout_s' out of range.",
            context={"step_id": step_id, "value": ts, "max": MAX_STEP_TIMEOUT_S},
        )
    metadata = raw.get("metadata", {}) or {}
    if not isinstance(metadata, dict):
        raise InvalidExpectedEffectError(
            f"Step {step_id!r} 'expected_effect.metadata' must be a dict.",
            context={"step_id": step_id},
        )
    return ExpectedEffect(
        check_name=check_name,
        expected=raw.get("expected"),
        timeout_s=ts,
        description=str(raw.get("description", "") or ""),
        metadata=dict(metadata),
    )


# ---------------------------------------------------------------------------
# Dependency graph validation (DAG)
# ---------------------------------------------------------------------------

def _validate_dependencies(steps: Sequence[PlanStep]) -> None:
    ids = {s.step_id for s in steps}
    for s in steps:
        for d in s.depends_on:
            if d not in ids:
                raise InvalidDependencyError(
                    f"Step {s.step_id!r} depends on unknown step {d!r}.",
                    context={"step_id": s.step_id, "missing": d},
                )

    # Cycle detection (Kahn's algorithm over the DAG).
    indegree: Dict[str, int] = {s.step_id: 0 for s in steps}
    edges: Dict[str, List[str]] = {s.step_id: [] for s in steps}
    for s in steps:
        for d in s.depends_on:
            indegree[s.step_id] = indegree.get(s.step_id, 0) + 1
            edges.setdefault(d, []).append(s.step_id)

    queue: List[str] = [sid for sid, deg in indegree.items() if deg == 0]
    visited = 0
    while queue:
        current = queue.pop(0)
        visited += 1
        for nxt in edges.get(current, []):
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                queue.append(nxt)
    if visited != len(steps):
        # Find one cycle to report.
        cyclic = [sid for sid, deg in indegree.items() if deg > 0]
        raise InvalidDependencyError(
            "Plan dependency graph contains a cycle.",
            context={"cyclic": sorted(cyclic)},
        )


# ---------------------------------------------------------------------------
# ID minting
# ---------------------------------------------------------------------------

def _short_id() -> str:
    return uuid.uuid4().hex[:12]
