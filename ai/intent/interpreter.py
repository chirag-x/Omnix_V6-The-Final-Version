"""
Omnix V6 — LLM-driven IntentInterpreter.

The interpreter turns a chunk of natural-language text into a
validated :class:`core.orchestration.Intent` value.  It does **not**
execute anything.  The actual production rules:

  1.  Build a deterministic, JSON-shaped system prompt from the
      :class:`IntentSpecRegistry`.
  2.  Call the configured :class:`ai.provider.LLMProvider` once.
  3.  Parse the provider's response as JSON.
  4.  Hand the parsed mapping to ``validate_intent_payload``.
  5.  Return a :class:`IntentResult` (always structured — never raise
      to the caller for ordinary "couldn't understand" cases; only
      for internal / configuration failures).

The interpreter must remain isolated from the Windows automation
surface.  See ``tests/test_intent_isolation.py``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional

from ai.provider.base import LLMProvider
from ai.provider.contracts import LLMMessage, LLMRequest, MessageRole, OutputFormat
from ai.provider.errors import ProviderError
from core.orchestration import Intent, IntentKind

from .specs import IntentSpecRegistry, IntentValidationError, build_default_registry
from .validation import validate_intent_payload

_log = logging.getLogger(__name__)


SYSTEM_PROMPT_TEMPLATE = """\
You are the Omnix V6 Intent Interpreter.

You convert natural-language user text into a single, structured JSON
object that conforms to the V6 Intent schema.

You MUST respond with exactly one JSON object.  No prose, no markdown
fences, no commentary.

The JSON object MUST use this shape:

{{
  "kind": "<one of the valid intent kinds below>",
  "dialogue_kind": "<optional dialogue kind, see below>",
  "objective": "<a short, normalized, present-tense statement of the user goal>",
  "parameters": {{ ... }},
  "confidence": <number between 0.0 and 1.0>,
  "source_text": "<the original user text, echoed verbatim>",
  "referenced_entities": [<list of string names the user mentioned>],
  "referenced_goal_id": null,
  "constraints": <list of constraint strings>,
  "metadata": {{ ... }}
}}

# Valid intent kinds
{intent_kinds}

# Per-kind parameter schemas
{param_schemas}

# dialogue_kind (optional, but if present must be one of these EXACT values)
#   inform   - the user is conveying information
#   query    - the user is asking a question
#   command  - the user wants the agent to do something
#   clarify  - the user is asking the agent to clarify
#   cancel   - the user is cancelling a prior request
#   unknown  - could not classify
# Do NOT invent a new dialogue_kind (e.g. "greeting", "request",
# "statement").  When unsure, OMIT the field entirely — the system
# derives a safe default from the action kind.

# examples
# User: "Hello"                -> {{"kind":"inform", "parameters":{{"information":"hello"}}, "objective":"greet", "confidence":0.9}}
# User: "What time is it?"     -> {{"kind":"query",  "parameters":{{"question":"what time is it"}}, "objective":"ask current time", "confidence":0.95}}
# User: "Open Notepad"         -> {{"kind":"open_application", "parameters":{{"app_name":"notepad"}}, "objective":"open notepad", "confidence":0.9}}
# User: "Click the button."    -> {{"kind":"ui_click_target", "parameters":{{"target_query":"button"}}, "objective":"click the button", "confidence":0.7}}
# User: "Open Notepad and type Hello World"
#   -> {{"kind":"compound_request", "parameters":{{"steps":["Open Notepad", "type Hello World"]}}, "objective":"open notepad and type hello world", "confidence":0.9}}
# User: "Open Chrome, then navigate to anthropic.com"
#   -> {{"kind":"compound_request", "parameters":{{"steps":["Open Chrome", "navigate to anthropic.com"]}}, "objective":"open chrome then navigate to anthropic.com", "confidence":0.9}}

# Hard rules
- Never invent fields not listed in the schema.
- Never embed shell commands, screen coordinates, window handles,
  API keys, or executable code in any field.
- If the user's text is ambiguous (e.g. "open it"), respond with
  kind="CLARIFY" and a short clarifying question.
- If you genuinely cannot map the text to a known intent, respond
  with kind="UNKNOWN" and confidence <= 0.3.
- Use semantic kinds (e.g. CONTROL_APPLICATION) over app-specific
  ones whenever the meaning is generic.
- If the user's text contains two or more actions joined by "and",
  "then", "after that", or a semicolon, emit a
  ``compound_request`` intent whose ``steps`` parameter is the
  ordered list of clauses.  Do NOT silently drop the trailing
  clauses; the planner depends on the full ordered list.
