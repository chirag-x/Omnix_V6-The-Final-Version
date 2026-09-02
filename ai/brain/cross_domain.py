"""
Omnix V6 — Phase 14: cross-domain workflow helpers.

Phase 14 §8 says a multi-step goal may straddle multiple V6
domains: open Notepad (desktop), type text (application), save to a
file (filesystem), open Chrome (desktop), navigate to a URL
(browser), upload the file (browser), verify the upload (browser /
vision).  No single capability family can express that; the
planner must *compose* them.

This module provides the composition primitives — pure data, no
execution:

    * :class:`DomainKind` — a closed enum of V6's execution domains.
    * :class:`DomainStep` — a typed description of one step in a
      cross-domain plan, with the originating domain, a target
      intent, and a precondition on a prior step.
    * :class:`CrossDomainPlan` — an ordered list of
      :class:`DomainStep` together with the per-domain safety
      policy tags the executor must honour.
    * :func:`compose_cross_domain_plan` — the canonical builder
      used by the LLM planner.  The LLM emits intent kinds in
      order; the helper turns them into a :class:`CrossDomainPlan`
      with the cross-domain safety checks already stamped.

The composition is *non-executing*: the helper never calls a
capability, never calls a service, never reads the screen.  The
executor still walks the plan through the canonical
:class:`core.orchestration.PlanExecutor`.

Architectural isolation:
    This module MUST NOT import:
        * :mod:`subprocess`
        * :mod:`pyautogui`
        * :mod:`win32gui` / :mod:`win32api`
        * :mod:`ctypes`
        * :mod:`core.capability_router`
        * :mod:`core.omnix_engine`
        * :mod:`core.pipeline`
        * :mod:`core.services.*` (vision / browser / memory / voice)
        * any V6 *Windows service* (e.g. ``system.windows.*``)
        * any V6 *AI provider* (e.g. ``ai.provider.*``)
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ai.intent.interpreter import IntentKind


class DomainKind(str, Enum):
    """The closed set of V6 execution domains a cross-domain plan may span.

    Each domain has its own safety policy surface; the executor
    routes a step through the domain's capability family via the
    closed :class:`core.capability.CapabilityRegistry`.  The set is
    closed; the cross-domain plan may *only* refer to kinds in this
    enum.
    """

    DESKTOP = "desktop"        # open / close / focus / list windows
    APPLICATION = "application"  # act inside an open native window
    BROWSER = "browser"        # act inside a web page
    FILESYSTEM = "filesystem"  # read / write / move / delete files
    PROCESS = "process"        # start / kill / list OS processes
    OBSERVATION = "observation"  # take an observation; no side-effect
    VERIFICATION = "verification"  # run a verification check; no side-effect


# Map of IntentKind to a recommended DomainKind.  Used by
# ``compose_cross_domain_plan`` to project a stream of intent kinds
# into a domain-tagged plan.  This is the *only* place that maps
# intents to domains; the LLM planner and the deterministic planner
# both use it.
_INTENT_KIND_TO_DOMAIN: Dict[IntentKind, DomainKind] = {
    IntentKind.OPEN_APPLICATION: DomainKind.DESKTOP,
    IntentKind.CLOSE_APPLICATION: DomainKind.DESKTOP,
    IntentKind.FOCUS_APPLICATION: DomainKind.DESKTOP,
    IntentKind.CONTROL_APPLICATION: DomainKind.APPLICATION,
    IntentKind.FILE_FIND: DomainKind.FILESYSTEM,
    IntentKind.FILE_MOVE: DomainKind.FILESYSTEM,
    IntentKind.FILE_COPY: DomainKind.FILESYSTEM,
    IntentKind.FILE_DELETE: DomainKind.FILESYSTEM,
    IntentKind.WINDOW_MANAGE: DomainKind.DESKTOP,
    IntentKind.QUERY_STATUS: DomainKind.OBSERVATION,
    IntentKind.UI_CLICK_TARGET: DomainKind.APPLICATION,
    IntentKind.UI_DOUBLE_CLICK_TARGET: DomainKind.APPLICATION,
    IntentKind.UI_RIGHT_CLICK_TARGET: DomainKind.APPLICATION,
    IntentKind.BROWSER_NAVIGATE: DomainKind.BROWSER,
    IntentKind.BROWSER_CLICK_TARGET: DomainKind.BROWSER,
    IntentKind.BROWSER_TYPE_TARGET: DomainKind.BROWSER,
    IntentKind.BROWSER_EXTRACT_TEXT: DomainKind.BROWSER,
    IntentKind.CANCEL_TASK: DomainKind.OBSERVATION,
    IntentKind.NO_OP: DomainKind.OBSERVATION,
}


@dataclass(frozen=True)
class DomainStep:
    """A single step in a :class:`CrossDomainPlan`.

    A :class:`DomainStep` is a *projection* of one
    :class:`core.orchestration.PlanStep` that the cross-domain
    helper has already tagged with its domain, its
    cross-domain precondition, and the safety policy tags that
    apply.  The executor still uses the canonical
    :class:`core.orchestration.Plan`; this dataclass is the
    planner's view.

    Attributes
    ----------
    domain_step_id:
        Stable id used as the precondition handle.  The
        :class:`CrossDomainPlan` issues these; consumers can
        reference them in a later step's ``depends_on_domain_step_id``.
    intent_kind:
        The originating :class:`IntentKind`.  The cross-domain
        helper never invents an intent kind.
    domain:
        The :class:`DomainKind` the step lives in.
    parameters:
        The intent's parameters, projected through the helper.
    depends_on_domain_step_id:
        A prior ``domain_step_id`` this step needs to be completed
        first.  Optional; the executor can also use the canonical
        ``PlanStep.depends_on`` for finer-grained ordering.
    safety_tags:
        Closed set of safety tags the executor must enforce.  The
        cross-domain helper stamps these from the domain — e.g.
        ``{"dangerous"}`` for ``FILE_DELETE``,
        ``{"network"}`` for ``BROWSER_NAVIGATE``.
    description:
        Human-readable label.
    """

    domain_step_id: str
    intent_kind: IntentKind
    domain: DomainKind
    parameters: Tuple[Tuple[str, Any], ...] = ()
    depends_on_domain_step_id: Optional[str] = None
    safety_tags: Tuple[str, ...] = ()
    description: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": "DomainStep",
            "domain_step_id": self.domain_step_id,
            "intent_kind": self.intent_kind.value,
            "domain": self.domain.value,
            "parameters": [
                {"key": k, "value": v} for k, v in self.parameters
            ],
            "depends_on_domain_step_id": self.depends_on_domain_step_id,
            "safety_tags": list(self.safety_tags),
            "description": self.description,
        }


@dataclass(frozen=True)
class CrossDomainPlan:
    """A composed, domain-tagged plan.

    The :class:`CrossDomainPlan` is what the cross-domain helper
    produces.  It is a *value* — the executor reads it to enrich
    each :class:`PlanStep` with the domain tag and the safety
    tags.  After enrichment, the executor walks the canonical
    :class:`core.orchestration.Plan`; the cross-domain helper is
    not on the hot path.
    """

    plan_id: str
    steps: Tuple[DomainStep, ...] = ()
    domains_used: Tuple[DomainKind, ...] = ()
    notes: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": "CrossDomainPlan",
            "plan_id": self.plan_id,
            "steps": [s.to_dict() for s in self.steps],
            "domains_used": [d.value for d in self.domains_used],
            "notes": self.notes,
            "metadata": dict(self.metadata),
        }


# ---------------------------------------------------------------------------
# Safety-tag policy per domain + intent kind.
# ---------------------------------------------------------------------------
# The cross-domain helper uses this to stamp every step with the
# safety tags the executor must enforce.  The tags are a closed set
# the executor knows about — Phase 14 keeps them small (network,
# filesystem-mutating, dangerous, requires-grant, requires-vision).
_DEFAULT_SAFETY_TAGS: Dict[IntentKind, Tuple[str, ...]] = {
    IntentKind.FILE_DELETE: ("filesystem-mutating", "dangerous"),
    IntentKind.FILE_MOVE: ("filesystem-mutating",),
    IntentKind.FILE_COPY: ("filesystem-mutating",),
    IntentKind.BROWSER_NAVIGATE: ("network", "browser"),
    IntentKind.BROWSER_CLICK_TARGET: ("browser",),
    IntentKind.BROWSER_TYPE_TARGET: ("browser",),
    IntentKind.BROWSER_EXTRACT_TEXT: ("browser",),
    IntentKind.UI_CLICK_TARGET: ("requires-vision",),
    IntentKind.UI_DOUBLE_CLICK_TARGET: ("requires-vision",),
    IntentKind.UI_RIGHT_CLICK_TARGET: ("requires-vision",),
}


def _short_id() -> str:
    return uuid.uuid4().hex[:12]


def compose_cross_domain_plan(
    *,
    intent_kinds: Sequence[IntentKind],
    parameters: Optional[Sequence[Dict[str, Any]]] = None,
    plan_id: Optional[str] = None,
    notes: str = "",
) -> CrossDomainPlan:
    """Compose a :class:`CrossDomainPlan` from a sequence of intent kinds.

    Each intent kind is projected into a :class:`DomainStep` with
    its domain, safety tags, and parameters.  ``parameters[i]`` is
    matched to ``intent_kinds[i]``; if the caller omits it, the
    corresponding step gets an empty parameter set (the planner
    will fill in the details when it turns the cross-domain plan
    into a :class:`core.orchestration.Plan`).

    The function is **deterministic**: the same inputs always
    produce the same ``plan_id``-bearing plan (the helper still
    generates a uuid for traceability; the *shape* is what is
    deterministic).
    """
    if not intent_kinds:
        raise ValueError(
            "compose_cross_domain_plan requires at least one intent kind"
        )

    parameters = parameters or [{} for _ in intent_kinds]
    if len(parameters) != len(intent_kinds):
        raise ValueError(
            "compose_cross_domain_plan: parameters length "
            f"({len(parameters)}) must match intent_kinds length "
            f"({len(intent_kinds)})"
        )

    domains_seen: List[DomainKind] = []
    steps: List[DomainStep] = []
    for idx, kind in enumerate(intent_kinds):
        domain = _INTENT_KIND_TO_DOMAIN.get(kind)
        if domain is None:
            raise ValueError(
                f"compose_cross_domain_plan: intent kind {kind!r} has no "
                f"domain mapping; refusing to invent one."
            )
        if domain not in domains_seen:
            domains_seen.append(domain)
        params = parameters[idx] or {}
        step_id = f"dstep_{idx + 1}_{_short_id()}"
        # The default cross-domain dependency is "the previous
        # step in the plan" — Phase 14 §3 demands tight ordering
        # for cross-domain workflows.  Callers may override later
        # by re-building the plan with explicit dependencies.
        depends_on = steps[-1].domain_step_id if steps else None
        tags = _DEFAULT_SAFETY_TAGS.get(kind, ())
        steps.append(
            DomainStep(
                domain_step_id=step_id,
                intent_kind=kind,
                domain=domain,
                parameters=tuple(sorted(params.items())),
                depends_on_domain_step_id=depends_on,
                safety_tags=tags,
                description=f"{kind.value} (domain={domain.value})",
            )
        )
    return CrossDomainPlan(
        plan_id=plan_id or f"cdp_{_short_id()}",
        steps=tuple(steps),
        domains_used=tuple(domains_seen),
        notes=notes or "composed by compose_cross_domain_plan",
    )


__all__ = [
    "DomainKind",
    "DomainStep",
    "CrossDomainPlan",
    "compose_cross_domain_plan",
]
