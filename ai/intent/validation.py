"""
Omnix V6 — Payload validation for raw LLM intent JSON.

The ``validate_intent_payload`` function is the single gate that turns
arbitrary provider output into a trusted ``Intent`` value.  It performs
schema-level validation (delegated to the matching ``IntentSpec``) and
also enforces global bounds such as maximum text length and the
confidence range.
"""

from typing import Any, Dict, Mapping, Optional, Tuple

from core.orchestration import Intent, IntentKind

from .specs import IntentSpecRegistry, IntentValidationError

#: Maximum number of characters allowed in the raw user text.
MAX_INTENT_TEXT_LENGTH: int = 4096

#: Maximum number of characters allowed in the normalized objective.
MAX_NORMALIZED_OBJECTIVE_LENGTH: int = 512

#: Inclusive lower bound for confidence values.
MIN_CONFIDENCE: float = 0.0

#: Inclusive upper bound for confidence values.
MAX_CONFIDENCE: float = 1.0


def _coerce_kind(raw_kind: Any) -> IntentKind:
    """Convert a raw ``kind`` field to an :class:`IntentKind` enum member."""
    if isinstance(raw_kind, IntentKind):
        return raw_kind
    if not isinstance(raw_kind, str):
        raise IntentValidationError(
            "Intent 'kind' must be a string.",
            context={"actual_type": type(raw_kind).__name__},
        )
    try:
        return IntentKind(raw_kind)
    except ValueError as exc:
        valid = [k.value for k in IntentKind]
        raise IntentValidationError(
            f"Unknown intent kind: {raw_kind!r}",
            context={"requested": raw_kind, "valid": valid},
        ) from exc


def _mint_intent_id() -> str:
    """A short, deterministic-looking id for an Intent created in this process."""
    import uuid
    return f"int_{uuid.uuid4().hex[:12]}"


