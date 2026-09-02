"""
Omnix V6 — LLM-based planner (Phase 5C+5D).

The :class:`LLMPlanner` is the *general* planner.  It takes a
:class:`Goal` and the originating :class:`Intent`, builds a
prompt that contains the closed capability surface (the
:class:`CapabilitySummary` list), calls the LLM via the
:class:`LLMProvider`, parses the model's JSON output, and runs it
through :func:`validate_plan_payload` to produce a trusted
:class:`Plan`.

The planner is the *only* place where the model sees capabilities.
It never invents a capability the LLM emits — anything not in the
canonical :class:`CapabilityRegistry` is rejected at validation time.

The LLM planner is read-only with respect to the world: it never
imports a Windows service, the :class:`CapabilityRouter`, or
``subprocess``/``pyautogui``/``win32gui``/``win32api``/``ctypes``.

Architectural isolation (mirrors :mod:`ai.provider`):

    This module MUST NOT import or use any of:

        * :mod:`subprocess`
        * :mod:`pyautogui`
        * :mod:`win32gui` / :mod:`win32api`
        * :mod:`ctypes`
        * :mod:`core.capability_router`
        * :mod:`core.omnix_engine`
        * any V6 *Windows service* (e.g. ``system.windows.*``,
          ``system.applications.*``)

The tests in ``tests/test_brain_isolation.py`` enforce this.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from typing import Any, Dict, List, Optional, Sequence

from core.capability_registry import CapabilityRegistry
from core.orchestration import (
    Failure,
    Goal,
    Intent,
    Plan,
)

from ai.provider import (
    LLMMessage,
    LLMProvider,
    LLMRequest,
    LLMUsage,
    MessageRole,
    OutputFormat,
)
from ai.provider.errors import (
    CancelledError as ProviderCancelledError,
    MalformedResponseError,
    ProviderError,
    TimeoutError_ as ProviderTimeoutError,
)

from .discovery import (
    CapabilitySummary,
    discover_capabilities,
    summarize_for_prompt,
)
from .exceptions import (
    CancelledError,
    ProviderFailure,
    ProviderMalformedResponse,
    ProviderTimeout,
    CannotPlanError,
)
from .validation import (
    MAX_PLAN_STEPS,
    validate_plan_payload,
)


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_PLANNER_MODEL: Optional[str] = None
DEFAULT_PLANNER_TEMPERATURE: float = 0.0
DEFAULT_MAX_TOKENS: int = 2048
DEFAULT_TIMEOUT_S: float = 30.0
DEFAULT_CALLER_TAG: str = "brain.plan"


# Markdown fence stripper.  The LLM is asked to return pure JSON, but
# as a belt-and-braces measure, fences are stripped on the way in.
_MARKDOWN_FENCE = re.compile(
    r"^\s*```(?:json)?\s*|\s*```\s*$",
    re.IGNORECASE | re.MULTILINE,
)


# ---------------------------------------------------------------------------
# LLMPlanner
# ---------------------------------------------------------------------------

class LLMPlanner:
    """An LLM-backed planner.

    The planner holds:

        * a :class:`CapabilityRegistry` (read-only),
        * an :class:`LLMProvider` (the seam to a model),
        * a pre-built system prompt and the current set of
          :class:`CapabilitySummary` records it will send the model.

    The system prompt is built once at construction time from the
    closed capability surface.  This guarantees determinism across
    plan() calls: the same registry produces the same prompt.
    """

    def __init__(
        self,
        provider: LLMProvider,
        registry: CapabilityRegistry,
        *,
        model: Optional[str] = DEFAULT_PLANNER_MODEL,
        temperature: float = DEFAULT_PLANNER_TEMPERATURE,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        max_steps: int = MAX_PLAN_STEPS,
        capability_tags: Optional[Sequence[str]] = None,
        name: str = "llm",
    ) -> None:
        if provider is None or not hasattr(provider, "generate"):
            raise ValueError("LLMPlanner requires an LLMProvider")
        if registry is None or not isinstance(registry, CapabilityRegistry):
            raise TypeError(
                f"LLMPlanner expected a CapabilityRegistry, "
                f"got {type(registry).__name__}"
            )
        self.provider = provider
        self.registry = registry
        self.model = model
        self.temperature = float(temperature)
        self.max_tokens = int(max_tokens)
        self.timeout_s = float(timeout_s)
        self.max_steps = int(max_steps)
        self.capability_tags = (
            tuple(str(t) for t in capability_tags) if capability_tags else None
        )
        self.name = str(name)

        # Pre-build the system prompt and the list of summaries.
        self._summaries: List[CapabilitySummary] = discover_capabilities(
            registry,
            tags=self.capability_tags,
        )
        self._prompt_summaries: List[Dict[str, Any]] = summarize_for_prompt(
            self._summaries
        )
        self.system_prompt: str = self._build_system_prompt(self._prompt_summaries)

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
        if not isinstance(goal, Goal):
            raise TypeError(
                f"LLMPlanner.plan expected a Goal, got {type(goal).__name__}"
            )
        user_prompt = self._build_user_prompt(goal, intent, prior_plan, failure)
        request = LLMRequest(
            messages=(LLMMessage(role=MessageRole.USER, content=user_prompt),),
            system=self.system_prompt,
            output_format=OutputFormat.JSON,
            model=self.model,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            timeout_s=self.timeout_s,
            caller=DEFAULT_CALLER_TAG,
        )
        try:
            response = self.provider.generate(request)
        except ProviderTimeoutError as exc:
            raise ProviderTimeout(
                "LLM provider call timed out while planning.",
                cause=exc,
                context={"code": getattr(exc, "code", "PROVIDER_TIMEOUT")},
            ) from exc
        except ProviderCancelledError as exc:
            raise CancelledError(
                "LLM provider call was cancelled while planning.",
                cause=exc,
                context={"code": getattr(exc, "code", "PROVIDER_CANCELLED")},
            ) from exc
        except MalformedResponseError as exc:
            raise ProviderMalformedResponse(
                "LLM provider returned a malformed response.",
                cause=exc,
                context={"code": getattr(exc, "code", "PROVIDER_MALFORMED_RESPONSE")},
            ) from exc
        except ProviderError as exc:
            raise ProviderFailure(
                "LLM provider call failed.",
                cause=exc,
                context={"code": getattr(exc, "code", "PROVIDER_ERROR")},
            ) from exc

        payload = self._parse_response(response)
        if payload is None:
            # The model emitted something we cannot interpret.  Reject
            # explicitly.  A planner must not invent plans.
            raise CannotPlanError(
                "LLM planner could not extract a plan payload from the response.",
                context={"raw_excerpt": self._excerpt(response)},
            )

        return validate_plan_payload(
            payload,
            registry=self.registry,
            plan_id=f"plan_{_short_id()}",
            goal_id=goal.goal_id,
            parent_plan_id=prior_plan.plan_id if prior_plan is not None else None,
            replan_count=goal.metadata.get("replan_count", 0) if isinstance(goal.metadata, dict) else 0,
            notes="llm-planner",
            metadata={"planner": self.name},
            max_steps=self.max_steps,
        )

    # ------------------------------------------------------------------
    # Prompt construction
    # ------------------------------------------------------------------

    @staticmethod
    def _build_system_prompt(summaries: Sequence[Dict[str, Any]]) -> str:
        """Build a deterministic system prompt from the closed capability set."""
        parts: List[str] = []
        parts.append(
            "You are the Omnix V6 PLANNER.  You turn a structured Goal into a "
            "structured Plan.  You NEVER execute anything and you NEVER call a "
            "model that is not in the closed capability list below.  You respond "
            "with exactly one JSON object.  No prose, no markdown, no commentary."
        )
        parts.append("")
        parts.append("Hard rules:")
        parts.append("- The plan's steps each have an action in {capability_call, observe, verify, wait, ask_user}.")
        parts.append("- Every step that is capability_call MUST reference a capability name that appears in the closed capability list below.  Never invent a name.")
        parts.append("- Every step's parameters MUST match the declared parameter spec of its capability.  Never include shell tokens, screen coordinates, or executable code in any field.")
        parts.append("- The plan's step dependencies MUST be a DAG (no cycles, no self-deps).")
        parts.append("- The plan MUST be achievable with at most 64 steps.")
        parts.append("- A step that claims a dangerous capability MUST keep its safety_classification = 'dangerous' (or omit it).  You may NOT downgrade a dangerous capability to 'safe' or 'reversible'.")
        parts.append("- Do NOT include secrets, API keys, tokens, or passwords in any field.")
        parts.append("- If the goal cannot be expressed with the available capabilities, respond with {\"goal_id\": <id>, \"steps\": []} so the orchestrator can re-prompt with a clarification.")
        parts.append("- Your output is JSON only.  Do not wrap it in prose or markdown fences.")
        parts.append("")
        parts.append("Vision grounding contract (Phase 7.3):")
        parts.append("- Every step that targets the screen (mouse clicks, double-clicks, right-clicks) MUST declare its intent in the step's 'metadata' block.")
        parts.append("- To ground a target: set 'metadata.vision_pre_action' to one of 'click', 'double_click', 'right_click', 'focus', 'type_into' AND set 'metadata.vision_target_query' to a short human-readable description of the target (e.g. 'the Save button').")
        parts.append("- Optionally set 'metadata.vision_preferred_strategy' to one of 'uia', 'ocr', 'visual', 'coordinates' to hint the router.")
        parts.append("- To opt out of vision (only when the coordinates are already known): set 'metadata.vision_skip_grounding = true' AND supply integer 'x' and 'y' in the step's parameters.")
        parts.append("- You MUST NOT combine 'vision_pre_action' with 'vision_skip_grounding=true'; pick one path.")
        parts.append("- Steps that do NOT target the screen (e.g. keyboard.type, file.read, application.open) do not need vision metadata; omit it.")
        parts.append("- A click without vision metadata or an explicit coordinate bypass is REJECTED at the plan boundary.")
        parts.append("")
        parts.append("Closed capability surface (use exactly these names):")
        parts.append(json.dumps(list(summaries), ensure_ascii=False, indent=2))
        return "\n".join(parts)

    @staticmethod
    def _build_user_prompt(
        goal: Goal,
        intent: Optional[Intent],
        prior_plan: Optional[Plan],
        failure: Optional[Failure],
    ) -> str:
        # Render the goal as a small, JSON-shaped block the model can read.
        block: Dict[str, Any] = {
            "goal_id": goal.goal_id,
            "description": goal.description,
            "success_criteria": list(goal.success_criteria),
            "constraints": list(goal.constraints),
            "priority": goal.priority,
            "metadata": dict(goal.metadata),
        }
        if intent is not None:
            block["intent"] = intent.to_dict()
        if prior_plan is not None:
            block["prior_plan"] = prior_plan.to_dict()
        if failure is not None:
            block["failure"] = failure.to_dict()
        return (
            "Produce a Plan for the following Goal.  Respond with a single JSON "
            "object of the shape:\n"
            "  { \"goal_id\": str, \"steps\": [ PlanStep, ... ] }\n"
            "where each PlanStep is:\n"
            "  { \"step_id\": str, \"description\": str,\n"
            "    \"action\": \"capability_call\" | \"observe\" | \"verify\" | \"wait\" | \"ask_user\",\n"
            "    \"capability_name\": str, \"parameters\": object,\n"
            "    \"expected_effect\": { \"check_name\": str, \"expected\": any,\n"
            "                          \"timeout_s\": number, \"description\": str } (optional),\n"
            "    \"depends_on\": [str], \"timeout_s\": number, \"max_retries\": int,\n"
            "    \"metadata\": { \"vision_pre_action\": str?, \"vision_target_query\": str?,\n"
            "                  \"vision_preferred_strategy\": str?, \"vision_skip_grounding\": bool? } }\n\n"
            "  Metadata rules:\n"
            "    - Steps calling a target-bearing capability (mouse click family) MUST\n"
            "      either set 'vision_pre_action' + 'vision_target_query' OR set\n"
            "      'vision_skip_grounding=true' with explicit 'x' and 'y' parameters.\n"
            "    - Other steps may omit the metadata block.\n\n"
            f"Goal:\n{json.dumps(block, ensure_ascii=False, indent=2)}"
        )

    # ------------------------------------------------------------------
    # Response parsing
    # ------------------------------------------------------------------

    def _parse_response(self, response: Any) -> Optional[Dict[str, Any]]:
        """Try every reasonable way to get a plan dict out of the response."""
        # 1) ``response.raw`` may already be a dict.
        raw = getattr(response, "raw", None)
        if isinstance(raw, dict):
            if self._looks_like_plan(raw):
                return raw
        # 2) Parse the content as JSON.
        content = getattr(response, "content", "") or ""
        if not isinstance(content, str):
            return None
        cleaned = _strip_fences(content)
        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError:
            # 3) Try a "first {...} block" extraction as a last resort.
            start = cleaned.find("{")
            end = cleaned.rfind("}")
            if start != -1 and end > start:
                try:
                    data = json.loads(cleaned[start : end + 1])
                except json.JSONDecodeError:
                    return None
            else:
                return None
        if isinstance(data, dict) and self._looks_like_plan(data):
            return data
        return None

    @staticmethod
    def _looks_like_plan(d: Any) -> bool:
        return isinstance(d, dict) and "steps" in d

    @staticmethod
    def _excerpt(response: Any) -> str:
        content = getattr(response, "content", "") or ""
        if not isinstance(content, str):
            return ""
        if len(content) > 200:
            return content[:200] + "..."
        return content


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _strip_fences(text: str) -> str:
    return _MARKDOWN_FENCE.sub("", text).strip()


def _short_id() -> str:
    return uuid.uuid4().hex[:12]
