import re
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple, Sequence
import logging
from dataclasses import dataclass

from core.orchestration import Intent, IntentKind
from ai.intent.interpreter import IntentResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Verbs and patterns (migrated from LocalActionDecisionEngine)
# ---------------------------------------------------------------------------

_APP_OPEN_VERBS = (r"open", r"launch", r"start", r"run", r"bring up", r"fire up", r"boot")
_APP_CLOSE_VERBS = (r"close", r"quit", r"exit", r"kill", r"terminate", r"shut down", r"stop")
_APP_FOCUS_VERBS = (r"focus", r"switch to", r"bring to front", r"show", r"activate", r"open .* window")
_TYPE_VERBS = (r"type", r"enter", r"input", r"write", r"send keys")
_CLICK_VERBS = (r"click", r"press", r"tap")
_MOVE_VERBS = (r"move", r"drag")
_SCROLL_VERBS = (r"scroll", r"scroll down", r"scroll up")
_READ_VERBS = (r"read", r"show me", r"show me the contents of")
_WRITE_VERBS = (r"write", r"save", r"save as")
_GENERATE_VERBS = (r"generate", r"create", r"compose", r"draft", r"give me code for", r"write a program that", r"write a", r"write an", r"write code", r"write script")
_DELETE_VERBS = (r"delete", r"remove", r"trash")
_FIND_VERBS = (r"find", r"search for", r"search", r"locate")
_LIST_VERBS = (r"list", r"show all", r"enumerate")
_NAVIGATE_VERBS = (r"go to", r"navigate to", r"visit", r"open url", r"open website", r"browse to")
_PRESS_VERBS = (r"press", r"hit", r"push")
_SCREENSHOT_VERBS = (r"screenshot", r"take screenshot", r"take a screenshot", r"capture screen", r"screen capture")
_LIST_WINDOWS_VERBS = (r"list windows", r"show windows", r"list all windows", r"show all windows")
_APP_STATUS_VERBS = (r"is", r"check if")

_POLITE_PREFIX = re.compile(
    r"^\s*(?:please\s+|can\s+you\s+|could\s+you\s+|would\s+you\s+|"
    r"i\s+want\s+to\s+|i\s+want\s+|i\s+need\s+to\s+|i\s+need\s+|"
    r"hey\s+|ok\s+,?\s+)?",
    re.IGNORECASE,
)
_POLITE_SUFFIX = re.compile(
    r"\s*(?:please|for me|now|thanks|thank you|\.|\?|!)\s*$",
    re.IGNORECASE,
)
_APP_STATUS_PATTERN = re.compile(
    r"^\s*(is|check if)\s+(.*?)\s*(?:running|open)\s*$", re.IGNORECASE
)

def _compile(verbs: Sequence[str], require_target: bool = True) -> List[Tuple[re.Pattern, str]]:
    compiled = []
    for verb in verbs:
        v = verb.replace(" ", r"\s+")
        if require_target:
            pat = re.compile(rf"^\s*{v}\s+(.+)$", re.IGNORECASE)
        else:
            pat = re.compile(rf"^\s*{v}\s*$", re.IGNORECASE)
        compiled.append((pat, verb))
    return compiled


