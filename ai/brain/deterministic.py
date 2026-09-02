"""
Omnix V6 — Deterministic planner (Phase 5C+5D).

A rule-based planner used for:

    * tests that do not want an LLM in the loop;
    * development mode where no real provider is configured;
    * a stable, auditable fallback when the LLM provider is offline.

The planner takes a :class:`Goal` and (optionally) the originating
:class:`Intent` and produces a :class:`Plan` using a small set of
heuristic rules keyed off the goal's ``metadata["normalized_objective"]``
and ``metadata["intent_kind"]``.

It is *deterministic* by construction:

    * the same goal + intent + registry always produces the same plan;
    * the planner never calls a provider, never imports an LLM, never
      invents a capability name;
    * the planner never executes a step; it only returns a plan.

Architectural isolation:

    This module MUST NOT import:

        * :mod:`subprocess`
        * :mod:`pyautogui`
        * :mod:`win32gui` / :mod:`win32api`
        * :mod:`ctypes`
        * :mod:`core.capability_router`
        * :mod:`core.omnix_engine`
        * any V6 *Windows service* (e.g. ``system.windows.*``,
          ``system.applications.*``)
        * any V6 *AI provider* (e.g. ``ai.provider.*``)

    Tests in ``tests/test_brain_isolation.py`` enforce this.
"""

from __future__ import annotations

import re
import time
import uuid
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from core.capability import CapabilitySpec
from core.capability_registry import CapabilityRegistry
from core.orchestration import (
    ActionKind,
    Failure,
    Goal,
    Intent,
    IntentKind,
    Plan,
    PlanStatus,
    PlanStep,
)

from .exceptions import CannotPlanError
from .validation import validate_plan_payload


# ---------------------------------------------------------------------------
# Heuristic rule table
# ---------------------------------------------------------------------------

# Each rule maps an intent kind (the semantic action) to a list of
# capability *templates*.  The template is a dict with a "name" key
# that must resolve in the registry at plan time, plus optional
# "param_overrides" (a Mapping[str, Any] | callable) and "depends_on"
# (an int = "the n-th prior step").

# Notes:
#   * The capability names are the *real* V6 names from the canonical
#     CapabilityRegistry (e.g. ``desktop.application.open``).  The
#     deterministic planner refuses to invent a capability name; if a
#     name is not in the registry, it returns :class:`CannotPlanError`.
#   * Rules are intentionally small.  The LLM planner is the general
#     case; this planner is a safety net for tests and dev mode.

# Phase 14.1: every ``desktop.application.*`` rule ships a
# non-null ``expected_effect`` so the step-level verifier in
# :class:`DefaultStepVerifier` has a check to run.  Without this,
# the verifier short-circuits the step and the goal verifier
# receives an empty step-verdicts list, which it conservatively
# classifies as ``UNCERTAIN``.  The recovery engine then triggers
# replan, eventually producing ``CLARIFICATION_REQUIRED`` even
# though the capability executed successfully.
#
# The check names match the verification blocks emitted by the
# real capabilities in :mod:`applications.desktop`:
#   * ``app_launched`` — desktop.application.open reports this
#   * ``app_closed``   — desktop.application.close reports this
#   * ``app_focused``  — desktop.application.focus reports this
_APP_OPEN_EFFECT: Dict[str, Any] = {
    "check_name": "app_launched",
    "expected": True,
    "timeout_s": 30.0,
    "description": "the named application process is running",
}
_APP_CLOSE_EFFECT: Dict[str, Any] = {
    "check_name": "app_closed",
    "expected": True,
    "timeout_s": 15.0,
    "description": "the named application process has exited",
}
_APP_FOCUS_EFFECT: Dict[str, Any] = {
    "check_name": "app_focused",
    "expected": True,
    "timeout_s": 5.0,
    "description": "the named application is the foreground window",
}