def validate_intent_payload(
    payload: Mapping[str, Any],
    registry: IntentSpecRegistry,
    *,
    max_text_length: int = MAX_INTENT_TEXT_LENGTH,
    max_objective_length: int = MAX_NORMALIZED_OBJECTIVE_LENGTH,
) -> Intent:
    """Validate a raw LLM payload and return a trusted :class:`Intent`.

    Args:
        payload: A mapping parsed from provider output.  Must include at
            minimum the fields ``kind`` and ``parameters``.
        registry: The :class:`IntentSpecRegistry` that defines the legal
            shapes.
        max_text_length: Inclusive upper bound on ``source_text`` length.
        max_objective_length: Inclusive upper bound on the
            normalized ``text`` length.

    Returns:
        A fully-validated :class:`Intent` instance.

    Raises:
        IntentValidationError: if the payload fails any rule.
    """
    if not isinstance(payload, Mapping):
        raise IntentValidationError(
            "Payload must be a mapping.",
            context={"actual_type": type(payload).__name__},
        )

    if "kind" not in payload:
        raise IntentValidationError(
            "Payload missing 'kind' field.",
        )

    kind = _coerce_kind(payload["kind"])
    spec = registry.get(kind)

    # 1) Schema-level parameter validation
    raw_params = payload.get("parameters", {})
    if raw_params is None:
        raw_params = {}
    if not isinstance(raw_params, dict):
        raise IntentValidationError(
            "'parameters' must be a dict.",
            context={"actual_type": type(raw_params).__name__},
        )
    spec.validate_payload(raw_params)

    # 2) Dialogue kind is optional but, when present, must be valid.
    # Phase 11.6: free-tier chat models routinely enrich `dialogue_kind`
    # with a non-canonical subtype (e.g. "greeting", "request",
    # "statement").  The action ``kind`` is already strictly validated
    # above; the dialogue layer is a speech-act hint and is safely
    # derived from the action kind.  When the model supplies a value
    # that is not a closed ``IntentKind`` member, treat it as if the
    # field were absent and let the auto-derivation produce a valid
    # value.  This is the only field for which V6 accepts a non-canonical
    # payload entry without rejecting the whole Intent.
    dialogue_kind_raw = payload.get("dialogue_kind")
    dialogue_kind: Optional[IntentKind] = None
    if dialogue_kind_raw is not None:
        try:
            dialogue_kind = _coerce_kind(dialogue_kind_raw)
        except IntentValidationError:
            # Non-canonical value.  Fall back to the auto-derivation
            # below; do not raise.  This branch is reached only when
            # the model emits a speech-act subtype that is not a
            # member of the closed IntentKind enum.
            dialogue_kind = None
    if dialogue_kind is None:
        # Default: treat action kinds as commands, dialogue-only as themselves.
        if kind in (
            IntentKind.INFORM,
            IntentKind.QUERY,
            IntentKind.CLARIFY,
            IntentKind.UNKNOWN,
            IntentKind.CANCEL,
        ):
            dialogue_kind = kind
        else:
            dialogue_kind = IntentKind.COMMAND

    # 3) Text / objective bounds.
    text = payload.get("objective") or payload.get("text") or ""
    if text is None:
        text = ""
    if not isinstance(text, str):
        raise IntentValidationError(
            "'objective'/'text' must be a string.",
            context={"actual_type": type(text).__name__},
        )
    if len(text) > max_objective_length:
        raise IntentValidationError(
            "'objective' exceeds maximum length.",
            context={
                "length": len(text),
                "max": max_objective_length,
            },
        )

    # 4) Source-text bounds.
    source_text = payload.get("source_text", "")
    if source_text is None:
        source_text = ""
    if not isinstance(source_text, str):
        raise IntentValidationError(
            "'source_text' must be a string.",
            context={"actual_type": type(source_text).__name__},
        )
    if len(source_text) > max_text_length:
        raise IntentValidationError(
            "'source_text' exceeds maximum length.",
            context={
                "length": len(source_text),
                "max": max_text_length,
            },
        )

    # 5) Confidence range.
    confidence = payload.get("confidence", 1.0)
    if confidence is None:
        confidence = 1.0
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
        raise IntentValidationError(
            "'confidence' must be a number.",
            context={"actual_type": type(confidence).__name__},
        )
    if not (MIN_CONFIDENCE <= float(confidence) <= MAX_CONFIDENCE):
        raise IntentValidationError(
            "'confidence' out of range.",
            context={
                "value": float(confidence),
                "min": MIN_CONFIDENCE,
                "max": MAX_CONFIDENCE,
            },
        )

    # 6) Optional fields: referenced_entities, referenced_goal_id, constraints, metadata.
    referenced_entities = payload.get("referenced_entities", ())
    if referenced_entities is None:
        referenced_entities = ()
    if not isinstance(referenced_entities, (list, tuple)) or not all(
        isinstance(e, str) for e in referenced_entities
    ):
        raise IntentValidationError(
            "'referenced_entities' must be a list of strings.",
        )

    referenced_goal_id = payload.get("referenced_goal_id")
    if referenced_goal_id is not None and not isinstance(referenced_goal_id, str):
        raise IntentValidationError(
            "'referenced_goal_id' must be a string when present.",
        )

    # Intent.constraints is Tuple[str, ...]; accept strings or a tuple/list.
    raw_constraints = payload.get("constraints", ())
    if raw_constraints is None:
        raw_constraints = ()
    if isinstance(raw_constraints, str):
        constraints_tuple: Tuple[str, ...] = (raw_constraints,)
    elif isinstance(raw_constraints, (list, tuple)):
        if not all(isinstance(c, str) for c in raw_constraints):
            raise IntentValidationError(
                "'constraints' entries must be strings.",
            )
        constraints_tuple = tuple(raw_constraints)
    else:
        raise IntentValidationError(
            "'constraints' must be a string, list, or tuple.",
        )

    metadata = payload.get("metadata", {})
    if metadata is None:
        metadata = {}
    if not isinstance(metadata, dict):
        raise IntentValidationError(
            "'metadata' must be a dict.",
        )

    return Intent(
        intent_id=_mint_intent_id(),
        kind=kind,
        dialogue_kind=dialogue_kind,
        text=text,
        parameters=dict(raw_params),
        confidence=float(confidence),
        referenced_entities=tuple(referenced_entities),
        referenced_goal_id=referenced_goal_id,
        source_text=source_text,
        constraints=constraints_tuple,
        metadata=dict(metadata),
    )

