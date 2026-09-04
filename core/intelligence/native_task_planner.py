import logging
import uuid
from typing import Any, Dict, List, Optional, Mapping

from core.orchestration import Goal, Intent, IntentKind, Plan, PlanStep, ActionKind, ExpectedEffect
from core.intelligence.capability_resolver import CapabilityResolver
from ai.brain.exceptions import CannotPlanError

logger = logging.getLogger(__name__)

class NativeTaskPlanner:
    """The Native Task Planner (Phase 22).
    
    Produces deterministic TaskPlans by resolving intents through the CapabilityResolver.
    Supports hybrid plans (calling AI for generation, then using the result natively).
    """
    
    name = "NativeTaskPlanner"

    def __init__(self, resolver: CapabilityResolver) -> None:
        self.resolver = resolver

    def plan(
        self,
        goal: Goal,
        *,
        intent: Optional[Intent] = None,
        context_snapshot: Optional[Dict[str, Any]] = None,
        prior_plan: Optional[Plan] = None,
        **kwargs: Any,
    ) -> Plan:
        if not intent:
            raise CannotPlanError("Native planner requires an Intent.")

        if intent.kind == IntentKind.COMPOUND_REQUEST:
            return self._plan_compound(goal, intent)

        return self._plan_single(goal, intent)

    def _plan_single(self, goal: Goal, intent: Intent) -> Plan:
        if intent.kind == IntentKind.GENERATE_CONTENT:
            # Generate and type out immediately
            gen_step_id = f"step_gen_{uuid.uuid4().hex[:8]}"
            gen_step = PlanStep(
                step_id=gen_step_id,
                description=f"AI Generation: {intent.text}",
                action=ActionKind.CAPABILITY_CALL,
                capability_name="ai.generate",
                parameters={"prompt": intent.text}
            )

            type_step_id = f"step_type_{uuid.uuid4().hex[:8]}"
            type_step = PlanStep(
                step_id=type_step_id,
                description="Type generated content",
                action=ActionKind.CAPABILITY_CALL,
                capability_name="desktop.keyboard.type",
                parameters={"text": f"{{{{ctx.steps.{gen_step_id}.generated_content}}}}", "target": "current"},
                depends_on=(gen_step_id,)
            )
            
            return Plan(
                plan_id=f"plan_{uuid.uuid4().hex[:8]}",
                goal_id=goal.goal_id,
                steps=(gen_step, type_step),
                metadata={"planner": self.name}
            )

        resolution = self.resolver.resolve(intent)
        
        if not resolution:
            raise CannotPlanError(f"No deterministic native capability handles intent: {intent.kind}")
            
        cap_name, param_overrides, expected_effect_dict = resolution
        
        # Merge intent parameters with overrides
        final_params = dict(intent.parameters or {})
        final_params.update(param_overrides)
        
        expected_effect = None
        if expected_effect_dict:
            expected_effect = ExpectedEffect(**expected_effect_dict)

        step = PlanStep(
            step_id=f"step_{uuid.uuid4().hex[:8]}",
            description=intent.text,
            action=ActionKind.CAPABILITY_CALL,
            capability_name=cap_name,
            parameters=final_params,
            expected_effect=expected_effect,
        )

        return Plan(
            plan_id=f"plan_{uuid.uuid4().hex[:8]}",
            goal_id=goal.goal_id,
            steps=(step,),
            metadata={"planner": self.name}
        )

    def _plan_compound(self, goal: Goal, intent: Intent) -> Plan:
        steps_text = intent.parameters.get("steps", [])
        if not steps_text:
            raise CannotPlanError("Compound intent missing 'steps'.")

        plan_steps = []
        prev_step_id = None
        
        # Track active app for subsequent typing/clicks
        active_target = None

        for idx, clause in enumerate(steps_text):
            # Extremely basic sub-classification for the clause
            sub_intent = self._classify_clause(clause, active_target)
            
            # Hybrid task detection: If the sub-intent requires AI code generation
            # (e.g. "write a Python calculator")
            if self._is_hybrid_generation(clause, sub_intent):
                # Generate step
                gen_step_id = f"step_{idx}_gen_{uuid.uuid4().hex[:8]}"
                gen_step = PlanStep(
                    step_id=gen_step_id,
                    description=f"AI Generation: {clause}",
                    action=ActionKind.CAPABILITY_CALL,
                    capability_name="ai.generate",
                    parameters={"prompt": clause},
                    depends_on=(prev_step_id,) if prev_step_id else ()
                )
                plan_steps.append(gen_step)
                prev_step_id = gen_step_id

                # Type out the generated content
                type_step_id = f"step_{idx}_type_{uuid.uuid4().hex[:8]}"
                type_step = PlanStep(
                    step_id=type_step_id,
                    description=f"Type generated content",
                    action=ActionKind.CAPABILITY_CALL,
                    capability_name="desktop.keyboard.type",
                    # This relies on task executor resolving context variables
                    parameters={"text": f"{{{{ctx.steps.{gen_step_id}.generated_content}}}}", "target": active_target or "current"},
                    depends_on=(prev_step_id,)
                )
                plan_steps.append(type_step)
                prev_step_id = type_step_id
                continue

            # Native path
            resolution = self.resolver.resolve(sub_intent)
            if not resolution:
                raise CannotPlanError(f"Cannot resolve capability for clause: {clause}")
            
            cap_name, param_overrides, expected_effect_dict = resolution
            final_params = dict(sub_intent.parameters or {})
            final_params.update(param_overrides)
            
            # Track active app focus
            if cap_name in ("desktop.application.open", "desktop.application.focus"):
                active_target = final_params.get("app_name")

            expected_effect = None
            if expected_effect_dict:
                expected_effect = ExpectedEffect(**expected_effect_dict)

            step_id = f"step_{idx}_{uuid.uuid4().hex[:8]}"
            step = PlanStep(
                step_id=step_id,
                description=clause,
                action=ActionKind.CAPABILITY_CALL,
                capability_name=cap_name,
                parameters=final_params,
                expected_effect=expected_effect,
                depends_on=(prev_step_id,) if prev_step_id else ()
            )
            plan_steps.append(step)
            prev_step_id = step_id

        return Plan(
            plan_id=f"plan_{uuid.uuid4().hex[:8]}",
            goal_id=goal.goal_id,
            steps=tuple(plan_steps),
            metadata={"planner": self.name}
        )

    def _classify_clause(self, clause: str, active_target: Optional[str]) -> Intent:
        from core.intelligence.native_intent_interpreter import NativeIntentInterpreter
        interpreter = NativeIntentInterpreter()
        result = interpreter.interpret(clause)
        if result.status == "ok" and result.intent:
            intent = result.intent
            # Carry forward implicit target for typing
            if intent.kind == IntentKind.CONTROL_APPLICATION and "target" not in (intent.parameters or {}):
                if active_target:
                    intent = intent.with_parameter("app_name", active_target)
            return intent
        # Fallback to NO_OP if parsing fails, but CapabilityResolver will likely fail
        return Intent(intent_id=str(uuid.uuid4()), kind=IntentKind.NO_OP, text=clause)

    def _is_hybrid_generation(self, clause: str, intent: Intent) -> bool:
        return intent.kind == IntentKind.GENERATE_CONTENT
