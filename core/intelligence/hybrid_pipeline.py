import logging
from typing import Any, Dict, Optional
from core.orchestration.interfaces import IntentInterpreter, Planner
from core.orchestration.models import Goal, Intent, Plan
from ai.intent.interpreter import IntentResult
from ai.brain.exceptions import CannotPlanError
from core.intelligence.native_intent_interpreter import NativeIntentInterpreter
from core.intelligence.native_task_planner import NativeTaskPlanner
from core.intelligence.planning_policy import PlanningPolicy

logger = logging.getLogger(__name__)

class HybridIntentInterpreter:
    def __init__(self, native_interpreter: NativeIntentInterpreter, llm_interpreter: IntentInterpreter):
        self.name = "HybridIntentInterpreter"
        self.native = native_interpreter
        self.llm = llm_interpreter
        self.policy = PlanningPolicy()

    def interpret(self, text: str, *, context_snapshot: Optional[Dict[str, Any]] = None) -> IntentResult:
        # First attempt native classification
        native_result = self.native.interpret(text, context_snapshot=context_snapshot)
        
        if native_result.status == "ok" and native_result.intent:
            classification = self.policy.classify_intent(native_result.intent)
            # If it's fully AI required (e.g. conversational), fallback to LLM intent
            if classification == "AI_REQUIRED":
                logger.info("HybridIntentInterpreter: Intent requires full AI escalation. Routing to LLM Interpreter.")
                return self.llm.interpret(text, context_snapshot=context_snapshot)
            # If NATIVE or HYBRID, we return the native intent!
            return native_result
            
        # If native parsing completely failed or was unknown, fallback
        logger.info("HybridIntentInterpreter: Native parsing failed. Routing to LLM Interpreter.")
        return self.llm.interpret(text, context_snapshot=context_snapshot)


class HybridPlanner:
    def __init__(self, native_planner: NativeTaskPlanner, llm_planner: Planner):
        self.name = "HybridPlanner"
        self.native = native_planner
        self.llm = llm_planner

    def plan(
        self,
        goal: Goal,
        *,
        intent: Optional[Intent] = None,
        context_snapshot: Optional[Dict[str, Any]] = None,
        prior_plan: Optional[Plan] = None,
        **kwargs: Any,
    ) -> Plan:
        # Attempt native planning first
        try:
            plan = self.native.plan(goal, intent=intent, context_snapshot=context_snapshot, prior_plan=prior_plan, **kwargs)
            logger.info("HybridPlanner: Successfully generated native plan.")
            return plan
        except CannotPlanError as e:
            logger.info(f"HybridPlanner: Native planning skipped ({e}). Falling back to LLM Planner.")
            return self.llm.plan(goal, intent=intent, context_snapshot=context_snapshot, prior_plan=prior_plan, **kwargs)
        except Exception as e:
            import traceback
            logger.error(f"HybridPlanner: Exception during native planning: {e}\n{traceback.format_exc()}")
            raise

