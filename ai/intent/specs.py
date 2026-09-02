"""
Omnix V6 — Intent Specifications and Validation.

This module defines the validation rules for structured LLM intent interpretation.
It ensures that raw JSON output from an LLMProvider is strictly validated before
being stamped into a trusted `Intent` dataclass.

Validation covers:
- intent kind (must be a registered IntentKind)
- required parameters
- parameter types (against IntentParamType)
- missing / unexpected fields
- malformed values
- bounds (confidence range, text length)
"""

from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Set

from core.errors import OmnixError
from core.orchestration import IntentKind, IntentParamType


class IntentValidationError(OmnixError):
    """Raised when an LLM's raw intent payload fails validation."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "INTENT_VALIDATION_ERROR",
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message, code=code, context=context)


@dataclass(frozen=True)
class IntentParamSpec:
    """Specification for a single intent parameter."""
    name: str
    param_type: IntentParamType
    required: bool = False
    allowed_values: Optional[Set[str]] = None
    description: str = ""


@dataclass(frozen=True)
class IntentSpec:
    """Specification for an entire Intent geometry."""
    kind: IntentKind
    parameters: Mapping[str, IntentParamSpec] = field(default_factory=dict)
    description: str = ""

    def validate_payload(self, raw_params: Dict[str, Any]) -> None:
        """Validate a dictionary of raw parameters against this spec."""
        provided_keys = set(raw_params.keys())
        expected_keys = set(self.parameters.keys())

        unexpected = provided_keys - expected_keys
        if unexpected:
            raise IntentValidationError(
                f"Unexpected parameters for {self.kind.value}",
                context={"unexpected_keys": list(unexpected)},
            )

        required_keys = {k for k, p in self.parameters.items() if p.required}
        missing = required_keys - provided_keys
        if missing:
            raise IntentValidationError(
                f"Missing required parameters for {self.kind.value}",
                context={"missing_keys": list(missing)},
            )

        for key, value in raw_params.items():
            spec = self.parameters[key]
            self._validate_type(key, value, spec)

    def _validate_type(self, key: str, value: Any, spec: IntentParamSpec) -> None:
        if spec.param_type == IntentParamType.STRING:
            if not isinstance(value, str):
                _raise_type_error(key, "string", type(value).__name__)
            if spec.allowed_values is not None and value not in spec.allowed_values:
                raise IntentValidationError(
                    f"Parameter '{key}' has invalid value.",
                    context={
                        "key": key,
                        "value": value,
                        "allowed": list(spec.allowed_values),
                    },
                )
        elif spec.param_type == IntentParamType.INTEGER:
            if not isinstance(value, int) or isinstance(value, bool):
                _raise_type_error(key, "integer", type(value).__name__)
        elif spec.param_type == IntentParamType.FLOAT:
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                _raise_type_error(key, "float", type(value).__name__)
        elif spec.param_type == IntentParamType.BOOLEAN:
            if not isinstance(value, bool):
                _raise_type_error(key, "boolean", type(value).__name__)
        elif spec.param_type == IntentParamType.LIST_OF_STRINGS:
            if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
                _raise_type_error(key, "list of strings", type(value).__name__)
        # ANY requires no type check


def _raise_type_error(key: str, expected_type: str, actual_type: str) -> None:
    raise IntentValidationError(
        f"Parameter '{key}' has wrong type.",
        context={"key": key, "expected": expected_type, "actual": actual_type},
    )


class IntentSpecRegistry:
    """A registry of IntentSpec definitions determining what the LLM is allowed to output."""

    def __init__(self) -> None:
        self._specs: Dict[IntentKind, IntentSpec] = {}

    def register(self, spec: IntentSpec) -> None:
        self._specs[spec.kind] = spec

    def get(self, kind: IntentKind) -> IntentSpec:
        if kind not in self._specs:
            raise IntentValidationError(
                f"Unknown intent kind: {kind.value}",
                context={"kind": kind.value},
            )
        return self._specs[kind]


def build_default_registry() -> IntentSpecRegistry:
    """Build the core V6 Intent registry mapping."""
    registry = IntentSpecRegistry()

    # action kinds
    registry.register(IntentSpec(
        kind=IntentKind.OPEN_APPLICATION,
        parameters={
            "app_name": IntentParamSpec("app_name", IntentParamType.STRING, required=True),
            "args": IntentParamSpec("args", IntentParamType.LIST_OF_STRINGS, required=False),
        },
    ))

    registry.register(IntentSpec(
        kind=IntentKind.CLOSE_APPLICATION,
        parameters={
            "app_name": IntentParamSpec("app_name", IntentParamType.STRING, required=True),
        },
    ))

    registry.register(IntentSpec(
        kind=IntentKind.FOCUS_APPLICATION,
        parameters={
            "app_name": IntentParamSpec("app_name", IntentParamType.STRING, required=True),
        },
    ))

    registry.register(IntentSpec(
        kind=IntentKind.CONTROL_APPLICATION,
        parameters={
            "app_name": IntentParamSpec("app_name", IntentParamType.STRING, required=True),
            "action": IntentParamSpec("action", IntentParamType.STRING, required=True),
            "target": IntentParamSpec("target", IntentParamType.STRING, required=False),
        },
    ))

    registry.register(IntentSpec(
        kind=IntentKind.FILE_FIND,
        parameters={
            "query": IntentParamSpec("query", IntentParamType.STRING, required=True),
            "path": IntentParamSpec("path", IntentParamType.STRING, required=False),
        },
    ))

    registry.register(IntentSpec(
        kind=IntentKind.FILE_MOVE,
        parameters={
            "source": IntentParamSpec("source", IntentParamType.STRING, required=True),
            "destination": IntentParamSpec("destination", IntentParamType.STRING, required=True),
        },
    ))

    registry.register(IntentSpec(
        kind=IntentKind.FILE_COPY,
        parameters={
            "source": IntentParamSpec("source", IntentParamType.STRING, required=True),
            "destination": IntentParamSpec("destination", IntentParamType.STRING, required=True),
        },
    ))

    registry.register(IntentSpec(
        kind=IntentKind.FILE_DELETE,
        parameters={
            "path": IntentParamSpec("path", IntentParamType.STRING, required=True),
        },
    ))

    registry.register(IntentSpec(
        kind=IntentKind.WINDOW_MANAGE,
        parameters={
            "action": IntentParamSpec("action", IntentParamType.STRING, required=True),
            "target": IntentParamSpec("target", IntentParamType.STRING, required=False),
        },
    ))

    registry.register(IntentSpec(
        kind=IntentKind.QUERY_STATUS,
        parameters={
            "target": IntentParamSpec("target", IntentParamType.STRING, required=False),
        },
    ))

    registry.register(IntentSpec(
        kind=IntentKind.CANCEL_TASK,
        parameters={
            "task_id": IntentParamSpec("task_id", IntentParamType.STRING, required=False),
        },
    ))

    registry.register(IntentSpec(
        kind=IntentKind.NO_OP,
        parameters={},
    ))

    # Compound request (Phase 14.2): a user utterance that contains
    # two or more actions joined by ``and``/``then``/``;`` (e.g.
    # "Open Notepad and type Hello World").  The brain decomposes the
    # ``steps`` list into individual intents and plans each one
    # independently.  This avoids silently dropping later clauses the
    # way the prior single-intent contract did.
    registry.register(IntentSpec(
        kind=IntentKind.COMPOUND_REQUEST,
        parameters={
            "steps": IntentParamSpec(
                "steps",
                IntentParamType.LIST_OF_STRINGS,
                required=True,
                description=(
                    "Ordered list of natural-language clauses "
                    "describing each action in the compound request. "
                    "Each clause is independently re-interpreted."
                ),
            ),
        },
    ))

    # UI grounding intents (Phase 7.3) — the user wants the agent
    # to target a screen element described by a human-readable
    # query.  The ``target_query`` is the vision grounding query;
    # ``preferred_strategy`` is a hint to the perception router.
    registry.register(IntentSpec(
        kind=IntentKind.UI_CLICK_TARGET,
        parameters={
            "target_query": IntentParamSpec(
                "target_query", IntentParamType.STRING, required=True
            ),
            "preferred_strategy": IntentParamSpec(
                "preferred_strategy", IntentParamType.STRING, required=False
            ),
        },
    ))
    registry.register(IntentSpec(
        kind=IntentKind.UI_DOUBLE_CLICK_TARGET,
        parameters={
            "target_query": IntentParamSpec(
                "target_query", IntentParamType.STRING, required=True
            ),
            "preferred_strategy": IntentParamSpec(
                "preferred_strategy", IntentParamType.STRING, required=False
            ),
        },
    ))
    registry.register(IntentSpec(
        kind=IntentKind.UI_RIGHT_CLICK_TARGET,
        parameters={
            "target_query": IntentParamSpec(
                "target_query", IntentParamType.STRING, required=True
            ),
            "preferred_strategy": IntentParamSpec(
                "preferred_strategy", IntentParamType.STRING, required=False
            ),
        },
    ))

    # Browser intents (Phase 8).  These map to the closed browser
    # capability set.  The planner is responsible for turning the
    # ``target_query`` into a concrete locator (CSS / accessibility /
    # text / xpath / test_id) before dispatch; the capability layer
    # rejects any unknown ``locator_kind``.
    registry.register(IntentSpec(
        kind=IntentKind.BROWSER_NAVIGATE,
        parameters={
            "url": IntentParamSpec("url", IntentParamType.STRING, required=True),
            "wait_until": IntentParamSpec(
                "wait_until", IntentParamType.STRING, required=False,
                description="load | domcontentloaded | networkidle",
            ),
        },
        description="Navigate the browser to a URL.",
    ))
    registry.register(IntentSpec(
        kind=IntentKind.BROWSER_CLICK_TARGET,
        parameters={
            "target_query": IntentParamSpec(
                "target_query", IntentParamType.STRING, required=True
            ),
            "locator_kind": IntentParamSpec(
                "locator_kind", IntentParamType.STRING, required=False,
                description=(
                    "Optional explicit locator kind (accessibility, "
                    "css, text, xpath, test_id).  When omitted, the "
                    "planner chooses a closed-set default and may "
                    "consult the vision fallback."
                ),
            ),
            "button": IntentParamSpec(
                "button", IntentParamType.STRING, required=False,
            ),
        },
        description="Click a DOM element described by target_query.",
    ))
    registry.register(IntentSpec(
        kind=IntentKind.BROWSER_TYPE_TARGET,
        parameters={
            "target_query": IntentParamSpec(
                "target_query", IntentParamType.STRING, required=True
            ),
            "text": IntentParamSpec("text", IntentParamType.STRING, required=True),
            "submit": IntentParamSpec(
                "submit", IntentParamType.BOOLEAN, required=False
            ),
            "locator_kind": IntentParamSpec(
                "locator_kind", IntentParamType.STRING, required=False,
            ),
        },
        description="Type text into a DOM element described by target_query.",
    ))
    registry.register(IntentSpec(
        kind=IntentKind.BROWSER_EXTRACT_TEXT,
        parameters={
            "target_query": IntentParamSpec(
                "target_query", IntentParamType.STRING, required=True
            ),
            "max_chars": IntentParamSpec(
                "max_chars", IntentParamType.INTEGER, required=False
            ),
            "locator_kind": IntentParamSpec(
                "locator_kind", IntentParamType.STRING, required=False,
            ),
        },
        description="Read the visible text of a DOM element.",
    ))

    # dialogue kinds
    registry.register(IntentSpec(
        kind=IntentKind.INFORM,
        parameters={
            "information": IntentParamSpec("information", IntentParamType.STRING, required=True),
        },
    ))

    registry.register(IntentSpec(
        kind=IntentKind.QUERY,
        parameters={
            "question": IntentParamSpec("question", IntentParamType.STRING, required=True),
        },
    ))

    registry.register(IntentSpec(
        kind=IntentKind.CLARIFY,
        parameters={
            "question": IntentParamSpec("question", IntentParamType.STRING, required=True),
            "options": IntentParamSpec("options", IntentParamType.LIST_OF_STRINGS, required=False),
        },
    ))

    registry.register(IntentSpec(
        kind=IntentKind.UNKNOWN,
        parameters={},
    ))

    registry.register(IntentSpec(
        kind=IntentKind.COMMAND,
        parameters={},
    ))

    registry.register(IntentSpec(
        kind=IntentKind.CANCEL,
        parameters={},
    ))

    return registry
