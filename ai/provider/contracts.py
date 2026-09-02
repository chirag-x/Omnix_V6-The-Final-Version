"""
Omnix V6 â€” Provider request/response contracts (Phase 5A).

A *provider* is a thin adapter that turns a :class:`LLMRequest` into
an :class:`LLMResponse`.  Providers do not know about Omnix
capabilities, Windows, or the engine; they speak only the contracts
defined here.

Design constraints:

    * All types are frozen dataclasses (R-10: results are immutable).
    * The provider-specific wire format NEVER leaks out: callers see
      only ``LLMResponse``.
    * Errors are raised as :class:`ProviderError` subclasses, never
      as raw provider-specific exceptions.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Role + message
# ---------------------------------------------------------------------------

class MessageRole(str, Enum):
    """Roles for the messages in a :class:`LLMRequest.messages` list."""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"          # reserved for Phase 5B tool-use extension


@dataclass(frozen=True)
class LLMMessage:
    """One message in a chat-style request.

    ``content`` is intentionally a plain ``str`` for Phase 5A.  Multi-
    modal / structured content support is a Phase 5B+ concern.
    """

    role: MessageRole
    content: str
    name: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"role": self.role.value if hasattr(self.role, "value") else str(self.role), "content": self.content}
        if self.name is not None:
            d["name"] = self.name
        return d


# ---------------------------------------------------------------------------
# Output format hint
# ---------------------------------------------------------------------------

class OutputFormat(str, Enum):
    """How the caller wants the model output shaped."""

    TEXT = "text"            # free-form natural language
    JSON = "json"            # model is asked to produce JSON


# ---------------------------------------------------------------------------
# Finish reason + status
# ---------------------------------------------------------------------------

class FinishReason(str, Enum):
    """Standardized finish reasons.

    Mapped from provider-specific values; never the raw value.
    """

    STOP = "stop"
    LENGTH = "length"
    TOOL_CALLS = "tool_calls"   # reserved for Phase 5B
    CONTENT_FILTER = "content_filter"
    CANCELLED = "cancelled"
    ERROR = "error"
    UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LLMUsage:
    """Token / call accounting.

    All fields are optional because not every provider exposes them.
    Callers that need strict accounting must check ``is_complete()``.
    """

    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    def is_complete(self) -> bool:
        return (
            self.prompt_tokens is not None
            and self.completion_tokens is not None
            and self.total_tokens is not None
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "extra": dict(self.extra),
        }


# ---------------------------------------------------------------------------
# Request
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LLMRequest:
    """The data a provider needs to produce one model response.

    A request is a *specification*; providers may add their own model
    identifier and options via :attr:`options`.
    """

    messages: tuple = ()                    # tuple[LLMMessage]
    system: Optional[str] = None            # convenience: prepended as SYSTEM
    context: Dict[str, Any] = field(default_factory=dict)
    output_format: OutputFormat = OutputFormat.TEXT
    model: Optional[str] = None             # provider-specific model id
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    timeout_s: Optional[float] = None       # per-request override
    options: Dict[str, Any] = field(default_factory=dict)
    # An opaque caller tag (e.g. "brain.classify", "brain.plan") for
    # log correlation.  Not interpreted by the provider.
    caller: Optional[str] = None

    def __post_init__(self) -> None:
        # Normalize: tuple of LLMMessage.  Allow callers to pass a list
        # of either LLMMessage or dicts for ergonomics.
        normalized: List[LLMMessage] = []
        for m in self.messages:
            if isinstance(m, LLMMessage):
                normalized.append(m)
            elif isinstance(m, dict):
                normalized.append(
                    LLMMessage(
                        role=MessageRole(m.get("role", "user")),
                        content=str(m.get("content", "")),
                        name=m.get("name"),
                    )
                )
            else:
                raise TypeError(
                    f"LLMRequest.messages entries must be LLMMessage or dict; "
                    f"got {type(m).__name__}"
                )
        # If a system prompt was provided separately, ensure it is the
        # first SYSTEM message.
        if self.system:
            if normalized and normalized[0].role is MessageRole.SYSTEM:
                normalized[0] = LLMMessage(
                    role=MessageRole.SYSTEM,
                    content=self.system,
                    name=normalized[0].name,
                )
            else:
                normalized.insert(0, LLMMessage(role=MessageRole.SYSTEM, content=self.system))
        object.__setattr__(self, "messages", tuple(normalized))

    # ------------------------------------------------------- derived
    @property
    def user_messages(self) -> List[LLMMessage]:
        return [m for m in self.messages if m.role is MessageRole.USER]

    def with_message(self, message: LLMMessage) -> "LLMRequest":
        return replace(self, messages=self.messages + (message,))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "messages": [m.to_dict() for m in self.messages],
            "system": self.system,
            "context": dict(self.context),
            "output_format": self.output_format.value,
            "model": self.model,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "timeout_s": self.timeout_s,
            "options": dict(self.options),
            "caller": self.caller,
        }


# ---------------------------------------------------------------------------
# Response
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LLMResponse:
    """The data a provider returns to the Brain.

    The provider is responsible for parsing raw wire output into
    :attr:`content` and :attr:`usage`.  The Brain never sees a
    provider-native object.
    """

    content: str
    finish_reason: FinishReason = FinishReason.STOP
    model: Optional[str] = None
    usage: LLMUsage = field(default_factory=LLMUsage)
    provider: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "content": self.content,
            "finish_reason": self.finish_reason.value,
            "model": self.model,
            "usage": self.usage.to_dict(),
            "provider": self.provider,
            "metadata": dict(self.metadata),
            # ``raw`` is omitted from to_dict by default to avoid leaking
            # provider-internal fields.  Use ``raw`` directly when needed.
        }

