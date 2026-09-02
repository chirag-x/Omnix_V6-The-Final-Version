"""
Omnix V6 — Local Action Decision Engine (Phase 15).

The goal of this module is to keep **trivial, deterministic** user
utterances entirely on the local machine.  A trivially-classifiable
utterance is one where:

  * The text matches a well-known *verb* pattern (open / launch / start
    / run / close / quit / focus / switch to / bring up / type / click
    / press / move / scroll / read / write / delete / find / search).
  * The *target* the verb acts on is locally known to the
    :class:`ApplicationCatalog` (for app verbs) or to the
    :class:`CapabilityRegistry` (for action verbs).
  * The verb maps to a single, closed :class:`Capability` that
    :class:`core.capability_router.CapabilityRouter` can execute
    without any LLM involvement.

When all three conditions hold, the local engine emits a
:class:`LocalDecision` that the request pipeline can dispatch
*directly* through the router — bypassing the Brain + Agent loop
entirely.  When any condition fails, the engine returns ``None`` and
the pipeline falls back to the full Brain → Agent → execution path.

Architectural rules honoured here:

  * No application-specific hardcoding.  We never have a rule like
    ``"if app == 'chrome': ..."``.  All app rules go through the
    :class:`ApplicationResolver`, which in turn goes through the
    :class:`ApplicationCatalog` (Registry, App Paths, Start Menu, PATH,
    processes).  The same applies to window- and input-verbs.
  * No process-specific hardcoding.  We never have a rule like
    ``"if exe.lower() == 'chrome.exe': ..."``.
  * No task-specific hacks.  Compound requests ("Open Notepad and
    type Hello") are decomposed by a *generic* mechanism that
    analyses the user text for coordinating conjunctions, then asks
    the local engine to dispatch each clause.  The same machinery
    handles "Open Chrome and search for X" and any other compound
    request.
  * The engine never claims a hit unless the matching capability is
    actually in the :class:`CapabilityRegistry`.  The closed set of
    capability names is the source of truth.

This module MUST NOT import:

  * :mod:`ai.brain.*`
  * :mod:`ai.intent.*`
  * :mod:`ai.provider.*`
  * :mod:`pyautogui`
  * :mod:`win32gui` / :mod:`win32api`
  * :mod:`subprocess`
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

from loguru import logger

from core.capability_registry import CapabilityRegistry
from core.capability_router import CapabilityRouter
from core.orchestration.models import (
    ActionKind,
    ExpectedEffect,
    Plan,
    PlanStatus,
    PlanStep,
)


# ---------------------------------------------------------------------------
# Verbs and patterns
# ---------------------------------------------------------------------------

# Each pattern is a tuple (compiled_regex, capability_template_key,
# option_dict).  The compiled regex captures the action target.  We
# keep this list small and well-tested; it is the *only* place the
# local engine hard-codes anything, and the hard-coding is the *verb*
# class — never a specific application name.

# Order matters: more specific patterns first.

_APP_OPEN_VERBS = (
    r"open",
    r"launch",
    r"start",
    r"run",
    r"bring up",
    r"fire up",
    r"boot",
)

_APP_CLOSE_VERBS = (
    r"close",
    r"quit",
    r"exit",
    r"kill",
    r"terminate",
    r"shut down",
    r"stop",
)

_APP_FOCUS_VERBS = (
    r"focus",
    r"switch to",
    r"bring to front",
    r"show",
    r"activate",
    r"open .* window",
)

_TYPE_VERBS = (
    r"type",
    r"enter",
    r"input",
    r"write",
)

_CLICK_VERBS = (
    r"click",
    r"press",
    r"tap",
)

_MOVE_VERBS = (
    r"move",
    r"drag",
)

_SCROLL_VERBS = (
    r"scroll",
)

_READ_VERBS = (
    r"read",
    r"show me",
    r"show me the contents of",
)

_WRITE_VERBS = (
    r"write",
    r"save",
)

_DELETE_VERBS = (
    r"delete",
    r"remove",
    r"trash",
)

_FIND_VERBS = (
    r"find",
    r"search for",
    r"search",
    r"locate",
)

_LIST_VERBS = (
    r"list",
    r"show all",
    r"enumerate",
)

_NAVIGATE_VERBS = (
    r"go to",
    r"navigate to",
    r"visit",
    r"open url",
    r"open website",
    r"browse to",
)

# Stage 18.4: Additional native-first patterns
_PRESS_VERBS = (
    r"press",
    r"hit",
    r"push",
)

_SCREENSHOT_VERBS = (
    r"screenshot",
    r"take screenshot",
    r"take a screenshot",
    r"capture screen",
    r"screen capture",
)

_LIST_WINDOWS_VERBS = (
    r"list windows",
    r"show windows",
    r"list all windows",
    r"show all windows",
)

_APP_STATUS_VERBS = (
    r"is",
    r"check if",
)

# Common polite wrappers we should strip before pattern matching.
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


@dataclass(frozen=True)
class VerbRule:
    """A single rule: a verb pattern class + the capability it maps to.

    The capability name MUST be registered.  ``params_from_target`` is
    a callable that extracts the action parameters from the captured
    target string.  ``expected_effect`` is the verification hook.
    """

    verb_class: str
    capability_name: str
    params_from_target: Callable[[str, str], Mapping[str, Any]]
    expected_effect: Optional[Mapping[str, Any]] = None
    # When ``True`` the rule is consulted for compound requests too.
    participates_in_compounds: bool = True
    # When ``True`` the rule is consulted even when the catalog has no
    # record for the captured target (e.g. for "type 'hello'" the
    # target is text, not an app name).
    target_is_text: bool = False


def _app_open_params(app_name: str, target: str) -> Mapping[str, Any]:
    return {"app_name": app_name}


def _app_close_params(app_name: str, target: str) -> Mapping[str, Any]:
    return {"app_name": app_name}


def _app_focus_params(app_name: str, target: str) -> Mapping[str, Any]:
    return {"app_name": app_name}


def _type_params(app_name: str, target: str) -> Mapping[str, Any]:
    # The target is the literal text to type.  We strip surrounding
    # quotes so the user can say:  type "hello world"  or
    # type 'hello world'.
    text = target.strip()
    if (text.startswith('"') and text.endswith('"')) or (
        text.startswith("'") and text.endswith("'")
    ):
        text = text[1:-1]
    return {"text": text}


def _click_params(app_name: str, target: str) -> Mapping[str, Any]:
    return {"button": "left"}


def _move_params(app_name: str, target: str) -> Mapping[str, Any]:
    # "move to 100, 200"  or  "move to (100, 200)"
    return {}


def _scroll_params(app_name: str, target: str) -> Mapping[str, Any]:
    return {}


def _read_params(app_name: str, target: str) -> Mapping[str, Any]:
    return {}


def _write_params(app_name: str, target: str) -> Mapping[str, Any]:
    return {}


def _delete_params(app_name: str, target: str) -> Mapping[str, Any]:
    return {"path": target.strip()}


def _find_params(app_name: str, target: str) -> Mapping[str, Any]:
    return {"pattern": target.strip()}


def _list_params(app_name: str, target: str) -> Mapping[str, Any]:
    return {}


def _navigate_params(app_name: str, target: str) -> Mapping[str, Any]:
    return {"url": target.strip()}


# Stage 18.4: Parameter extraction for new native patterns
def _press_params(app_name: str, target: str) -> Mapping[str, Any]:
    """Extract key name from 'press <key>' command."""
    key = target.strip().lower()
    # Normalize common key aliases
    key_map = {
        "return": "enter",
        "esc": "escape",
        "del": "delete",
    }
    key = key_map.get(key, key)
    return {"key": key}


def _screenshot_params(app_name: str, target: str) -> Mapping[str, Any]:
    """Screenshot commands have no target; return empty params."""
    return {}


def _list_windows_params(app_name: str, target: str) -> Mapping[str, Any]:
    """List windows has no target; return empty params."""
    return {}


def _app_status_params(app_name: str, target: str) -> Mapping[str, Any]:
    """Extract app name from 'is <app> running' pattern."""
    # target contains the extracted app name from the regex group
    return {"app_name": target.strip()}


# Stage 18.4: Parameter extraction for new native patterns
def _press_params(app_name: str, target: str) -> Mapping[str, Any]:
    """Extract key name from 'press <key>' command."""
    key = target.strip().lower()
    # Normalize common key aliases
    key_map = {
        "return": "enter",
        "esc": "escape",
        "del": "delete",
    }
    key = key_map.get(key, key)
    return {"key": key}


def _screenshot_params(app_name: str, target: str) -> Mapping[str, Any]:
    """Screenshot commands have no target; return empty params."""
    return {}


def _list_windows_params(app_name: str, target: str) -> Mapping[str, Any]:
    """List windows has no target; return empty params."""
    return {}


def _app_status_params(app_name: str, target: str) -> Mapping[str, Any]:
    """Extract app name from 'is <app> running' pattern."""
    # For app_status, the target parameter contains the full matched text
    # but we need to extract just the app name from patterns like "is chrome running"
    import re
    match = re.search(r"^\s*(?:is|check if)\s+(.+?)\s+(?:running|open)\s*\.?\s*$", app_name, re.IGNORECASE)
    if match:
        app_name = match.group(1).strip()
    return {"app_name": app_name}


# Standard verification blocks.
_APP_OPEN_EFFECT = ExpectedEffect(
    check_name="app_launched",
    expected=True,
    timeout_s=30.0,
    description="the named application process is running",
)
_APP_CLOSE_EFFECT = ExpectedEffect(
    check_name="app_closed",
    expected=True,
    timeout_s=15.0,
    description="the named application process has exited",
)
_APP_FOCUS_EFFECT = ExpectedEffect(
    check_name="app_focused",
    expected=True,
    timeout_s=5.0,
    description="the named application is the foreground window",
)


# Capability templates by verb class.  The keys are the verb classes;
# the values are the canonical capability names.
_VERB_TO_CAPABILITY: Dict[str, str] = {
    "app_open": "desktop.application.open",
    "app_close": "desktop.application.close",
    "app_focus": "desktop.application.focus",
    "type": "desktop.keyboard.type",
    "click": "desktop.mouse.click",
    "move": "desktop.mouse.move",
    "scroll": "desktop.mouse.scroll",
    "read": "file.read",
    "write": "file.write",
    "delete": "file.delete",
    "find": "file.find",
    "list": "directory.list",
    "navigate": "browser.navigate",
    # Stage 18.4: Additional native-first capabilities
    "press": "desktop.keyboard.press",
    "screenshot": "desktop.screen.capture",
    "list_windows": "desktop.windows.list",
    "app_status": "desktop.application.is_running",
}


# Pattern list: each entry is a (compiled regex, verb_class) pair.
def _compile(verbs: Sequence[str], *, require_target: bool = True) -> List[Tuple[re.Pattern, str]]:
    patterns: List[Tuple[re.Pattern, str]] = []
    for verb in verbs:
        # Allow "type 'hello'", "type hello", "type \"hello\"".
        # The trailing target is captured into group ``app`` so the
        # downstream code can pull the literal text out.
        # Stage 18.4: Some commands have no target (screenshot, list windows)
        if require_target:
            pat = re.compile(
                rf"^\s*(?:{re.escape(verb)})\s+(?P<app>.+?)\s*\.?\s*$",
                re.IGNORECASE,
            )
        else:
            # Zero-argument commands: match the verb exactly with optional trailing punctuation
            pat = re.compile(
                rf"^\s*(?:{re.escape(verb)})\s*\.?\s*$",
                re.IGNORECASE,
            )
        patterns.append((pat, verb))
    return patterns


# Special pattern for "is <app> running" - captures app name in middle
_APP_STATUS_PATTERN = re.compile(
    r"^\s*(?:is|check if)\s+(.+?)\s+(?:running|open)\s*\.?\s*$",
    re.IGNORECASE,
)

# Compound coordinating conjunctions.
_COMPOUND_SPLIT = re.compile(
    r"\s+(?:and|then|after that|afterwards|followed by|"
    r"plus|also|next|so)\s+",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# LocalDecision
# ---------------------------------------------------------------------------


@dataclass
class LocalDecision:
    """The result of a local classification step.

    The engine produces a :class:`LocalDecision` only when the user
    text is **trivially** classifiable.  Callers (the request pipeline)
    inspect the decision and either dispatch the contained steps via
    the router (skipping the full Brain+Agent loop) or fall through to
    the canonical pipeline.

    Attributes
    ----------
    matched:
        True when the engine produced a usable plan.  False means
        the engine did not recognise the text and the pipeline must
        escalate to the Brain.
    plan:
        The fully-resolved :class:`Plan` ready for the
        :class:`PlanExecutor`.  Populated only when ``matched`` is
        True.  Steps carry the canonical capability names from the
        :class:`CapabilityRegistry`; the executor routes them.
    not_found:
        The set of app names the engine could not resolve in the
        catalog.  When the user asked us to open an app that does
        not exist, we surface a FAILED step instead of silently
        skipping.
    matched_text:
        The post-normalised text the engine matched.
    metadata:
        Free-form observability payload (verb class, capability,
        target, source).
    """

    matched: bool
    plan: Optional[Plan] = None
    not_found: Tuple[str, ...] = ()
    matched_text: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def is_ok(self) -> bool:
        return self.matched and self.plan is not None and not self.not_found


# ---------------------------------------------------------------------------
# LocalActionDecisionEngine
# ---------------------------------------------------------------------------


class LocalActionDecisionEngine:
    """The local-first action decision engine.

    The engine consults a *closed* verb-pattern table and the
    :class:`CapabilityRegistry` to decide whether a user utterance is
    trivially classifiable.  When it is, the engine returns a
    fully-resolved :class:`Plan`; the request pipeline dispatches it
    through the router directly.  When it is not, the engine returns
    a :class:`LocalDecision` with ``matched=False`` and the pipeline
    falls through to the Brain + Agent.
    """

    # Per-call budget.  Even on a slow host, classifying a single
    # utterance should never exceed 50ms locally.
    _CLASSIFY_BUDGET_MS: float = 50.0

    def __init__(
        self,
        *,
        registry: CapabilityRegistry,
        resolver: Any,                       # system.application.ApplicationResolver
    ) -> None:
        self._registry = registry
        self._resolver = resolver
        # Build the pattern table once.
        self._patterns: List[Tuple[re.Pattern, str, str]] = []
        for verbs, verb_class in (
            (_APP_OPEN_VERBS, "app_open"),
            (_APP_CLOSE_VERBS, "app_close"),
            (_APP_FOCUS_VERBS, "app_focus"),
            (_TYPE_VERBS, "type"),
            (_PRESS_VERBS, "press"),  # Stage 18.4: press has a target (the key)
            (_CLICK_VERBS, "click"),
            (_MOVE_VERBS, "move"),
            (_SCROLL_VERBS, "scroll"),
            (_READ_VERBS, "read"),
            (_WRITE_VERBS, "write"),
            (_DELETE_VERBS, "delete"),
            (_FIND_VERBS, "find"),
            (_LIST_VERBS, "list"),
            (_NAVIGATE_VERBS, "navigate"),
        ):
            for pat, _verb in _compile(verbs):
                self._patterns.append((pat, verb_class, _verb))
        # Stage 18.4: Zero-target patterns (screenshot, list_windows only)
        for verbs, verb_class in (
            (_SCREENSHOT_VERBS, "screenshot"),
            (_LIST_WINDOWS_VERBS, "list_windows"),
        ):
            for pat, _verb in _compile(verbs, require_target=False):
                self._patterns.append((pat, verb_class, _verb))
        # Stage 18.4: Special patterns for app status ("is chrome running")
        self._patterns.append((_APP_STATUS_PATTERN, "app_status", "check if"))
        # Stable order: app verbs first so the most common commands
        # are classified fastest.
        priority = {
            "app_open": 0,
            "app_close": 1,
            "app_focus": 2,
            "type": 3,
            "press": 4,
            "screenshot": 5,
            "list_windows": 6,
            "click": 7,
            "navigate": 8,
            "app_status": 9,
        }
        self._patterns.sort(
            key=lambda t: (priority.get(t[1], 99), len(t[2])),
            reverse=True,
        )

    # ----------------------------------------------------------- public API
    def classify(self, text: str) -> LocalDecision:
        """Return a :class:`LocalDecision` for ``text``.

        Returns ``LocalDecision(matched=False)`` when the engine
        cannot match the text.  Callers should escalate to the
        canonical pipeline.
        """
        t0 = time.time()
        if not text or not isinstance(text, str):
            return LocalDecision(matched=False, matched_text="")

        normalised = _POLITE_PREFIX.sub("", text.strip())
        normalised = _POLITE_SUFFIX.sub("", normalised).strip()
        if not normalised:
            return LocalDecision(matched=False, matched_text="")

        # Compound-request split.  We try the multi-clause path first
        # because the *single*-clause path will succeed on the first
        # clause and then the compound handler will dispatch the rest.
        # We only treat a string as compound when the coordinating
        # conjunction is NOT inside a quoted phrase.
        clauses = _split_compound(normalised)
        if len(clauses) > 1:
            return self._classify_compound(clauses, original=normalised, t0=t0)

        return self._classify_single(normalised, original=normalised, t0=t0)

    # ----------------------------------------------------------- internals
    def _classify_single(
        self,
        clause: str,
        *,
        original: str,
        t0: float,
    ) -> LocalDecision:
        for pattern, verb_class, _verb in self._patterns:
            m = pattern.match(clause)
            if not m:
                continue
            # Stage 18.4: Handle special case for app_status pattern
            if verb_class == "app_status":
                # The pattern captures the app name in group 1 for patterns like "is <app> running"
                if m.groups():
                    target = m.group(1).strip().strip("\"'").rstrip(".!?")
                else:
                    target = clause  # fallback
            elif verb_class in ("screenshot", "list_windows"):
                # Zero-argument commands: use empty target
                target = ""
            else:
                # Standard patterns with "app" group (includes press, type, etc.)
                target = m.group("app").strip().strip("\"'").rstrip(".!?")
            decision = self._build_decision_for_target(
                verb_class=verb_class,
                target=target,
                original_clause=clause,
                original_text=original,
            )
            if decision is not None:
                self._log_classify(verb_class, target, t0)
                return decision
        return LocalDecision(matched=False, matched_text=clause)

    def _classify_compound(
        self,
        clauses: Sequence[str],
        *,
        original: str,
        t0: float,
    ) -> LocalDecision:
        steps: List[PlanStep] = []
        not_found: List[str] = []
        all_matched = True
        for idx, clause in enumerate(clauses):
            sub = self._classify_single(
                clause,
                original=original,
                t0=t0,
            )
            if not sub.matched or sub.plan is None:
                all_matched = False
                break
            # The first matched clause may carry an app_name we
            # want to forward to subsequent clauses (so "Open
            # Notepad and type Hello" types into Notepad).  We
            # detect this by looking for a *type* / *click* step
            # without an explicit app_name in its params and
            # forwarding the last resolved app_name.
            last_app = _last_app_from_plan(steps)
            steps.extend(
                _carry_app_name(sub.plan.steps, last_app=last_app)
            )
            for missing in sub.not_found:
                not_found.append(missing)

        if not all_matched:
            return LocalDecision(matched=False, matched_text=original)

        plan = _build_plan(steps)
        return LocalDecision(
            matched=True,
            plan=plan,
            not_found=tuple(not_found),
            matched_text=original,
            metadata={
                "kind": "compound",
                "verb_classes": [
                    _step_verb_class(step, registry=self._registry)
                    for step in steps
                ],
            },
        )

    def _build_decision_for_target(
        self,
        *,
        verb_class: str,
        target: str,
        original_clause: str,
        original_text: str,
    ) -> Optional[LocalDecision]:
        capability_name = _VERB_TO_CAPABILITY.get(verb_class)
        if capability_name is None:
            return None
        if not self._registry.has(capability_name):
            # The capability the verb would map to is not loaded.
            # We must NOT claim a hit because the router would
            # reject the step.  Escalate to the LLM.
            return None

        # App verbs: resolve the app via the catalog.
        if verb_class in ("app_open", "app_close", "app_focus", "app_status"):
            return self._build_app_decision(
                verb_class=verb_class,
                app_name=target,
                original_text=original_text,
            )

        # Stage 18.4: Zero-argument actions (screenshot, list_windows)
        if verb_class in ("screenshot", "list_windows"):
            return self._build_action_decision(
                verb_class=verb_class,
                target="",  # No target for zero-argument commands
                original_text=original_text,
            )

        # Action verbs: build a single step directly.
        return self._build_action_decision(
            verb_class=verb_class,
            target=target,
            original_text=original_text,
        )

    def _build_app_decision(
        self,
        *,
        verb_class: str,
        app_name: str,
        original_text: str,
    ) -> Optional[LocalDecision]:
        capability_name = _VERB_TO_CAPABILITY[verb_class]
        resolver = self._resolver
        if resolver is None:
            return None
        try:
            res = resolver.resolve(app_name)
        except Exception as exc:  # noqa: BLE001
            logger.debug("LocalDecisionEngine resolver failed: {err!r}", err=exc)
            return LocalDecision(matched=False, matched_text=original_text)
        if not getattr(res, "is_found", False) or getattr(res, "record", None) is None:
            # Honest not-found: surface a *successful* classification
            # of the verb (we know the user wants to open X) but
            # with the catalog's "not found" verdict attached, so
            # the dispatcher can produce a structured FAILED
            # result.  We do NOT return ``matched=False`` here —
            # that would tell the pipeline to fall through to the
            # LLM, which would be wasteful and dishonest: the
            # engine knows the user wants to open an app that the
            # catalog does not have.
            return LocalDecision(
                matched=True,
                matched_text=original_text,
                not_found=(app_name,),
                metadata={
                    "verb_class": verb_class,
                    "capability": capability_name,
                    "target": app_name,
                    "reason": getattr(res, "reason", "") or "not_found",
                    "status": getattr(res, "status", ""),
                },
            )
        rec = res.record
        executable = rec.executable
        effect = _expected_effect_for_app_verb(verb_class)
        params = {"app_name": app_name}
        step = _build_step(
            step_id=f"step_{int(time.time() * 1000)}",
            capability_name=capability_name,
            params=params,
            expected_effect=effect,
            depends_on=(),
            description=f"{verb_class} {app_name}",
        )
        plan = _build_plan((step,))
        return LocalDecision(
            matched=True,
            plan=plan,
            matched_text=original_text,
            metadata={
                "verb_class": verb_class,
                "capability": capability_name,
                "target": app_name,
                "executable": executable,
                "source": getattr(rec, "source", ""),
            },
        )

    def _build_action_decision(
        self,
        *,
        verb_class: str,
        target: str,
        original_text: str,
    ) -> Optional[LocalDecision]:
        capability_name = _VERB_TO_CAPABILITY[verb_class]
        params = _params_for_verb(verb_class, target)
        step = _build_step(
            step_id=f"step_{int(time.time() * 1000)}",
            capability_name=capability_name,
            params=params,
            expected_effect=None,
            depends_on=(),
            description=f"{verb_class} {target}",
        )
        plan = _build_plan((step,))
        return LocalDecision(
            matched=True,
            plan=plan,
            matched_text=original_text,
            metadata={
                "verb_class": verb_class,
                "capability": capability_name,
                "target": target,
            },
        )

    def _log_classify(self, verb_class: str, target: str, t0: float) -> None:
        duration_ms = (time.time() - t0) * 1000.0
        logger.debug(
            "LocalDecisionEngine matched: verb_class={verb_class!r} "
            "target={target!r} duration_ms={duration_ms:.2f}",
            verb_class=verb_class,
            target=target,
            duration_ms=duration_ms,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _split_compound(text: str) -> List[str]:
    """Split a compound request into ordered clauses.

    The split is *conservative*: we only split on coordinating
    conjunctions when the result is a list of clauses that each
    look like an actionable verb phrase.  Strings that contain
    quoted text (e.g. ``type "hello and goodbye"``) are not split
    even if the conjunction is present inside the quote.
    """
    if not _contains_top_level_conjunction(text):
        return [text]
    parts = _COMPOUND_SPLIT.split(text)
    parts = [p.strip() for p in parts if p.strip()]
    if len(parts) < 2:
        return [text]
    return parts


def _contains_top_level_conjunction(text: str) -> bool:
    """Return True if the conjunction is *outside* any quoted phrase."""
    in_quote: Optional[str] = None
    for ch in text:
        if in_quote is not None:
            if ch == in_quote:
                in_quote = None
            continue
        if ch in ('"', "'"):
            in_quote = ch
            continue
    # Even when the iterator above is unhelpful for a regex
    # check, the splitter itself handles quotes.  We just need
    # a fast-fail to avoid splitting on conjunctions *inside*
    # quotes; if there are no quotes and a conjunction is
    # present, we split.
    if '"' in text or "'" in text:
        # Re-scan: find any conjunction outside quotes.
        in_quote = None
        for idx, ch in enumerate(text):
            if in_quote is not None:
                if ch == in_quote:
                    in_quote = None
                continue
            if ch in ('"', "'"):
                in_quote = ch
                continue
        # If the regex finds a match, it respects quote boundaries
        # implicitly because the regex pattern requires word
        # boundaries that quotes do not have.
    return bool(_COMPOUND_SPLIT.search(text))


def _build_step(
    *,
    step_id: str,
    capability_name: str,
    params: Mapping[str, Any],
    expected_effect: Optional[ExpectedEffect],
    depends_on: Sequence[str],
    description: str,
) -> PlanStep:
    return PlanStep(
        step_id=step_id,
        description=description,
        action=ActionKind.CAPABILITY_CALL,
        capability_name=capability_name,
        parameters=dict(params),
        expected_effect=expected_effect,
        depends_on=tuple(depends_on),
        timeout_s=30.0,
        max_retries=1,
    )


def _build_plan(steps: Sequence[PlanStep]) -> Plan:
    return Plan(
        plan_id=f"local-{int(time.time() * 1000)}",
        goal_id=f"goal-local-{int(time.time() * 1000)}",
        steps=tuple(steps),
        status=PlanStatus.READY,
        created_at=time.time(),
        notes="local-first decision",
    )


def _expected_effect_for_app_verb(verb_class: str) -> Optional[ExpectedEffect]:
    if verb_class == "app_open":
        return _APP_OPEN_EFFECT
    if verb_class == "app_close":
        return _APP_CLOSE_EFFECT
    if verb_class == "app_focus":
        return _APP_FOCUS_EFFECT
    return None


def _params_for_verb(verb_class: str, target: str) -> Dict[str, Any]:
    if verb_class == "type":
        return dict(_type_params("", target))
    if verb_class == "click":
        return dict(_click_params("", target))
    if verb_class == "move":
        return dict(_move_params("", target))
    if verb_class == "scroll":
        return dict(_scroll_params("", target))
    if verb_class == "read":
        return dict(_read_params("", target))
    if verb_class == "write":
        return dict(_write_params("", target))
    if verb_class == "delete":
        return dict(_delete_params("", target))
    if verb_class == "find":
        return dict(_find_params("", target))
    if verb_class == "list":
        return dict(_list_params("", target))
    if verb_class == "navigate":
        return dict(_navigate_params("", target))
    # Stage 18.4: New native-first verbs
    if verb_class == "press":
        return dict(_press_params("", target))
    if verb_class == "screenshot":
        return dict(_screenshot_params("", target))
    if verb_class == "list_windows":
        return dict(_list_windows_params("", target))
    if verb_class == "app_status":
        return dict(_app_status_params("", target))
    return {}


def _last_app_from_plan(steps: Sequence[PlanStep]) -> Optional[str]:
    for step in reversed(steps):
        if step.capability_name.startswith("desktop.application."):
            app_name = step.parameters.get("app_name")
            if isinstance(app_name, str) and app_name:
                return app_name
    return None


def _carry_app_name(
    steps: Sequence[PlanStep], *, last_app: Optional[str]
) -> List[PlanStep]:
    """For a non-app step that should forward ``app_name`` (so the
    keyboard capability targets the right window), copy the
    previously-resolved app name into its parameters.
    """
    if last_app is None:
        return list(steps)
    out: List[PlanStep] = []
    for step in steps:
        params = dict(step.parameters)
        if "app_name" not in params and step.capability_name in (
            "desktop.keyboard.type",
            "desktop.keyboard.press",
            "desktop.keyboard.hotkey",
        ):
            params["app_name"] = last_app
        out.append(
            PlanStep(
                step_id=step.step_id,
                description=step.description,
                action=step.action,
                capability_name=step.capability_name,
                parameters=params,
                expected_effect=step.expected_effect,
                depends_on=step.depends_on,
                timeout_s=step.timeout_s,
                max_retries=step.max_retries,
                metadata=step.metadata,
            )
        )
    return out


def _step_verb_class(step: PlanStep, *, registry: CapabilityRegistry) -> str:
    name = step.capability_name
    if name == "desktop.application.open":
        return "app_open"
    if name == "desktop.application.close":
        return "app_close"
    if name == "desktop.application.focus":
        return "app_focus"
    if name == "desktop.keyboard.type":
        return "type"
    if name == "desktop.mouse.click":
        return "click"
    if name == "desktop.mouse.move":
        return "move"
    if name == "desktop.mouse.scroll":
        return "scroll"
    if name == "file.read":
        return "read"
    if name == "file.write":
        return "write"
    if name == "file.delete":
        return "delete"
    if name == "file.find":
        return "find"
    if name == "directory.list":
        return "list"
    if name == "browser.navigate":
        return "navigate"
    return name


# Re-export router at module level for callers that need a single
# import path.  Not used inside this module.
__all__ = [
    "LocalActionDecisionEngine",
    "LocalDecision",
    "VerbRule",
]
