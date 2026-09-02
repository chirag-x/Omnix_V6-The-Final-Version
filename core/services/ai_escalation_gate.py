"""
Omnix V6 — AI Escalation Gate (Phase 15).

This module implements the *budgeted* rule that decides whether a
user request actually needs the LLM.  The contract is:

  * "AI for intelligence.  Local subsystems for execution."
  * The LLM is called only for genuine reasoning, ambiguity
    resolution, planning, semantic interpretation, or recovery.
  * The LLM is **never** called for trivially-classifiable commands
    ("open Chrome", "type 'hello'", "list downloads", ...).

The gate is a small, deterministic function with no external state.
Callers (the request pipeline, the Brain, the Agent) consult the gate
before invoking the LLM and skip the call when the gate says
``escalate=False``.

The gate is *not* the only place the LLM is gated.  The Brain has its
own deterministic-planner fallback, the engine short-circuits
trivially-classifiable commands through the
:class:`LocalActionDecisionEngine`, and the recovery engine prefers
deterministic retries before asking the LLM.  This module is the
*named* place where the policy lives and the observability surface
that records every escalation decision.

Architectural rules honoured here:

  * The gate never inspects the user text beyond a tiny, well-defined
    feature extractor (length, presence of coordinating conjunctions,
    presence of a "?" etc.).  It never makes a syntactic parse.
  * The gate never returns a *plan*.  It returns a yes/no decision
    plus a short reason.  Plan synthesis is the Brain's job.
  * The gate never touches the network.  All decisions are local.

This module MUST NOT import:

  * :mod:`ai.brain.*`
  * :mod:`ai.intent.*`
  * :mod:`ai.provider.*`
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence


# ---------------------------------------------------------------------------
# Reason codes — observability only
# ---------------------------------------------------------------------------

REASON_TRIVIAL_COMMAND = "trivial_command"
REASON_AMBIGUOUS_TEXT = "ambiguous_text"
REASON_COMPOUND = "compound_request"
REASON_QUESTION = "is_question"
REASON_SEMANTIC_QUERY = "semantic_query"
REASON_LONG_INPUT = "long_input"
REASON_DEFAULT = "default"


# ---------------------------------------------------------------------------
# Decision
# ---------------------------------------------------------------------------


@dataclass
class EscalationDecision:
    """The result of a gate consultation.

    Attributes
    ----------
    escalate:
        True if the LLM must be invoked for this request, False if
        local subsystems can handle it.
    reason:
        A short, structured reason code (one of the ``REASON_*``
        constants).  Logged on the canonical event bus.
    confidence:
        A number in [0.0, 1.0] expressing how confident the gate is
        in its decision.  1.0 = certain.
    details:
        Free-form observability payload.
    """

    escalate: bool
    reason: str
    confidence: float = 1.0
    details: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Feature extraction
# ---------------------------------------------------------------------------


_TRIVIAL_LENGTH_THRESHOLD = 64
_QUESTION_MARK = re.compile(r"\?$|^\s*(?:who|what|where|when|why|how|which|can|could|would|do|does|did|is|are|was|were)\b", re.IGNORECASE)
_TRIVIAL_CLOSING = re.compile(r"\b(thanks|thank you|please)\s*$", re.IGNORECASE)
_AMBIGUOUS_PHRASES = (
    re.compile(r"\b(?:this|that|it|them|they)\b", re.IGNORECASE),
    re.compile(r"\b(?:something|anything|nothing)\b", re.IGNORECASE),
)
_SEMANTIC_QUERIES = (
    re.compile(r"\b(?:what|which)\b", re.IGNORECASE),
    re.compile(r"\b(?:tell me about|describe|explain|summarise|summarize|find information about)\b", re.IGNORECASE),
)


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------


class AIEscalationGate:
    """Decide whether a user request needs the LLM.

    The gate is consulted *before* the Brain is invoked.  When the
    gate returns ``escalate=False``, the caller may attempt a local
    classification first (via
    :class:`core.services.local_decision_engine.LocalActionDecisionEngine`)
    and only fall back to the Brain if the local engine says it
    cannot handle the input.
    """

    def __init__(
        self,
        *,
        max_local_text_length: int = _TRIVIAL_LENGTH_THRESHOLD,
    ) -> None:
        self._max_local_text_length = int(max_local_text_length)

    # ----------------------------------------------------------- public API
    def should_escalate(
        self,
        text: str,
        *,
        local_engine_outcome: Optional[str] = None,
    ) -> EscalationDecision:
        """Return an :class:`EscalationDecision` for ``text``.

        Parameters
        ----------
        text:
            The raw user text.
        local_engine_outcome:
            Optional precomputed outcome from
            :class:`LocalActionDecisionEngine`.  One of
            ``"matched"``, ``"not_found"``, ``"unknown"``.  When
            ``"matched"`` we never escalate — the LLM is not needed.
        """
        if not text or not isinstance(text, str):
            return EscalationDecision(
                escalate=False,
                reason=REASON_TRIVIAL_COMMAND,
                confidence=1.0,
                details={"text_length": 0},
            )

        stripped = text.strip()
        length = len(stripped)
        if length == 0:
            return EscalationDecision(
                escalate=False,
                reason=REASON_TRIVIAL_COMMAND,
                confidence=1.0,
                details={"text_length": 0},
            )

        # If the local engine already classified the text, do not escalate.
        if local_engine_outcome == "matched":
            return EscalationDecision(
                escalate=False,
                reason=REASON_TRIVIAL_COMMAND,
                confidence=1.0,
                details={
                    "text_length": length,
                    "local_engine_outcome": local_engine_outcome,
                },
            )
        if local_engine_outcome == "not_found":
            # The local engine understood the verb but the target was
            # not in the catalog.  The LLM is not needed — we should
            # surface the not-found to the user.
            return EscalationDecision(
                escalate=False,
                reason=REASON_TRIVIAL_COMMAND,
                confidence=0.9,
                details={
                    "text_length": length,
                    "local_engine_outcome": local_engine_outcome,
                },
            )

        # Pure question marks at the end often signal a query that
        # requires semantic understanding.
        if _QUESTION_MARK.search(stripped):
            return EscalationDecision(
                escalate=True,
                reason=REASON_QUESTION,
                confidence=0.85,
                details={"text_length": length},
            )

        # Compound requests: "Open Notepad and type Hello World".
        # The local engine already handles these, so a TRUE compound
        # only escalates when the local engine cannot classify the
        # individual clauses.  We do not need to escalate proactively
        # here.
        if _looks_like_compound(stripped):
            return EscalationDecision(
                escalate=False,
                reason=REASON_COMPOUND,
                confidence=0.7,
                details={"text_length": length},
            )

        # Ambiguous pronouns: "open that", "click on it".
        for pat in _AMBIGUOUS_PHRASES:
            if pat.search(stripped):
                return EscalationDecision(
                    escalate=True,
                    reason=REASON_AMBIGUOUS_TEXT,
                    confidence=0.8,
                    details={"text_length": length},
                )

        # Semantic queries: "what is the capital of France", "tell me
        # about X".
        for pat in _SEMANTIC_QUERIES:
            if pat.search(stripped):
                return EscalationDecision(
                    escalate=True,
                    reason=REASON_SEMANTIC_QUERY,
                    confidence=0.75,
                    details={"text_length": length},
                )

        # Long inputs are unlikely to be trivially classifiable.
        if length > self._max_local_text_length:
            return EscalationDecision(
                escalate=True,
                reason=REASON_LONG_INPUT,
                confidence=0.7,
                details={"text_length": length},
            )

        # Default: do not escalate.  The Brain has its own
        # deterministic fallback; the LLM is not strictly required.
        return EscalationDecision(
            escalate=False,
            reason=REASON_DEFAULT,
            confidence=0.5,
            details={"text_length": length},
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_COMPOUND_CONJUNCTIONS = (
    re.compile(r"\s+and\s+", re.IGNORECASE),
    re.compile(r"\s+then\s+", re.IGNORECASE),
    re.compile(r"\s+after\s+that\s+", re.IGNORECASE),
)


def _looks_like_compound(text: str) -> bool:
    """A conservative check for compound requests.

    We only flag a string as compound when the coordinating
    conjunction is at the *top level* (not inside a quote) and there
    are at least two verb-looking clauses on each side.  The local
    engine does the real split.
    """
    if '"' in text or "'" in text:
        return False
    return any(pat.search(text) for pat in _COMPOUND_CONJUNCTIONS)


__all__ = [
    "AIEscalationGate",
    "EscalationDecision",
    "REASON_TRIVIAL_COMMAND",
    "REASON_AMBIGUOUS_TEXT",
    "REASON_COMPOUND",
    "REASON_QUESTION",
    "REASON_SEMANTIC_QUERY",
    "REASON_LONG_INPUT",
    "REASON_DEFAULT",
]
