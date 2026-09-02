"""
Omnix V6 — Identity / conversation context containers.

These are the three *identity* containers that the :class:`ContextService`
owns (the other two — :class:`TaskState` and :class:`WorldState` — are
in :mod:`core.state.domain`):

    * :class:`ConversationContext` — the rolling chat buffer + intent
    * :class:`EntityContext`        — known entities (apps, files, people)
    * :class:`UserContext`          — user profile, preferences, identity

All three follow the same rules as the domain state:
    - frozen dataclass
    - ``with_*`` methods that return new instances
    - ``to_dict`` for logging / persistence
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# ConversationContext
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ConversationTurn:
    """One user↔engine exchange in the conversation buffer."""

    role: str                       # "user" | "assistant" | "system"
    content: str
    timestamp: float
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "role": self.role,
            "content": self.content,
            "timestamp": self.timestamp,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class ConversationContext:
    """The chat / command buffer the brain reasons about.

    The buffer is *bounded*: when ``max_turns`` is exceeded, the
    oldest non-pinned turns are dropped.  ``pinned`` is a tiny list
    of turns (typically the system prompt and the original goal) that
    must never be dropped.
    """

    session_id: str
    turns: Tuple[ConversationTurn, ...] = ()
    pinned: Tuple[ConversationTurn, ...] = ()
    current_intent: str = ""
    current_intent_confidence: float = 0.0
    max_turns: int = 20
    metadata: Dict[str, Any] = field(default_factory=dict)

    # --------------------------------------------------------- derived
    @property
    def last_user_turn(self) -> Optional[ConversationTurn]:
        for turn in reversed(self.turns):
            if turn.role == "user":
                return turn
        return None

    @property
    def last_assistant_turn(self) -> Optional[ConversationTurn]:
        for turn in reversed(self.turns):
            if turn.role == "assistant":
                return turn
        return None

    # -------------------------------------------------------- updates
    def append_turn(self, turn: ConversationTurn) -> "ConversationContext":
        new_turns: List[ConversationTurn] = list(self.turns)
        new_turns.append(turn)
        # bound: keep last ``max_turns`` of non-pinned
        if len(new_turns) > self.max_turns:
            new_turns = new_turns[-self.max_turns:]
        return replace(self, turns=tuple(new_turns))

    def with_intent(self, intent: str, confidence: float = 0.0) -> "ConversationContext":
        return replace(
            self,
            current_intent=intent,
            current_intent_confidence=max(0.0, min(1.0, confidence)),
        )

    def with_pinned(self, turn: ConversationTurn) -> "ConversationContext":
        return replace(self, pinned=tuple([*self.pinned, turn]))

    def clear(self) -> "ConversationContext":
        return replace(self, turns=(), pinned=(), current_intent="", current_intent_confidence=0.0)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": "ConversationContext",
            "session_id": self.session_id,
            "turns": [t.to_dict() for t in self.turns],
            "pinned": [t.to_dict() for t in self.pinned],
            "current_intent": self.current_intent,
            "current_intent_confidence": self.current_intent_confidence,
            "max_turns": self.max_turns,
            "metadata": dict(self.metadata),
        }


# ---------------------------------------------------------------------------
# EntityContext
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Entity:
    """A named thing the engine knows about.

    ``kind`` is a free-form label (``"app"``, ``"file"``, ``"person"``,
    ``"url"`` …).  ``aliases`` lets the user say "browser" / "chrome" /
    "google chrome" and resolve to the same entity.
    """

    name: str
    kind: str
    aliases: Tuple[str, ...] = ()
    attributes: Dict[str, Any] = field(default_factory=dict)
    last_seen: Optional[float] = None
    mention_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "aliases": list(self.aliases),
            "attributes": dict(self.attributes),
            "last_seen": self.last_seen,
            "mention_count": self.mention_count,
        }


@dataclass(frozen=True)
class EntityContext:
    """Catalog of named entities the user has referred to.

    Stored as a tuple for immutability; lookup helpers scan linearly
    because the catalog is expected to stay small (dozens, not
    thousands, of entries per session).
    """

    entities: Tuple[Entity, ...] = ()

    # ------------------------------------------------------------ helpers
    def find(self, name: str) -> Optional[Entity]:
        """Find by exact canonical name or any alias (case-insensitive)."""
        target = name.strip().lower()
        for ent in self.entities:
            if ent.name.lower() == target:
                return ent
            for alias in ent.aliases:
                if alias.lower() == target:
                    return ent
        return None

    def by_kind(self, kind: str) -> Tuple[Entity, ...]:
        return tuple(e for e in self.entities if e.kind == kind)

    # ----------------------------------------------------------- updates
    def upsert(self, entity: Entity) -> "EntityContext":
        """Insert or update an entity by canonical name."""
        new_list: List[Entity] = []
        replaced = False
        for e in self.entities:
            if e.name == entity.name:
                new_list.append(
                    Entity(
                        name=entity.name,
                        kind=entity.kind,
                        aliases=tuple({*e.aliases, *entity.aliases}),
                        attributes={**e.attributes, **entity.attributes},
                        last_seen=entity.last_seen or e.last_seen,
                        mention_count=e.mention_count + entity.mention_count + 1,
                    )
                )
                replaced = True
            else:
                new_list.append(e)
        if not replaced:
            new_list.append(entity)
        return replace(self, entities=tuple(new_list))

    def remove(self, name: str) -> "EntityContext":
        return replace(
            self,
            entities=tuple(e for e in self.entities if e.name != name),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": "EntityContext",
            "entities": [e.to_dict() for e in self.entities],
        }


# ---------------------------------------------------------------------------
# UserContext
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class UserContext:
    """Static / slow-changing information about the human user."""

    user_id: str
    display_name: str = ""
    preferred_language: str = "en"
    timezone: str = "UTC"
    preferences: Dict[str, Any] = field(default_factory=dict)
    permissions: Tuple[str, ...] = ()
    metadata: Dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------ helpers
    def has_permission(self, name: str) -> bool:
        return name in self.permissions

    # ----------------------------------------------------------- updates
    def with_preference(self, key: str, value: Any) -> "UserContext":
        prefs = dict(self.preferences)
        prefs[key] = value
        return replace(self, preferences=prefs)

    def with_permission(self, name: str, granted: bool = True) -> "UserContext":
        if granted and name not in self.permissions:
            return replace(self, permissions=tuple([*self.permissions, name]))
        if not granted:
            return replace(
                self,
                permissions=tuple(p for p in self.permissions if p != name),
            )
        return self

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": "UserContext",
            "user_id": self.user_id,
            "display_name": self.display_name,
            "preferred_language": self.preferred_language,
            "timezone": self.timezone,
            "preferences": dict(self.preferences),
            "permissions": list(self.permissions),
            "metadata": dict(self.metadata),
        }