_DEFAULT_RULES: Dict[str, List[Dict[str, Any]]] = {
    "open_application": [
        {
            "name": "desktop.application.open",
            "param_overrides": {},
            "expected_effect": _APP_OPEN_EFFECT,
        },
    ],
    "close_application": [
        {
            "name": "desktop.application.close",
            "param_overrides": {},
            "expected_effect": _APP_CLOSE_EFFECT,
        },
    ],
    "focus_application": [
        {
            "name": "desktop.application.focus",
            "param_overrides": {},
            "expected_effect": _APP_FOCUS_EFFECT,
        },
    ],
    "control_application": [
        # The deterministic planner cannot introspect the "action"
        # verb at this layer (it depends on the intent's
        # parameters), so it emits a single step that uses the
        # generic "desktop.application.is_running" capability if
        # registered; otherwise it falls back to a planning failure.
        # The LLM planner handles the fine-grained mapping.
        #
        # Phase 14.2: ``action="type"`` is special-cased here so the
        # compound-request path can drive ``desktop.keyboard.type``
        # directly without going through the LLM planner.  The
        # intent's ``target`` parameter is forwarded as the text to
        # type; ``app_name`` is forwarded unchanged so the keyboard
        # capability can route the input to the right window.
        # This is a generic mechanism — any other ``action`` value
        # still falls through to the conservative ``is_running``
        # step (and the LLM planner) below.
        {
            "name": "desktop.keyboard.type",
            "param_overrides": {},
            "when_action": "type",
            "required_capability": "desktop.keyboard.type",
        },
        # Conservative fallback for any non-typing control action.
        {"name": "desktop.application.is_running", "param_overrides": {}},
    ],
    "file_find": [
        {"name": "file.read", "param_overrides": {}},
    ],
    "file_move": [
        # Phase 12: file.move / file.copy are still placeholder
        # mappings; the LLM planner is the canonical place to
        # produce a multi-step move/copy.  The deterministic planner
        # degrades to file.read so the LLM path is the single
        # source of truth for these intents.
        {"name": "file.read", "param_overrides": {}},
    ],
    "file_copy": [
        {"name": "file.read", "param_overrides": {}},
    ],
    "file_delete": [
        # Phase 12: real delete via the dangerous file.delete
        # capability.  The safety layer must still authorise the
        # call; the deterministic planner only declares the
        # *intent* to delete, never bypasses the gate.
        {
            "name": "file.delete",
            "param_overrides": {},
            "requires_intent_params": ("path",),
        },
    ],
    "window_manage": [
        {"name": "desktop.window.list", "param_overrides": {}},
    ],
    "query_status": [
        {"name": "desktop.foreground_window", "param_overrides": {}},
    ],
    "ui_click_target": [
        # A target-bearing click — the deterministic planner emits
        # the proper planner → vision contract so the Agent must
        # consult vision before dispatching.  The
        # ``vision_target_query`` is read from
        # ``intent.parameters["target_query"]`` at plan time; the
        # ``vision_preferred_strategy`` is read from
        # ``intent.parameters["preferred_strategy"]`` when present.
        {
            "name": "desktop.mouse.click",
            "param_overrides": {},
            "vision_from_intent": True,
            "vision_pre_action": "click",
        },
    ],
    "ui_double_click_target": [
        {
            "name": "desktop.mouse.double_click",
            "param_overrides": {},
            "vision_from_intent": True,
            "vision_pre_action": "double_click",
        },
    ],
    "ui_right_click_target": [
        {
            "name": "desktop.mouse.right_click",
            "param_overrides": {},
            "vision_from_intent": True,
            "vision_pre_action": "right_click",
        },
    ],
    # Phase 8: browser intents.  The deterministic planner never
    # invents a concrete locator — if the intent does not supply both
    # ``locator_kind`` and ``locator_value``, planning fails and the
    # caller falls back to the LLM planner.  This is the same
    # discipline as the vision rules: the planner is a safety net, not
    # a guesser.
    "browser_navigate": [
        {
            "name": "browser.navigate",
            "param_overrides": {},
            "requires_intent_params": ("url",),
        },
    ],
    "browser_click_target": [
        {
            "name": "browser.click",
            "param_overrides": {},
            "requires_intent_params": ("locator_kind", "locator_value"),
        },
    ],
    "browser_type_target": [
        {
            "name": "browser.type",
            "param_overrides": {},
            "requires_intent_params": (
                "locator_kind", "locator_value", "text"
            ),
        },
    ],
    "browser_extract_text": [
        {
            "name": "browser.extract_text",
            "param_overrides": {},
            "requires_intent_params": ("locator_kind", "locator_value"),
        },
    ],
    "cancel_task": [
        # The deterministic planner does not have enough context to
        # construct a cancel step; it produces a planning failure so
        # the orchestrator surfaces the request to the user.
    ],
    "no_op": [
        # A no-op plan: zero work.  The Brain layer handles this
        # case specially (returns a NO_OP plan with one observed
        # step) without ever going through this table.
    ],
    "compound_request": [
        # The deterministic planner does NOT know how to flatten a
        # compound request into a single capability call — it dispatches
        # ``DeterministicPlanner.plan`` to ``_plan_compound_request``
        # which decomposes the ``steps`` list clause-by-clause.
        # The entry here exists only so the rule lookup never returns
        # ``None`` for the compound kind.  See :meth:`plan`.
    ],
}


