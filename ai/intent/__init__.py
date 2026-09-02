"""
Omnix V6 — Natural-Language Intent Interpreter (Phase 5B).

This package provides the structured-intent layer that sits between the
Phase 5A LLMProvider seam and the Phase 5C Planner:

    User text
      -> IntentInterpreter
      -> validated Intent
      -> (future) Goal
      -> (future) Planner
      -> (future) Plan -> ActionRequest -> (engine seam)

The interpreter is forbidden from importing any Windows automation,
process control, or capability-execution surface.  The validation layer
rejects LLM output that does not conform to the V6 Intent schema.
"""

from .interpreter import IntentResult, LLMIntentInterpreter
from .specs import (
    IntentParamSpec,
    IntentSpec,
    IntentSpecRegistry,
    IntentValidationError,
    build_default_registry,
)
from .validation import (
    MAX_INTENT_TEXT_LENGTH,
    MAX_NORMALIZED_OBJECTIVE_LENGTH,
    validate_intent_payload,
)

__all__ = [
    "IntentParamSpec",
    "IntentSpec",
    "IntentSpecRegistry",
    "IntentValidationError",
    "build_default_registry",
    "MAX_INTENT_TEXT_LENGTH",
    "MAX_NORMALIZED_OBJECTIVE_LENGTH",
    "validate_intent_payload",
    "IntentResult",
    "LLMIntentInterpreter",
]