"""


@dataclass(frozen=True)
class IntentResult:
    """Structured outcome of an interpretation attempt.

    The interpreter never raises for ordinary "couldn't understand"
    cases — it returns an :class:`IntentResult` whose ``status`` field
    tells the caller what to do next.
    """

    status: str  # "ok" | "clarification" | "unknown" | "error"
    intent: Optional[Intent] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    error_context: Optional[Dict[str, Any]] = None
    clarifying_question: Optional[str] = None

    @property
    def is_ok(self) -> bool:
        return self.status == "ok" and self.intent is not None

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"status": self.status}
        if self.intent is not None:
            out["intent"] = self.intent.to_dict()
        if self.error_code is not None:
            out["error_code"] = self.error_code
        if self.error_message is not None:
            out["error_message"] = self.error_message
        if self.error_context is not None:
            out["error_context"] = self.error_context
        if self.clarifying_question is not None:
            out["clarifying_question"] = self.clarifying_question
        return out


def _build_system_prompt(registry: IntentSpecRegistry) -> str:
    """Render the deterministic system prompt from the registry."""
    kinds = sorted(k.value for k in IntentKind)
    schemas: List[str] = []
    for kind in sorted(registry._specs.keys(), key=lambda k: k.value):  # noqa: SLF001
        spec = registry._specs[kind]  # noqa: SLF001
        if not spec.parameters:
            schemas.append(f"- {kind.value}: (no parameters)")
            continue
        rows = []
        for name, pspec in spec.parameters.items():
            opt = "required" if pspec.required else "optional"
            rows.append(f"    * {name} ({pspec.param_type.value}, {opt})")
        schemas.append(f"- {kind.value}:\n" + "\n".join(rows))
    return SYSTEM_PROMPT_TEMPLATE.format(
        intent_kinds=", ".join(kinds),
        param_schemas="\n".join(schemas),
    )


class LLMIntentInterpreter:
    """An :class:`IntentInterpreter` backed by an :class:`LLMProvider`."""

    def __init__(
        self,
        provider: LLMProvider,
        registry: Optional[IntentSpecRegistry] = None,
        *,
        caller: str = "intent",
        default_timeout_s: Optional[float] = 30.0,
    ) -> None:
        self._provider = provider
        self._registry = registry or build_default_registry()
        self._caller = caller
        self._default_timeout_s = default_timeout_s
        self._system_prompt = _build_system_prompt(self._registry)

    @property
    def system_prompt(self) -> str:
        return self._system_prompt

    @property
    def registry(self) -> IntentSpecRegistry:
        return self._registry

    def interpret(
        self,
        text: str,
        *,
        context_snapshot: Optional[Mapping[str, Any]] = None,
    ) -> IntentResult:
        """Interpret ``text`` and return an :class:`IntentResult`."""
        if not isinstance(text, str):
            return IntentResult(
                status="error",
                error_code="INTENT_INVALID_INPUT",
                error_message="text must be a string",
            )
        if not text.strip():
            return IntentResult(
                status="clarification",
                clarifying_question="I didn't catch that. Could you say it again?",
            )

        request = LLMRequest(
            system=self._system_prompt,
            messages=[LLMMessage(role=MessageRole.USER, content=text)],
            output_format=OutputFormat.JSON,
            model=None,
            temperature=0.0,
            timeout_s=self._default_timeout_s,
            caller=self._caller,
        )

        try:
            response = self._provider.generate(request)
        except ProviderError as exc:
            _log.warning("intent interpreter provider failure: %s", exc.code)
            return IntentResult(
                status="error",
                error_code=exc.code,
                error_message=exc.message,
                error_context=exc.context,
            )

        payload = _parse_json_object(response.content)
        if payload is None:
            return IntentResult(
                status="error",
                error_code="INTENT_MALFORMED_JSON",
                error_message="Provider returned content that is not a JSON object.",
            )

        # Always stamp the source text so downstream layers see the
        # original input even if the model omits it.
        payload.setdefault("source_text", text)

        try:
            intent = validate_intent_payload(payload, self._registry)
        except IntentValidationError as exc:
            return IntentResult(
                status="error",
                error_code=exc.code,
                error_message=exc.message,
                error_context=exc.context,
            )

        # Handle the structured "no decision" outcomes.
        if intent.kind == IntentKind.CLARIFY:
            q = intent.parameters.get("question") or "Could you clarify?"
            return IntentResult(
                status="clarification",
                intent=intent,
                clarifying_question=q,
            )
        if intent.kind == IntentKind.UNKNOWN:
            return IntentResult(status="unknown", intent=intent)

        return IntentResult(status="ok", intent=intent)


def _parse_json_object(content: str) -> Optional[Dict[str, Any]]:
    """Parse provider output as a JSON object.

    Tolerant of leading/trailing whitespace, of markdown code
    fences that the model may have added despite instructions, and
    (Phase 11.6) of a small amount of additional prose wrapping
    the JSON object — e.g. ``"Sure! Here you go: {...}"`` produced
    by free-tier chat models.  In that case we locate the first
    ``{`` and the last matching ``}`` and try to parse the
    resulting substring.  If that also fails, or the result is not
    a dict, the parser fails closed (returns ``None``); the
    interpreter must not accept arbitrary free-form text as a valid
    Intent payload.
    """
    if content is None:
        return None
    text = content.strip()
    if text.startswith("```"):
        # strip code fence
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        # Phase 11.6: model may wrap the JSON in a small amount of
        # additional prose.  Try to extract the first JSON object
        # by locating the first ``{`` and the last ``}`` and parsing
        # that substring.  Fail closed if no object is found.
        first = text.find("{")
        last = text.rfind("}")
        if first == -1 or last == -1 or last <= first:
            return None
        candidate = text[first : last + 1]
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            return None
    if not isinstance(parsed, dict):
        return None
    return parsed