class NativeIntentInterpreter:
    """The Native Intent Interpreter (Phase 22).
    
    Parses natural language into deterministic Intent structures without using an LLM.
    If the utterance cannot be deterministically classified, it returns an unknown IntentResult,
    allowing the pipeline to fallback to the LLMIntentInterpreter.
    """

    name = "NativeIntentInterpreter"

    def __init__(self) -> None:
        self._patterns: List[Tuple[re.Pattern, str, str]] = []
        for verbs, verb_class in (
            (_APP_OPEN_VERBS, IntentKind.OPEN_APPLICATION.value),
            (_APP_CLOSE_VERBS, IntentKind.CLOSE_APPLICATION.value),
            (_APP_FOCUS_VERBS, IntentKind.FOCUS_APPLICATION.value),
            (_TYPE_VERBS, IntentKind.CONTROL_APPLICATION.value),
            (_PRESS_VERBS, IntentKind.CONTROL_APPLICATION.value),
            (_CLICK_VERBS, IntentKind.UI_CLICK_TARGET.value),
            (_MOVE_VERBS, IntentKind.NO_OP.value),
            (_SCROLL_VERBS, IntentKind.CONTROL_APPLICATION.value),
            (_READ_VERBS, IntentKind.FILE_FIND.value),
            (_WRITE_VERBS, IntentKind.CONTROL_APPLICATION.value),
            (_GENERATE_VERBS, IntentKind.GENERATE_CONTENT.value),
            (_DELETE_VERBS, IntentKind.FILE_DELETE.value),
            (_FIND_VERBS, IntentKind.BROWSER_NAVIGATE.value),
            (_LIST_VERBS, IntentKind.WINDOW_MANAGE.value),
            (_NAVIGATE_VERBS, IntentKind.BROWSER_NAVIGATE.value),
        ):
            for pat, _verb in _compile(verbs):
                self._patterns.append((pat, verb_class, _verb))

        for verbs, verb_class in (
            (_SCREENSHOT_VERBS, IntentKind.NO_OP.value),
            (_LIST_WINDOWS_VERBS, IntentKind.WINDOW_MANAGE.value),
        ):
            for pat, _verb in _compile(verbs, require_target=False):
                self._patterns.append((pat, verb_class, _verb))

        self._patterns.append((_APP_STATUS_PATTERN, IntentKind.QUERY_STATUS.value, "check if"))
        
        priority = {
            IntentKind.OPEN_APPLICATION.value: 0,
            IntentKind.CLOSE_APPLICATION.value: 1,
            IntentKind.FOCUS_APPLICATION.value: 2,
            IntentKind.CONTROL_APPLICATION.value: 3,
            IntentKind.GENERATE_CONTENT.value: 4,
            IntentKind.NO_OP.value: 5,
            IntentKind.WINDOW_MANAGE.value: 6,
            IntentKind.UI_CLICK_TARGET.value: 7,
            IntentKind.BROWSER_NAVIGATE.value: 8,
            IntentKind.QUERY_STATUS.value: 9,
        }
        self._patterns.sort(
            key=lambda t: (priority.get(t[1], 99), len(t[2])),
            reverse=True,
        )

    def interpret(self, text: str, *, context_snapshot: Optional[Mapping[str, Any]] = None) -> IntentResult:
        if not text or not isinstance(text, str):
            return IntentResult(status="unknown")

        normalised = _POLITE_PREFIX.sub("", text.strip())
        normalised = _POLITE_SUFFIX.sub("", normalised).strip()
        if not normalised:
            return IntentResult(status="unknown")

        # Handle compound requests
        clauses = self._split_compound(normalised)
        if len(clauses) > 1:
            steps = []
            for clause in clauses:
                steps.append(clause)
            intent = Intent(
                intent_id="native_compound",
                kind=IntentKind.COMPOUND_REQUEST,
                text=text,
                source_text=text,
                parameters={"steps": steps},
                confidence=0.9
            )
            return IntentResult(status="ok", intent=intent)

        # Single clause matching
        for pat, intent_kind, verb in self._patterns:
            m = pat.match(normalised)
            if m:
                target = m.group(1).strip() if m.groups() else ""
                
                if intent_kind == IntentKind.QUERY_STATUS.value and len(m.groups()) == 2:
                    target = m.group(2).strip()

                params = self._build_params(intent_kind, verb, target)
                intent = Intent(
                    intent_id="native_single",
                    kind=IntentKind(intent_kind),
                    text=text,
                    source_text=text,
                    parameters=params,
                    confidence=0.9
                )
                return IntentResult(status="ok", intent=intent)

        # Basic conversational bypassing
        lower = normalised.lower()
        if lower in ["hello", "hi", "hey", "hello omnix"]:
            # Let AI handle greetings
            return IntentResult(status="unknown")
            
        return IntentResult(status="unknown")

    def _build_params(self, intent_kind: str, verb: str, target: str) -> Dict[str, Any]:
        params = {}
        if intent_kind in (IntentKind.OPEN_APPLICATION.value, IntentKind.CLOSE_APPLICATION.value, IntentKind.FOCUS_APPLICATION.value):
            params["app_name"] = target
        elif intent_kind == IntentKind.CONTROL_APPLICATION.value:
            if verb in _TYPE_VERBS:
                params["action"] = "type"
                params["target"] = target
            elif verb in _PRESS_VERBS:
                params["action"] = "press"
                params["target"] = target
            else:
                params["action"] = "type"
                params["target"] = target
        elif intent_kind == IntentKind.BROWSER_NAVIGATE.value:
            if verb in _FIND_VERBS:
                # Map to search action instead of raw navigate
                params["action"] = "search"
                params["target"] = target
            else:
                params["url"] = target
        elif intent_kind == IntentKind.UI_CLICK_TARGET.value:
            params["target_query"] = target
        else:
            params["target"] = target
        return params

    def _split_compound(self, text: str) -> List[str]:
        # Simple compound splitting
        parts = re.split(r'\b(?:and(?:\s+then)?|then|after\s+that)\b', text, flags=re.IGNORECASE)
        cleaned = [p.strip() for p in parts if p.strip()]
        return cleaned

