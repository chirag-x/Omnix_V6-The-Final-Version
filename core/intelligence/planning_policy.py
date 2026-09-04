import re
from typing import Optional, Tuple
from core.orchestration import Intent, IntentKind

class PlanningPolicy:
    """Policy for Native-First Intelligence (Phase 22)."""

    def classify_intent(self, intent: Intent) -> str:
        """Classifies an intent into NATIVE, HYBRID, AI_REQUIRED, UNSUPPORTED, or AMBIGUOUS."""
        kind = intent.kind

        if kind == IntentKind.COMPOUND_REQUEST:
            # Check if any step requires AI
            steps = intent.parameters.get("steps", [])
            from core.intelligence.native_intent_interpreter import NativeIntentInterpreter
            interpreter = NativeIntentInterpreter()
            
            requires_ai = False
            for step_text in steps:
                sub_res = interpreter.interpret(str(step_text))
                if sub_res.status == "ok" and sub_res.intent and sub_res.intent.kind == IntentKind.GENERATE_CONTENT:
                    requires_ai = True
                    break
            
            return "HYBRID" if requires_ai else "NATIVE"

        # Single intents
        if kind == IntentKind.GENERATE_CONTENT:
            # Single generate intent is handled natively via _plan_single now (it inserts an AI step + a type step)
            # So the intent interpreter should return it as NATIVE or HYBRID, allowing NativeTaskPlanner to handle it
            return "HYBRID"

        if kind == IntentKind.NO_OP:
            return "AI_REQUIRED"

        # Deterministic things
        if kind in (
            IntentKind.OPEN_APPLICATION,
            IntentKind.CLOSE_APPLICATION,
            IntentKind.FOCUS_APPLICATION,
            IntentKind.CONTROL_APPLICATION,
            IntentKind.UI_CLICK_TARGET,
            IntentKind.FILE_FIND,
            IntentKind.FILE_DELETE,
            IntentKind.WINDOW_MANAGE,
            IntentKind.QUERY_STATUS,
            IntentKind.BROWSER_NAVIGATE
        ):
            return "NATIVE"

        return "AMBIGUOUS"

    def requires_ai_escalation(self, intent: Intent) -> bool:
        return self.classify_intent(intent) in ("HYBRID", "AI_REQUIRED")