# ---------------------------------------------------------------------------
# Vision metadata helpers (Phase 7.3)
# ---------------------------------------------------------------------------

_VISION_TARGET_KEY = "target_query"
_VISION_PREFERRED_KEY = "preferred_strategy"


class DeterministicPlanner:
    """A rule-based planner used in tests and dev mode.

    The planner is read-only with respect to the world: it never
    calls a provider and never executes a step.  The output is a
    validated :class:`Plan`.
    """

    def __init__(
        self,
        registry: CapabilityRegistry,
        *,
        rules: Optional[Dict[str, List[Dict[str, Any]]]] = None,
        name: str = "deterministic",
    ) -> None:
        if registry is None:
            raise ValueError("DeterministicPlanner requires a CapabilityRegistry")
        if not isinstance(registry, CapabilityRegistry):
            raise TypeError(
                "DeterministicPlanner expected a CapabilityRegistry, "
                f"got {type(registry).__name__}"
            )
        self.registry = registry
        self.rules: Dict[str, List[Dict[str, Any]]] = (
            dict(rules) if rules is not None else {k: [dict(t) for t in v] for k, v in _DEFAULT_RULES.items()}
        )
        self.name = str(name)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def plan(
        self,
        goal: Goal,
        *,
        intent: Optional[Intent] = None,
        context_snapshot: Optional[Dict[str, Any]] = None,
        prior_plan: Optional[Plan] = None,
        failure: Optional[Failure] = None,
    ) -> Plan:
        """Return a deterministic :class:`Plan` for ``goal``.

        The plan is built by:

            1.  Reading ``goal.metadata["intent_kind"]`` (preferred)
                or the ``prior_plan.metadata["intent_kind"]`` (for
                replans).  If neither is present, fall back to the
                goal description and try to map it heuristically.

            2.  Looking up the matching rule in
                :data:`_DEFAULT_RULES`.

            3.  Projecting each template into a :class:`PlanStep` by
                merging the rule's ``param_overrides`` with the
                intent's ``parameters``.

            4.  Validating the assembled payload through
                :func:`validate_plan_payload` so the output is a
                *trusted* plan (the same gate the LLM planner uses).

        Raises:
            CannotPlanError: The intent kind has no deterministic
                rule, the rule's capability is not in the registry,
                or the assembled payload is invalid.
        """
        if not isinstance(goal, Goal):
            raise TypeError(
                f"DeterministicPlanner.plan expected a Goal, "
                f"got {type(goal).__name__}"
            )

        intent_kind = self._resolve_intent_kind(goal, intent)
        if intent_kind is None:
            raise CannotPlanError(
                "Cannot infer an intent kind from the goal.",
                context={"goal_id": goal.goal_id},
            )

        # Phase 14.2: compound requests are decomposed by the planner
        # into one step per clause.  We re-classify each clause against
        # the single-intent pattern table, build a sub-intent, then
        # expand the per-intent rule.  This keeps the planner the
        # single source of truth for "what does the user want?" and
        # means a new intent kind only needs a single rule — the
        # compound path reuses it transparently.
        if intent_kind == IntentKind.COMPOUND_REQUEST:
            return self._plan_compound_request(
                goal=goal,
                intent=intent,
                context_snapshot=context_snapshot,
                prior_plan=prior_plan,
            )

        rule = self.rules.get(intent_kind)
        if rule is None:
            raise CannotPlanError(
                f"Deterministic planner has no rule for intent kind {intent_kind!r}.",
                context={"intent_kind": intent_kind, "goal_id": goal.goal_id},
            )
        if not rule:
            # Empty rule list (e.g. cancel_task) is a planning failure.
            raise CannotPlanError(
                f"Deterministic planner cannot plan for intent kind {intent_kind!r}.",
                context={"intent_kind": intent_kind, "goal_id": goal.goal_id},
            )

        # Build a synthetic payload shaped like an LLM output.
        intent_params: Dict[str, Any] = {}
        if intent is not None:
            intent_params = dict(intent.parameters)
        # Strip planner-only fields from the parameters forwarded to
        # the capability layer.  ``target_query`` and
        # ``preferred_strategy`` are planner → vision metadata, NOT
        # capability parameters.  Keeping them in ``parameters`` would
        # make them fail the capability's parameter spec.
        capability_params: Dict[str, Any] = {
            k: v
            for k, v in intent_params.items()
            if k not in (_VISION_TARGET_KEY, _VISION_PREFERRED_KEY)
        }
        steps_payload: List[Dict[str, Any]] = []
        # Phase 14.2: when a rule carries ``when_action`` discriminators,
        # at most one of them may fire (the matching one).  All
        # non-discriminated templates (those without ``when_action``)
        # are included unconditionally.  This keeps the deterministic
        # rule table expressive — e.g. ``control_application`` has
        # one ``action="type"`` template and one conservative default,
        # and exactly one of them runs per intent.
        has_action_branch = any(
            isinstance(t, dict) and t.get("when_action") is not None
            for t in rule
        )
        action_branch_matched = False
        for idx, template in enumerate(rule):
            when_action = template.get("when_action")
            if when_action is not None:
                current_action = intent_params.get("action")
                if not isinstance(current_action, str) or current_action.strip().lower() != str(when_action).strip().lower():
                    continue
                action_branch_matched = True
            elif has_action_branch and action_branch_matched:
                # We have already picked the matching action branch
                # in this rule; the conservative default is skipped
                # for this intent.  This is what makes the action
                # discriminator mutually exclusive with the fallback.
                continue
            cap_name = str(template.get("name", "")).strip()
            if not cap_name:
                raise CannotPlanError(
                    f"Rule for {intent_kind!r} has no capability name at step {idx}.",
                    context={"intent_kind": intent_kind, "index": idx},
                )
            if self.registry.get(cap_name) is None:
                raise CannotPlanError(
                    f"Rule for {intent_kind!r} references unknown capability {cap_name!r}.",
                    context={"intent_kind": intent_kind, "capability": cap_name},
                )
            overrides = dict(template.get("param_overrides", {}) or {})
            merged: Dict[str, Any] = {**capability_params, **overrides}
            # Phase 14.2: the keyboard.type capability expects a
            # ``text`` parameter, but the intent layer models the
            # payload as ``target`` (a generic term shared with
            # other control actions).  Translate here so the
            # capability's strict parameter spec passes without
            # the LLM planner having to massage the payload.
            if cap_name == "desktop.keyboard.type" and "text" not in merged:
                text_value = merged.get("target")
                if isinstance(text_value, str) and text_value.strip():
                    merged["text"] = text_value
            # Strip fields the capability's parameter spec does not
            # accept — the strict validator rejects anything extra.
            cap_obj = self.registry.get(cap_name)
            allowed_names: set = set()
            if cap_obj is not None:
                spec_obj = getattr(cap_obj, "spec", None)
                spec_params = getattr(spec_obj, "parameters", None) if spec_obj is not None else None
                # Spec.parameters may be a Mapping of name → param
                # (canonical for inline dict specs) or a list of
                # CapabilityParameter (canonical for class-defined
                # specs).  Normalise.
                if isinstance(spec_params, Mapping):
                    allowed_names = set(spec_params.keys())
                elif isinstance(spec_params, (list, tuple, set)):
                    for p in spec_params:
                        name = getattr(p, "name", None)
                        if name:
                            allowed_names.add(name)
            if allowed_names:
                merged = {k: v for k, v in merged.items() if k in allowed_names}
            step_payload: Dict[str, Any] = {
                "step_id": f"step_{idx + 1}",
                "description": _step_description(intent_kind, cap_name, idx),
                "action": ActionKind.CAPABILITY_CALL.value,
                "capability_name": cap_name,
                "parameters": merged,
            }
            if "depends_on" in template:
                step_payload["depends_on"] = list(template["depends_on"])
            if "timeout_s" in template:
                step_payload["timeout_s"] = float(template["timeout_s"])
            if "expected_effect" in template:
                step_payload["expected_effect"] = dict(template["expected_effect"])

            # Phase 7.3: project the planner → vision contract into
            # step metadata when the template declares it.  The
            # deterministic planner never invents a ``vision_target_query``;
            # it must be supplied via the intent (the only user-facing
            # surface for the rule).  Validation rejects unknown kinds
            # and contradictory declarations.
            step_metadata: Dict[str, Any] = {}
            if template.get("vision_from_intent"):
                pre_action = template.get("vision_pre_action")
                if pre_action:
                    step_metadata["vision_pre_action"] = str(pre_action)
                target_query = intent_params.get(_VISION_TARGET_KEY)
                if not isinstance(target_query, str) or not target_query.strip():
                    raise CannotPlanError(
                        f"Rule for {intent_kind!r} at step {idx} requires a "
                        f"non-empty 'target_query' in the intent parameters; "
                        f"the deterministic planner never invents one.",
                        context={
                            "intent_kind": intent_kind,
                            "index": idx,
                            "capability": cap_name,
                        },
                    )
                step_metadata["vision_target_query"] = target_query
                preferred = intent_params.get(_VISION_PREFERRED_KEY)
                if isinstance(preferred, str) and preferred:
                    step_metadata["vision_preferred_strategy"] = preferred
            if step_metadata:
                step_payload["metadata"] = step_metadata

            # Phase 8: deterministic planner refuses to invent missing
            # intent parameters.  When a rule declares
            # ``requires_intent_params``, every name listed must be
            # present and non-empty in the intent's parameters.
            required_params = template.get("requires_intent_params") or ()
            if required_params:
                missing = [
                    name for name in required_params
                    if name not in intent_params
                    or (
                        isinstance(intent_params[name], str)
                        and not str(intent_params[name]).strip()
                    )
                ]
                if missing:
                    raise CannotPlanError(
                        f"Rule for {intent_kind!r} at step {idx} is missing "
                        f"required intent parameters: {missing!r}.  The "
                        f"deterministic planner never invents intent "
                        f"parameters; the LLM planner should be used for "
                        f"this goal.",
                        context={
                            "intent_kind": intent_kind,
                            "index": idx,
                            "capability": cap_name,
                            "missing": list(missing),
                        },
                    )
            steps_payload.append(step_payload)

        payload: Dict[str, Any] = {
            "goal_id": goal.goal_id,
            "steps": steps_payload,
        }
        return validate_plan_payload(
            payload,
            registry=self.registry,
            plan_id=f"plan_{_short_id()}",
            goal_id=goal.goal_id,
            parent_plan_id=prior_plan.plan_id if prior_plan is not None else None,
            replan_count=goal.metadata.get("replan_count", 0) if isinstance(goal.metadata, dict) else 0,
            notes="deterministic-planner",
            metadata={"planner": self.name, "intent_kind": intent_kind},
        )

    # ------------------------------------------------------------------
    # Compound-request decomposition (Phase 14.2)
    # ------------------------------------------------------------------
    def _plan_compound_request(
        self,
        *,
        goal: Goal,
        intent: Optional[Intent],
        context_snapshot: Optional[Dict[str, Any]] = None,
        prior_plan: Optional[Plan] = None,
    ) -> Plan:
        """Decompose a ``compound_request`` intent into N sub-plans.

        Each clause in ``intent.parameters["steps"]`` is re-classified
        by running it back through the same rule table the Brain uses
        for single intents.  The result is a single :class:`Plan`
        whose steps are the union of all per-clause steps in
        topological order (each later step ``depends_on`` the
        immediately-prior step so the executor cannot skip ahead).

        We deliberately do NOT hard-code any app names or
        application-specific templates.  The decomposition reuses
        ``self.rules`` for every clause, so any new intent kind that
        gains a rule is automatically composable inside a compound
        request.
        """
        if intent is None:
            raise CannotPlanError(
                "Compound request planning requires an Intent with a "
                "'steps' parameter; intent was None.",
                context={"goal_id": goal.goal_id},
            )
        steps = intent.parameters.get("steps") if isinstance(intent.parameters, dict) else None
        if not isinstance(steps, list) or not steps:
            raise CannotPlanError(
                "Compound request has no 'steps' parameter; cannot plan.",
                context={
                    "goal_id": goal.goal_id,
                    "intent_kind": intent.kind.value,
                    "parameters": dict(intent.parameters or {}),
                },
            )
        clauses = [str(s).strip() for s in steps if isinstance(s, str) and str(s).strip()]
        if not clauses:
            raise CannotPlanError(
                "Compound request 'steps' parameter is empty after normalisation.",
                context={"goal_id": goal.goal_id},
            )

        # Build sub-goals for each clause.  We attach a per-clause
        # intent_kind on the goal metadata so the recursive call
        # routes correctly.  The recursive ``plan()`` is the same
        # path used for every other intent — the compound kind
        # never bypasses the rule table.
        # We also carry forward an "implicit target" — the most
        # recent ``app_name`` from an earlier clause — so a clause
        # like "type Hello World" inherits the focus of "Open
        # Notepad" without hard-coding any specific compound.
        implicit_target: Optional[str] = None
        sub_plans: List[Plan] = []
        for clause_idx, clause in enumerate(clauses):
            sub_intent_kind = self._classify_clause(clause)
            if sub_intent_kind is None:
                raise CannotPlanError(
                    f"Compound request clause #{clause_idx + 1} could not "
                    f"be classified: {clause!r}.",
                    context={
                        "goal_id": goal.goal_id,
                        "clause": clause,
                        "clause_index": clause_idx,
                    },
                )
            # Re-parse the clause so the sub-intent carries the
            # structured parameters the per-kind rule needs (e.g.
            # ``app_name`` for ``open_application``).  Without this
            # the recursive ``plan`` call would receive an empty
            # intent.parameters dict and the validation layer would
            # reject the step for missing required parameters.
            sub_intent = self._build_sub_intent(
                clause=clause,
                intent_kind=sub_intent_kind,
                clause_idx=clause_idx,
                implicit_target=implicit_target,
            )
            # Update the carry-forward target after each clause.
            if isinstance(sub_intent.parameters, dict):
                maybe_app = sub_intent.parameters.get("app_name")
                if isinstance(maybe_app, str) and maybe_app.strip():
                    implicit_target = maybe_app.strip()
            sub_goal = Goal(
                goal_id=f"{goal.goal_id}_clause_{clause_idx + 1}",
                description=clause,
                success_criteria=(
                    f"clause {clause_idx + 1} of compound request "
                    f"({sub_intent_kind}) completed and verified"
                ),
                metadata={
                    "intent_kind": sub_intent_kind,
                    "source_text": clause,
                    "compound_parent_goal_id": goal.goal_id,
                    "clause_index": clause_idx,
                },
            )
            sub_plans.append(
                self.plan(
                    sub_goal,
                    intent=sub_intent,
                    context_snapshot=context_snapshot,
                    prior_plan=prior_plan,
                )
            )

        # Stitch the per-clause plans into a single flat plan.  Each
        # later clause's steps declare ``depends_on`` the previous
        # clause's first step — the executor already has a topological
        # runner, so this is the minimal way to make compound
        # execution sequential.
        merged_steps: List[Dict[str, Any]] = []
        for clause_idx, sub_plan in enumerate(sub_plans):
            clause_first_step_id: Optional[str] = None
            for step_idx, step in enumerate(sub_plan.steps):
                # Rename step ids to keep them globally unique inside
                # the parent goal.
                new_id = f"step_{len(merged_steps) + 1}"
                step_dict = step.to_dict() if hasattr(step, "to_dict") else dict(step)
                step_dict["step_id"] = new_id
                # Renumber depends_on references for cross-clause
                # dependencies.  This is conservative: only step_1
                # in each clause (i.e. the entry capability) depends
                # on the previous clause; deeper steps inside the
                # clause keep their intra-clause deps.
                if clause_first_step_id is None:
                    clause_first_step_id = new_id
                    if clause_idx > 0:
                        step_dict["depends_on"] = [
                            f"step_{len(merged_steps)}"  # previous step
                        ]
                merged_steps.append(step_dict)

        payload: Dict[str, Any] = {
            "goal_id": goal.goal_id,
            "steps": merged_steps,
        }
        return validate_plan_payload(
            payload,
            registry=self.registry,
            plan_id=f"plan_{_short_id()}",
            goal_id=goal.goal_id,
            parent_plan_id=prior_plan.plan_id if prior_plan is not None else None,
            replan_count=goal.metadata.get("replan_count", 0) if isinstance(goal.metadata, dict) else 0,
            notes="deterministic-planner:compound",
            metadata={
                "planner": self.name,
                "intent_kind": IntentKind.COMPOUND_REQUEST.value,
                "clause_count": len(clauses),
                "clause_intent_kinds": [
                    self._classify_clause(c) for c in clauses
                ],
            },
        )

    @staticmethod
    def _classify_clause(clause: str) -> Optional[str]:
        """Map a single compound clause to an intent kind string.

        Mirrors the smart_mock_responder's classification so the
        planner and the LLM layer agree on what each clause means.
        The mapping is closed-set — unknown clauses are a planning
        failure rather than a guess.  This is deliberately narrow:
        a clause that doesn't fit the closed set is reported as
        unclassifiable and the orchestrator surfaces a clarification.
        """
        text = (clause or "").strip()
        if not text:
            return None
        lower = text.lower()
        # Single-verb app commands.
        if re.match(r"^\s*(open|launch|start)\b", lower):
            return IntentKind.OPEN_APPLICATION.value
        if re.match(r"^\s*(close|quit|exit)\b", lower):
            return IntentKind.CLOSE_APPLICATION.value
        if re.match(r"^\s*(focus|switch\s+to)\b", lower):
            return IntentKind.FOCUS_APPLICATION.value
        # Typing / keyboard input — drive via the generic control
        # action.  The deterministic rule for ``control_application``
        # is conservative (single ``is_running`` step) so this path
        # will be the one the LLM planner replaces; the deterministic
        # planner's job is only to surface that the request is a
        # multi-step plan, not to invent the exact typing call.
        if re.match(r"^\s*(type|enter|input|write|send\s+keys)\b", lower):
            return IntentKind.CONTROL_APPLICATION.value
        # Browser navigation.
        if re.match(r"^\s*(navigate\s+to|go\s+to|visit|open\s+url)\b", lower):
            return IntentKind.BROWSER_NAVIGATE.value
        # Generic fallback for unrecognised clauses.  We do not guess;
        # the orchestrator surfaces a clarification instead of
        # silently swallowing the clause.
        return None

    @staticmethod
    def _build_sub_intent(
        *,
        clause: str,
        intent_kind: str,
        clause_idx: int,
        implicit_target: Optional[str] = None,
    ) -> Intent:
        """Construct the per-clause :class:`Intent` for compound planning.

        The sub-intent must carry the same structured parameters the
        rule table expects for that kind (e.g. ``app_name`` for
        ``open_application``).  We re-parse the clause text with the
        closed-set regexes from :meth:`_classify_clause`; the verb is
        dropped, the remainder becomes the parameter value.

        ``implicit_target`` is the most-recent ``app_name`` from an
        earlier clause.  It is used as a fallback when a clause does
        not name a target itself — e.g. "type Hello World" after
        "Open Notepad" inherits Notepad as the focused application.
        This is the generic carry-forward mechanism, not a hard-coded
        special case for any specific app.
        """
        text = (clause or "").strip()
        lower = text.lower()
        # The verb-to-param mapping mirrors ``_classify_clause``.
        verb_re = re.compile(
            r"^\s*(?:open|launch|start|close|quit|exit|focus|switch\s+to|"
            r"navigate\s+to|go\s+to|visit|open\s+url|"
            r"type|enter|input|write|send\s+keys)\b\s*",
            re.IGNORECASE,
        )
        target_match = verb_re.match(text)
        target = text[target_match.end():].strip().rstrip(".,!?") if target_match else text
        # Carry-forward: if the clause has no target of its own
        # (e.g. "type" alone, or "type Hello World" — which leaves
        # "Hello World" as the payload, not an app), fall back to
        # the implicit target from the most recent app clause.
        if not target and implicit_target:
            target = implicit_target
        if not target:
            raise CannotPlanError(
                f"Compound request clause #{clause_idx + 1} is missing "
                f"a target after the verb: {clause!r}.",
                context={"clause": clause, "clause_index": clause_idx},
            )

        # Build the parameters dict for the specific intent kind.
        try:
            kind_enum = IntentKind(intent_kind)
        except ValueError:
            raise CannotPlanError(
                f"Compound request clause #{clause_idx + 1} classified "
                f"to unknown intent kind {intent_kind!r}.",
                context={"clause": clause, "intent_kind": intent_kind},
            )

        if kind_enum in (
            IntentKind.OPEN_APPLICATION,
            IntentKind.CLOSE_APPLICATION,
            IntentKind.FOCUS_APPLICATION,
        ):
            params: Dict[str, Any] = {"app_name": target}
        elif kind_enum == IntentKind.CONTROL_APPLICATION:
            # For control actions like "type Hello World", the
            # payload (after the verb) is the *action argument*,
            # NOT the app name.  The app is the implicit target
            # from the previous clause.  This generic carry-forward
            # is what makes "Open X and type Y" mean "type Y into
            # X" without hard-coding any specific app.
            params = {
                "app_name": implicit_target or target,
                "action": "type",
                "target": target if implicit_target else "",
            }
        elif kind_enum == IntentKind.BROWSER_NAVIGATE:
            params = {"url": target}
        else:
            params = {"target": target}

        return Intent(
            intent_id=f"subintent_{clause_idx + 1}",
            kind=kind_enum,
            text=text,
            parameters=params,
            confidence=0.9,
            source_text=text,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_intent_kind(
        goal: Goal,
        intent: Optional[Intent],
    ) -> Optional[str]:
        if intent is not None:
            return intent.kind.value
        meta = goal.metadata if isinstance(goal.metadata, dict) else {}
        ik = meta.get("intent_kind")
        if isinstance(ik, str) and ik:
            return ik
        # Heuristic: scan the goal description for known verbs.
        desc = (goal.description or "").lower()
        for key in (
            "open_application",
            "close_application",
            "focus_application",
            "control_application",
            "file_find",
            "file_move",
            "file_copy",
            "file_delete",
            "window_manage",
            "query_status",
            "browser_navigate",
            "browser_click_target",
            "browser_type_target",
            "browser_extract_text",
            "cancel_task",
            "no_op",
        ):
            if key.replace("_", " ") in desc:
                return key
        return None


def _step_description(intent_kind: str, cap_name: str, idx: int) -> str:
    if idx == 0:
        return f"{intent_kind}: call {cap_name}"
    return f"{intent_kind} (cont. {idx}): call {cap_name}"


def _short_id() -> str:
    return uuid.uuid4().hex[:12]
