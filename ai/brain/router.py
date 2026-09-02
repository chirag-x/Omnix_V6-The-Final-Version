"""
Omnix V6 — System 2 Brain request router (Phase 17).

The router is a *deterministic* function that takes raw user text
and returns a :class:`RoutingDecision`.  The decision tells the
Brain whether the input is:

  * :attr:`TaskKind.CONVERSATIONAL`  — pure social input, no
                                       subsystem needed.
  * :attr:`TaskKind.COMPUTER_USE`    — a deterministic local
                                       command ("open Notepad",
                                       "type hello", "navigate to
                                       example.com").
  * :attr:`TaskKind.HYBRID`          — a request that combines a
                                       deterministic part and a
                                       generative part ("open
                                       Notepad and write me a
                                       Python calculator").
  * :attr:`TaskKind.UNKNOWN`         — cannot decide locally;
                                       escalate to the LLM.

The router is **the single source of truth** for the
"AI for intelligence, local subsystems for execution" rule.

The router never imports an LLM provider, a Windows service, or a
capability.  It is pure data + a small rule table.

The router DOES NOT replace the existing
:class:`core.services.ai_escalation_gate.AIEscalationGate` — it
sits *in front* of it and produces a higher-level task kind.  The
Brain composes both: the router's task kind decides the Brain's
overall behaviour; the escalation gate decides whether the LLM
itself is consulted for the *generative* part of a hybrid task.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from .task.models import TaskKind


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------


# Conversational triggers.  These are *exact* openers; the
# interpreter's own greeting pre-classification (regex) is the
# canonical detector, but the router needs a quick pre-check so
# the Brain can short-circuit before the interpreter.
_CONVERSATIONAL_TRIGGERS: Sequence[str] = (
    "hi",
    "hello",
    "hey",
    "hiya",
    "howdy",
    "yo",
    "thanks",
    "thank you",
    "goodbye",
    "bye",
    "good morning",
    "good afternoon",
    "good evening",
    "good day",
    "how are you",
    "how are ya",
    "how are things",
    "what's up",
    "whats up",
    "greetings",
    "nice to meet you",
)

# Computer-use verbs — the ones the local engine can resolve.
_LOCAL_VERBS: Sequence[str] = (
    "open", "launch", "start", "run", "bring up", "fire up", "boot",
    "close", "quit", "exit", "kill", "terminate", "shut down", "stop",
    "focus", "switch to", "bring to front", "activate",
    "type", "enter", "input", "write",
    "click", "press", "tap",
    "move", "drag",
    "scroll",
    "read", "show me",
    "delete", "remove", "trash",
    "find", "search", "search for", "locate",
    "list", "show all", "enumerate",
    "go to", "navigate to", "visit", "browse to", "open url", "open website",
    "google", "google for", "look up", "web search for",
    "save as", "save it as", "save the file as", "save file as", "save",
    "store as", "export as",
)

# Generative markers.  When the user text contains a *generative*
# verb in addition to a local verb, the task is hybrid.
_GENERATIVE_VERBS: Sequence[str] = (
    "write me", "generate", "create", "compose", "draft", "code",
    "build me", "make me", "design me", "implement", "summarise",
    "summarize", "explain", "translate", "convert", "rewrite",
    "refactor", "add comments to", "document", "review", "test",
)


# ---------------------------------------------------------------------------
# Decision
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RoutingDecision:
    """The output of :class:`RequestRouter.classify`.

    Attributes
    ----------
    kind:
        The :class:`TaskKind` the Brain should adopt.
    escalate:
        True when the Brain should consult the LLM (e.g. for the
        generative part of a hybrid task).  False when the Brain
        can run the request entirely on the local machine.
    reason:
        A short, structured reason code for observability.
    matched_verbs:
        The list of local verbs the router matched (one per
        clause for compound requests).
    matched_generative_verbs:
        The list of generative verbs the router matched.
    matched_conversational:
        The conversational trigger that matched, or an empty
        string.
    """

    kind: TaskKind
    escalate: bool = False
    reason: str = ""
    matched_verbs: Tuple[str, ...] = ()
    matched_generative_verbs: Tuple[str, ...] = ()
    matched_conversational: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_local_only(self) -> bool:
        return self.kind is TaskKind.COMPUTER_USE and not self.escalate

    @property
    def is_conversational(self) -> bool:
        return self.kind is TaskKind.CONVERSATIONAL


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def _normalise(text: str) -> str:
    return (text or "").strip().lower()


def _strip_polite(text: str) -> str:
    """Strip a leading ``please`` / ``can you`` / ``hey`` etc.

    The local engine does the same; we keep a tiny, well-defined
    version here so the router is independent.
    """
    return re.sub(
        r"^\s*(?:please\s+|can\s+you\s+|could\s+you\s+|would\s+you\s+|"
        r"i\s+want\s+to\s+|i\s+want\s+|i\s+need\s+to\s+|i\s+need\s+|"
        r"hey\s+|ok\s+,?\s+)?",
        "",
        text,
        flags=re.IGNORECASE,
    ).strip()


def _match_conversational(text: str) -> Optional[str]:
    low = _normalise(text)
    # Strip trailing punctuation.
    low = re.sub(r"[\.\?\!]+$", "", low).strip()
    for trig in _CONVERSATIONAL_TRIGGERS:
        if low == trig or low.startswith(trig + " "):
            return trig
    return None


def _match_verbs(text: str, verbs: Sequence[str]) -> Tuple[str, ...]:
    """Return the verbs in ``verbs`` that occur in ``text`` as
    sentence-leading tokens.

    "Sentence-leading" means the verb must appear at the start of
    a clause, optionally preceded by a polite prefix.  This avoids
    matching "the close button" as a ``close`` command.
    """
    clauses = re.split(r"[,;\.]", text)
    matched: list[str] = []
    for raw in clauses:
        clause = _strip_polite(raw).strip()
        if not clause:
            continue
        # Sort by length, longest first, so "switch to" matches
        # before "switch" if both are in the table.
        for verb in sorted(verbs, key=len, reverse=True):
            pattern = rf"^\s*{re.escape(verb)}\b"
            if re.match(pattern, clause, flags=re.IGNORECASE):
                matched.append(verb)
                break
    return tuple(matched)


def _has_generative_marker(text: str, generative_verbs: Sequence[str]) -> Tuple[str, ...]:
    low = _normalise(text)
    return tuple(v for v in generative_verbs if v in low)


class RequestRouter:
    """The deterministic request router.

    Construction
    ------------
    The router is a value object.  It carries no mutable state and
    is safe to share a single instance across threads.
    """

    def __init__(
        self,
        *,
        conversational_triggers: Sequence[str] = _CONVERSATIONAL_TRIGGERS,
        local_verbs: Sequence[str] = _LOCAL_VERBS,
        generative_verbs: Sequence[str] = _GENERATIVE_VERBS,
    ) -> None:
        self._conversational_triggers = tuple(conversational_triggers)
        self._local_verbs = tuple(local_verbs)
        self._generative_verbs = tuple(generative_verbs)

    def classify(self, text: str) -> RoutingDecision:
        """Classify a single user utterance.

        The function is **idempotent and pure**.  The same input
        always produces the same :class:`RoutingDecision`.
        """
        if not text or not isinstance(text, str):
            return RoutingDecision(
                kind=TaskKind.UNKNOWN,
                reason="empty_input",
            )
        stripped = text.strip()
        if not stripped:
            return RoutingDecision(kind=TaskKind.UNKNOWN, reason="empty_input")

        # 1. Conversational short-circuit.
        conv = _match_conversational(stripped)
        if conv is not None:
            return RoutingDecision(
                kind=TaskKind.CONVERSATIONAL,
                escalate=False,
                reason="conversational_match",
                matched_conversational=conv,
            )

        # 2. Local-only verbs.
        local = _match_verbs(stripped, self._local_verbs)

        # 3. Generative markers.
        gen = _has_generative_marker(stripped, self._generative_verbs)

        if local and gen:
            return RoutingDecision(
                kind=TaskKind.HYBRID,
                escalate=True,
                reason="local_plus_generative",
                matched_verbs=local,
                matched_generative_verbs=gen,
            )
        if local:
            return RoutingDecision(
                kind=TaskKind.COMPUTER_USE,
                escalate=False,
                reason="local_verbs",
                matched_verbs=local,
            )
        if gen:
            return RoutingDecision(
                kind=TaskKind.HYBRID,
                escalate=True,
                reason="generative_only",
                matched_generative_verbs=gen,
            )
        return RoutingDecision(
            kind=TaskKind.UNKNOWN,
            escalate=True,
            reason="no_local_match",
        )


__all__ = ["RequestRouter", "RoutingDecision"]
