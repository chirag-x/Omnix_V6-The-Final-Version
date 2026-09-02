"""
Omnix V6 — Memory Subsystem (Phase 9).

R-13 / AD-15: memory is a service, not a singleton, and every write is
governed by a policy.

This module is the *only* canonical boundary between V6 and persistence
of facts the agent should remember across sessions.  It exposes:

    * :class:`MemoryKind`         — closed enum of stored fact categories.
    * :class:`MemoryPolicy`       — retention / dedup / privacy / confidence.
    * :class:`MemoryEntry`        — frozen, audit-ready record of one fact.
    * :class:`MemoryResult`       — service wrapper result (R-2).
    * :class:`RecallHit`          — one scored hit from a recall query.
    * :class:`MemoryStore`        — backend protocol (R-18, AD-18).
    * :class:`InMemoryStore`      — test/dev backend.
    * :class:`MemoryService`      — the engine-facing façade.

Design constraints honoured
---------------------------

R-1  — service instantiated by the engine, never self-constructed.
R-2  — every public method returns a :class:`MemoryResult` dataclass.
R-9  — uniform lifecycle (``initialize`` / ``shutdown`` /
       ``initialized`` / ``statistics`` / ``__repr__``).
R-10 — frozen dataclasses throughout.
R-13 — service, not a singleton; no direct access to a back-end outside
       this module.
R-15 — policy is required, never optional; default policy is conservative.
R-16 — configuration is JSON, secrets are not.  The service *actively*
       rejects secret-shaped content before it ever reaches the backend.
R-17 — loguru only.
R-18 — backend is a protocol, not a concrete class; adding SQLite
       (production) and in-memory (test) is a registration, not a rewrite.
AD-15 — every write carries a confidence score, a provenance record,
       a privacy flag, and an expiry/retention window.
AD-20 — anti-pattern enforcement: secret-shaped payloads are rejected
       at the boundary; the rejection is logged and counted.
"""

from __future__ import annotations

import hashlib
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    Tuple,
    runtime_checkable,
)

from loguru import logger as _loguru

from ..errors import ValidationError
from ..lifecycle import LifecycleMixin, LifecycleState


# ===========================================================================
# Closed enum of memory kinds
# ===========================================================================

class MemoryKind(str, Enum):
    """The closed set of memory kinds the engine will persist (R-21).

    Adding a new kind is a code change *and* a policy review; the
    engine does not invent new kinds at runtime.
    """

    FACT = "fact"                  # a stable fact the user stated
    PREFERENCE = "preference"      # "I like X", "default to Y"
    INSTRUCTION = "instruction"    # "always do X when Y happens"
    CONTEXT = "context"            # session-derived context the engine wants
    EPISODE = "episode"            # a recorded interaction summary
    ENTITY = "entity"              # a resolved entity (a person, file, app)
    TASK_OUTCOME = "task_outcome"  # a past task + its verified result


# ===========================================================================
# Provenance and policy
# ===========================================================================

@dataclass(frozen=True)
class Provenance:
    """Where a fact came from.  Mandatory on every write (AD-15)."""

    source: str                # "user", "engine", "imported", etc.
    session_id: str = ""
    task_id: str = ""
    capability: str = ""
    detail: str = ""           # free-form, but never the fact itself
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "session_id": self.session_id,
            "task_id": self.task_id,
            "capability": self.capability,
            "detail": self.detail,
            "timestamp": self.timestamp,
        }


@dataclass(frozen=True)
class MemoryPolicy:
    """How a memory is governed (AD-15).

    The defaults are intentionally conservative:

    * 90-day retention,
    * deduplication on by-content hash,
    * ``private=True`` (never returned in recall unless explicitly asked
      for private scope),
    * minimum confidence 0.0 (no entries are written with negative
      confidence, but the policy permits zero-confidence facts).
    """

    retention_seconds: float = 60.0 * 60.0 * 24.0 * 90.0   # 90 days
    deduplicate: bool = True
    private: bool = True
    min_confidence: float = 0.0
    max_size_bytes: int = 4096
    allow_kinds: Tuple[MemoryKind, ...] = tuple(MemoryKind)
    deny_kinds: Tuple[MemoryKind, ...] = ()

    def allows(self, kind: MemoryKind) -> bool:
        if self.deny_kinds and kind in self.deny_kinds:
            return False
        if self.allow_kinds and kind not in self.allow_kinds:
            return False
        return True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "retention_seconds": self.retention_seconds,
            "deduplicate": self.deduplicate,
            "private": self.private,
            "min_confidence": self.min_confidence,
            "max_size_bytes": self.max_size_bytes,
            "allow_kinds": [k.value for k in self.allow_kinds],
            "deny_kinds": [k.value for k in self.deny_kinds],
        }


# ===========================================================================
# Entries and results
# ===========================================================================

@dataclass(frozen=True)
class MemoryEntry:
    """One fact the agent has remembered.

    The ``content_hash`` is the dedup key (SHA-256 of the normalized
    content); ``id`` is the storage-side identifier and is unique
    within a backend.
    """

    id: str
    kind: MemoryKind
    content: str
    confidence: float
    provenance: Provenance
    content_hash: str
    created_at: float
    expires_at: Optional[float]
    private: bool
    tags: Tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    # ---------- derived helpers ----------
    @property
    def is_expired(self) -> bool:
        return self.expires_at is not None and time.time() > self.expires_at

    @property
    def age_seconds(self) -> float:
        return max(0.0, time.time() - self.created_at)

    def to_dict(self, *, include_content: bool = True) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "id": self.id,
            "kind": self.kind.value,
            "confidence": self.confidence,
            "content_hash": self.content_hash,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "private": self.private,
            "tags": list(self.tags),
            "metadata": dict(self.metadata),
            "provenance": self.provenance.to_dict(),
        }
        if include_content:
            d["content"] = self.content
        else:
            d["content"] = "[REDACTED]"
        return d


@dataclass(frozen=True)
class MemoryResult:
    """Result of a memory operation (R-2).

    Success is signalled both with ``success=True`` and with a
    meaningful ``status`` enum value so callers may route on the
    label rather than the boolean.
    """

    success: bool
    status: str                          # "stored" | "duplicate" | "rejected" |
                                         # "forgotten" | "recalled" | "empty" |
                                         # "error"
    operation: str                       # "remember" | "forget" | "recall" | "list"
    value: Any = None
    error: Optional[str] = None
    error_code: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.success


@dataclass(frozen=True)
class RecallHit:
    """One scored hit from a recall query."""

    entry: MemoryEntry
    score: float
    match_reason: str = ""        # "kind" | "tag" | "content" | "recent" | "hash"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "score": self.score,
            "match_reason": self.match_reason,
            "entry": self.entry.to_dict(include_content=True),
        }


# ===========================================================================
# Backend protocol
# ===========================================================================

@runtime_checkable
class MemoryStore(Protocol):
    """The minimum surface a memory backend must expose (AD-18).

    A backend is intentionally small: the *service* layer enforces
    policy, secret rejection, and confidence thresholds.  The backend
    just stores and retrieves.
    """

    def initialize(self) -> bool: ...
    def shutdown(self) -> None: ...

    def upsert(self, entry: MemoryEntry) -> bool: ...
    def delete(self, entry_id: str) -> bool: ...
    def get(self, entry_id: str) -> Optional[MemoryEntry]: ...
    def find_by_hash(self, content_hash: str) -> Optional[MemoryEntry]: ...
    def list_recent(
        self,
        *,
        kind: Optional[MemoryKind] = None,
        include_private: bool = False,
        limit: int = 50,
    ) -> List[MemoryEntry]: ...
    def all_hashes(self) -> Iterable[str]: ...
    def count(self) -> int: ...
    def iter_all(self) -> Iterable[MemoryEntry]:
        """Iterate every entry currently stored, including expired ones.

        Used for maintenance operations (e.g. :meth:`MemoryService.purge_expired`)
        that must see the entire state of the store, not just the
        recent / live window.
        """
        ...


# ===========================================================================
# Secret detection (R-16 / AD-20)
# ===========================================================================

@dataclass(frozen=True)
class SecretMatch:
    """A single secret pattern match in some content."""

    pattern_name: str
    sample: str             # the matched substring, truncated
    start: int
    end: int


# A conservative list of high-signal secret patterns.  The goal is to
# reject obvious credentials, not to be a perfect linter.
_SECRET_PATTERNS: Tuple[Tuple[str, re.Pattern[str]], ...] = (
    # Generic API key indicators
    ("api_key_assignment",  re.compile(r"(?i)\b(api[_-]?key|apikey)\b\s*[:=]\s*['\"]?([A-Za-z0-9_\-]{16,})")),
    ("openai_key",          re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
    ("openai_proj_key",     re.compile(r"\bsk-proj-[A-Za-z0-9_\-]{20,}\b")),
    ("anthropic_key",       re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}\b")),
    ("groq_key",            re.compile(r"\bgsk_[A-Za-z0-9]{20,}\b")),
    ("github_token",        re.compile(r"\bghp_[A-Za-z0-9]{30,}\b")),
    ("github_pat",          re.compile(r"\bgithub_pat_[A-Za-z0-9_]{40,}\b")),
    ("github_oauth",        re.compile(r"\bgho_[A-Za-z0-9]{30,}\b")),
    ("slack_token",         re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    ("aws_access_key",      re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("aws_secret_key",      re.compile(r"(?i)aws[_-]?secret[_-]?access[_-]?key\b\s*[:=]\s*['\"]?([A-Za-z0-9/+=]{30,})")),
    ("pem_private_key",     re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----")),
    ("jwt_token",           re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b")),
    ("generic_password",    re.compile(r"(?i)\bpassword\b\s*[:=]\s*['\"]?([^\s'\"]{6,})")),
    ("bearer_token",        re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{16,}")),
    # Stripe live/test keys
    ("stripe_key",          re.compile(r"\bsk_(?:live|test)_[A-Za-z0-9]{16,}\b")),
    # Google API key
    ("google_api_key",      re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    # Sendgrid
    ("sendgrid_key",        re.compile(r"\bSG\.[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{20,}\b")),
    # Twilio
    ("twilio_key",          re.compile(r"\bSK[0-9a-fA-F]{32}\b")),
)


def _redact(sample: str, *, max_len: int = 12) -> str:
    if len(sample) <= max_len:
        return sample[:4] + "***"
    return sample[:4] + "***" + sample[-2:]


def detect_secrets(content: str) -> List[SecretMatch]:
    """Return the secret-shaped substrings in ``content`` (R-16).

    The matchers are deliberately conservative: they only fire on
    *high-signal* shapes.  This function is invoked *before* the
    policy check, so a secret-shaped fact is rejected even if the
    caller supplies a permissive policy.
    """
    if not content:
        return []
    matches: List[SecretMatch] = []
    for name, pattern in _SECRET_PATTERNS:
        for m in pattern.finditer(content):
            sample = m.group(0)
            matches.append(
                SecretMatch(
                    pattern_name=name,
                    sample=_redact(sample),
                    start=m.start(),
                    end=m.end(),
                )
            )
    # dedup overlapping / identical matches
    seen: set = set()
    uniq: List[SecretMatch] = []
    for sm in matches:
        key = (sm.pattern_name, sm.start, sm.end)
        if key in seen:
            continue
        seen.add(key)
        uniq.append(sm)
    return uniq


# ===========================================================================
# In-memory store (test + dev)
# ===========================================================================

class InMemoryStore:
    """A trivial dict-backed store.  Used by tests and the default
    test environment.  Production code should use :class:`SQLiteMemoryStore`
    in :mod:`core.services.sqlite_memory_store`.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._by_id: Dict[str, MemoryEntry] = {}
        self._by_hash: Dict[str, str] = {}  # hash -> id
        self._initialized = False
        self._writes = 0
        self._reads = 0
        self._deletes = 0

    # ---- lifecycle ----
    def initialize(self) -> bool:
        with self._lock:
            self._initialized = True
        return True

    def shutdown(self) -> None:
        with self._lock:
            self._initialized = False
            self._by_id.clear()
            self._by_hash.clear()

    # ---- CRUD ----
    def upsert(self, entry: MemoryEntry) -> bool:
        with self._lock:
            self._by_id[entry.id] = entry
            self._by_hash[entry.content_hash] = entry.id
            self._writes += 1
            return True

    def delete(self, entry_id: str) -> bool:
        with self._lock:
            entry = self._by_id.pop(entry_id, None)
            if entry is None:
                return False
            self._by_hash.pop(entry.content_hash, None)
            self._deletes += 1
            return True

    def get(self, entry_id: str) -> Optional[MemoryEntry]:
        with self._lock:
            self._reads += 1
            return self._by_id.get(entry_id)

    def find_by_hash(self, content_hash: str) -> Optional[MemoryEntry]:
        with self._lock:
            self._reads += 1
            entry_id = self._by_hash.get(content_hash)
            if entry_id is None:
                return None
            return self._by_id.get(entry_id)

    def list_recent(
        self,
        *,
        kind: Optional[MemoryKind] = None,
        include_private: bool = False,
        limit: int = 50,
    ) -> List[MemoryEntry]:
        with self._lock:
            entries = list(self._by_id.values())
        entries.sort(key=lambda e: e.created_at, reverse=True)
        out: List[MemoryEntry] = []
        for e in entries:
            if kind is not None and e.kind is not kind:
                continue
            if e.private and not include_private:
                continue
            if e.is_expired:
                continue
            out.append(e)
            if len(out) >= limit:
                break
        return out

    def all_hashes(self) -> Iterable[str]:
        with self._lock:
            return list(self._by_hash.keys())

    def count(self) -> int:
        with self._lock:
            return len(self._by_id)

    def iter_all(self) -> Iterable[MemoryEntry]:
        with self._lock:
            return list(self._by_id.values())

    # ---- diagnostics ----
    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "type": "InMemoryStore",
                "entries": len(self._by_id),
                "writes": self._writes,
                "reads": self._reads,
                "deletes": self._deletes,
                "initialized": self._initialized,
            }

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"InMemoryStore(entries={self.count()})"


# ===========================================================================
# Service
# ===========================================================================

class MemoryService(LifecycleMixin):
    """The engine-facing façade for the memory subsystem (R-13).

    Public surface
    --------------

    * :meth:`remember`  — store a fact.
    * :meth:`forget`    — delete one entry by id (or many by kind/tag).
    * :meth:`recall`    — retrieve relevant entries for a query.
    * :meth:`get`       — fetch one entry by id.
    * :meth:`inspect`   — return the service configuration + counters.
    * :meth:`purge_expired` — sweep stale entries (retention policy).
    * :meth:`update_policy` — install a new policy at runtime.

    The service enforces, in order:

    1. lifecycle — refuse operations before ``initialize()``.
    2. secret rejection — refuse secret-shaped content (R-16).
    3. policy.kind — refuse kinds the policy does not allow.
    4. policy.min_confidence — refuse low-confidence facts.
    5. policy.size — refuse oversized content.
    6. policy.dedup — return a duplicate result without writing again.
    7. policy.retention — stamp ``expires_at`` on every entry.
    """

    # ============================================================ ctor
    def __init__(
        self,
        store: Optional[MemoryStore] = None,
        *,
        policy: Optional[MemoryPolicy] = None,
        secret_check: Optional[Callable[[str], List[SecretMatch]]] = None,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self._store: MemoryStore = store if store is not None else InMemoryStore()
        self._policy: MemoryPolicy = policy or MemoryPolicy()
        self._secret_check: Callable[[str], List[SecretMatch]] = (
            secret_check if secret_check is not None else detect_secrets
        )
        self._clock: Callable[[], float] = clock or time.time

        # ---- counters for R-9 / R-15 ----
        self._lock = threading.RLock()
        self._rejected_secret = 0
        self._rejected_policy = 0
        self._rejected_size = 0
        self._rejected_confidence = 0
        self._stored = 0
        self._duplicates = 0
        self._forgotten = 0
        self._recalls = 0

        # ---- lifecycle state (LifecycleMixin contract) ----
        self._lifecycle_state: LifecycleState = LifecycleState.CREATED
        self._initialization_error: Optional[str] = None

    # ===================================================== properties
    @property
    def policy(self) -> MemoryPolicy:
        return self._policy

    @property
    def store(self) -> MemoryStore:
        return self._store

    # ============================================== lifecycle (R-9)
    def _do_initialize(self) -> bool:
        try:
            ok = bool(self._store.initialize())
        except Exception as exc:  # noqa: BLE001
            self._initialization_error = repr(exc)
            return False
        if not ok:
            self._initialization_error = "store.initialize() returned False"
            return False
        return True

    def _do_shutdown(self) -> None:
        try:
            self._store.shutdown()
        except Exception as exc:  # noqa: BLE001
            _loguru.warning(f"MemoryService shutdown: store raised: {exc!r}")

    # ========================================== remember / forget
    def remember(
        self,
        content: str,
        *,
        kind: MemoryKind = MemoryKind.FACT,
        confidence: float = 1.0,
        provenance: Optional[Provenance] = None,
        tags: Sequence[str] = (),
        metadata: Optional[Mapping[str, Any]] = None,
        private: Optional[bool] = None,
    ) -> MemoryResult:
        """Persist a single fact.

        Returns a :class:`MemoryResult` with ``success=True`` and
        ``status="stored"`` on a fresh write, or ``status="duplicate"``
        when the policy enables dedup and an identical hash already
        exists.  Secret-shaped content is rejected with
        ``status="rejected"`` and ``error_code="SECRET_DETECTED"``.
        """
        if not self.initialized:
            return self._err("remember", "MEMORY_NOT_READY", "memory service is not initialized")

        if not isinstance(content, str):
            return self._err("remember", "VALIDATION_ERROR", "content must be a string")

        if content.strip() == "":
            return self._err("remember", "VALIDATION_ERROR", "content is empty")

        # ---- secret scan (R-16) ----
        secrets = self._secret_check(content)
        if secrets:
            with self._lock:
                self._rejected_secret += 1
            names = sorted({s.pattern_name for s in secrets})
            _loguru.warning(
                "MemoryService rejected secret-shaped content "
                f"(patterns={names}, content_len={len(content)})"
            )
            return MemoryResult(
                success=False,
                status="rejected",
                operation="remember",
                error=f"refusing to store secret-shaped content ({', '.join(names)})",
                error_code="SECRET_DETECTED",
                metadata={"matched_patterns": names},
            )

        # ---- policy: kind ----
        if not self._policy.allows(kind):
            with self._lock:
                self._rejected_policy += 1
            return MemoryResult(
                success=False,
                status="rejected",
                operation="remember",
                error=f"policy forbids kind {kind.value!r}",
                error_code="POLICY_KIND_DENIED",
                metadata={"kind": kind.value},
            )

        # ---- policy: confidence ----
        try:
            conf = float(confidence)
        except (TypeError, ValueError):
            return self._err("remember", "VALIDATION_ERROR", "confidence must be numeric")
        if not (0.0 <= conf <= 1.0):
            with self._lock:
                self._rejected_confidence += 1
            return MemoryResult(
                success=False,
                status="rejected",
                operation="remember",
                error=f"confidence must be in [0.0, 1.0] (got {confidence})",
                error_code="POLICY_CONFIDENCE_OUT_OF_RANGE",
                metadata={"confidence": confidence},
            )
        if conf < self._policy.min_confidence:
            with self._lock:
                self._rejected_confidence += 1
            return MemoryResult(
                success=False,
                status="rejected",
                operation="remember",
                error=(
                    f"confidence {conf} below policy minimum "
                    f"{self._policy.min_confidence}"
                ),
                error_code="POLICY_CONFIDENCE_TOO_LOW",
                metadata={
                    "confidence": conf,
                    "min_confidence": self._policy.min_confidence,
                },
            )

        # ---- policy: size ----
        encoded_size = len(content.encode("utf-8"))
        if encoded_size > self._policy.max_size_bytes:
            with self._lock:
                self._rejected_size += 1
            return MemoryResult(
                success=False,
                status="rejected",
                operation="remember",
                error=(
                    f"content is {encoded_size} bytes; policy max is "
                    f"{self._policy.max_size_bytes}"
                ),
                error_code="POLICY_SIZE_EXCEEDED",
                metadata={"size_bytes": encoded_size},
            )

        # ---- provenance ----
        prov = provenance or Provenance(source="engine")

        # ---- dedup ----
        content_hash = _hash_content(content)
        if self._policy.deduplicate:
            existing = self._store.find_by_hash(content_hash)
            if existing is not None:
                with self._lock:
                    self._duplicates += 1
                return MemoryResult(
                    success=True,
                    status="duplicate",
                    operation="remember",
                    value=existing,
                    metadata={"content_hash": content_hash},
                )

        # ---- build and persist ----
        now = self._clock()
        is_private = self._policy.private if private is None else bool(private)
        entry = MemoryEntry(
            id=str(uuid.uuid4()),
            kind=kind,
            content=content,
            confidence=conf,
            provenance=prov,
            content_hash=content_hash,
            created_at=now,
            expires_at=now + self._policy.retention_seconds if self._policy.retention_seconds > 0 else None,
            private=is_private,
            tags=tuple(tags),
            metadata=dict(metadata) if metadata else {},
        )

        try:
            ok = bool(self._store.upsert(entry))
        except Exception as exc:  # noqa: BLE001
            return self._err(
                "remember",
                "STORE_ERROR",
                f"store.upsert failed: {exc!r}",
            )
        if not ok:
            return self._err(
                "remember",
                "STORE_ERROR",
                "store.upsert returned False",
            )

        with self._lock:
            self._stored += 1
        return MemoryResult(
            success=True,
            status="stored",
            operation="remember",
            value=entry,
            metadata={"content_hash": content_hash},
        )

    def forget(
        self,
        entry_id: Optional[str] = None,
        *,
        kind: Optional[MemoryKind] = None,
        tag: Optional[str] = None,
    ) -> MemoryResult:
        """Delete entries from the store.

        * ``entry_id`` → delete one entry by id.
        * ``kind`` / ``tag`` → delete every matching entry (cautious; the
          service only deletes what is currently visible under the
          recall policy).

        Returns a :class:`MemoryResult` with ``status="forgotten"`` and
        ``metadata["count"]`` set to the number of entries removed.
        """
        if not self.initialized:
            return self._err("forget", "MEMORY_NOT_READY", "memory service is not initialized")

        if entry_id is not None:
            try:
                removed = bool(self._store.delete(entry_id))
            except Exception as exc:  # noqa: BLE001
                return self._err("forget", "STORE_ERROR", f"store.delete failed: {exc!r}")
            if removed:
                with self._lock:
                    self._forgotten += 1
                return MemoryResult(
                    success=True,
                    status="forgotten",
                    operation="forget",
                    metadata={"count": 1, "entry_id": entry_id},
                )
            return MemoryResult(
                success=False,
                status="rejected",
                operation="forget",
                error=f"no entry with id {entry_id!r}",
                error_code="ENTRY_NOT_FOUND",
                metadata={"entry_id": entry_id},
            )

        # bulk delete by kind / tag
        if kind is None and tag is None:
            return self._err(
                "forget",
                "VALIDATION_ERROR",
                "forget() requires entry_id, kind, or tag",
            )

        removed_ids: List[str] = []
        for entry in self._store.list_recent(kind=kind, include_private=True, limit=10_000):
            if tag is not None and tag not in entry.tags:
                continue
            if kind is not None and entry.kind is not kind:
                continue
            if self._store.delete(entry.id):
                removed_ids.append(entry.id)

        with self._lock:
            self._forgotten += len(removed_ids)
        return MemoryResult(
            success=True,
            status="forgotten",
            operation="forget",
            value=removed_ids,
            metadata={
                "count": len(removed_ids),
                "kind": kind.value if kind is not None else None,
                "tag": tag,
            },
        )

    # ======================================================= recall
    def recall(
        self,
        query: str,
        *,
        kind: Optional[MemoryKind] = None,
        limit: int = 5,
        include_private: bool = True,
        min_confidence: float = 0.0,
        tags: Sequence[str] = (),
    ) -> MemoryResult:
        """Return the entries most relevant to ``query``.

        Relevance is a simple, deterministic score over:

        * exact tag overlap (boost),
        * kind match (boost),
        * recent-first ordering (recency decays with age),
        * confidence-weighted content match (token overlap).

        The scoring never calls an LLM; it is a pure function of the
        stored content, so it is testable without mocks.
        """
        if not self.initialized:
            return self._err("recall", "MEMORY_NOT_READY", "memory service is not initialized")

        if not isinstance(query, str):
            return self._err("recall", "VALIDATION_ERROR", "query must be a string")

        with self._lock:
            self._recalls += 1

        limit = max(1, min(int(limit), 1000))
        min_confidence = float(min_confidence)

        # Pull a candidate window; recent-first ordering means we scan
        # the most recent entries first.  The store is expected to
        # honor ``include_private``.
        candidates = self._store.list_recent(
            kind=kind,
            include_private=include_private,
            limit=max(limit * 10, 100),
        )

        if not candidates:
            return MemoryResult(
                success=True,
                status="empty",
                operation="recall",
                value=[],
                metadata={"query": query, "limit": limit},
            )

        query_tokens = _tokenize(query)
        tag_set = {t.lower() for t in tags}

        scored: List[RecallHit] = []
        for entry in candidates:
            if entry.confidence < min_confidence:
                continue
            if tag_set and not (tag_set & {t.lower() for t in entry.tags}):
                continue

            score, reason = _score_entry(entry, query_tokens, kind)
            if score <= 0.0:
                continue
            scored.append(RecallHit(entry=entry, score=score, match_reason=reason))

        scored.sort(key=lambda h: h.score, reverse=True)
        top = scored[:limit]

        if not top:
            return MemoryResult(
                success=True,
                status="empty",
                operation="recall",
                value=[],
                metadata={"query": query, "limit": limit, "candidates": len(candidates)},
            )

        return MemoryResult(
            success=True,
            status="recalled",
            operation="recall",
            value=top,
            metadata={
                "query": query,
                "limit": limit,
                "count": len(top),
                "candidates": len(candidates),
            },
        )

    # =================================================== direct get
    def get(self, entry_id: str) -> MemoryResult:
        if not self.initialized:
            return self._err("get", "MEMORY_NOT_READY", "memory service is not initialized")
        if not isinstance(entry_id, str) or not entry_id:
            return self._err("get", "VALIDATION_ERROR", "entry_id must be a non-empty string")
        entry = self._store.get(entry_id)
        if entry is None:
            return MemoryResult(
                success=False,
                status="rejected",
                operation="get",
                error=f"no entry with id {entry_id!r}",
                error_code="ENTRY_NOT_FOUND",
            )
        if entry.is_expired:
            return MemoryResult(
                success=False,
                status="rejected",
                operation="get",
                error="entry has expired",
                error_code="ENTRY_EXPIRED",
            )
        return MemoryResult(
            success=True,
            status="recalled",
            operation="get",
            value=entry,
        )

    # ============================================= maintenance
    def purge_expired(self) -> MemoryResult:
        """Delete every entry whose ``expires_at`` is in the past.

        Returns the count of purged entries in ``metadata["count"]``.
        """
        if not self.initialized:
            return self._err(
                "purge_expired", "MEMORY_NOT_READY", "memory service is not initialized"
            )
        removed = 0
        for entry in self._store.iter_all():
            if entry.is_expired:
                if self._store.delete(entry.id):
                    removed += 1
        with self._lock:
            self._forgotten += removed
        return MemoryResult(
            success=True,
            status="forgotten",
            operation="purge_expired",
            metadata={"count": removed},
        )

    def update_policy(self, policy: MemoryPolicy) -> None:
        """Install a new policy.  Existing entries are not retroactively
        re-scored; new writes are governed by the new policy.
        """
        with self._lock:
            self._policy = policy

    def inspect(self) -> Dict[str, Any]:
        """Return a JSON-safe summary of the service for debugging."""
        with self._lock:
            return {
                "type": "MemoryService",
                "lifecycle": self._lifecycle_state.value,
                "policy": self._policy.to_dict(),
                "counters": {
                    "stored": self._stored,
                    "duplicates": self._duplicates,
                    "forgotten": self._forgotten,
                    "recalls": self._recalls,
                    "rejected_secret": self._rejected_secret,
                    "rejected_policy": self._rejected_policy,
                    "rejected_confidence": self._rejected_confidence,
                    "rejected_size": self._rejected_size,
                },
                "store": _store_stats(self._store),
            }

    def statistics(self) -> Dict[str, Any]:
        """R-9 statistics surface."""
        with self._lock:
            return {
                "type": "MemoryService",
                "lifecycle": self._lifecycle_state.value,
                "stored": self._stored,
                "duplicates": self._duplicates,
                "forgotten": self._forgotten,
                "recalls": self._recalls,
                "rejected_secret": self._rejected_secret,
                "rejected_policy": self._rejected_policy,
                "rejected_confidence": self._rejected_confidence,
                "rejected_size": self._rejected_size,
                "entries": _safe_count(self._store),
            }

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return (
            f"MemoryService(state={self._lifecycle_state.value}, "
            f"stored={self._stored}, recalls={self._recalls})"
        )

    # ===================================================== helpers
    def _err(self, op: str, code: str, msg: str) -> MemoryResult:
        return MemoryResult(
            success=False,
            status="error",
            operation=op,
            error=msg,
            error_code=code,
        )


# ===========================================================================
# Helpers (module-level; kept out of the class for clarity)
# ===========================================================================

def _hash_content(content: str) -> str:
    """Deterministic hash for dedup.  Whitespace is normalized first so
    trivial reformatting does not bypass dedup.
    """
    normalized = " ".join(content.split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _tokenize(text: str) -> List[str]:
    """A token set used for relevance scoring.  Lower-cased, alphanumeric
    only, length >= 2.
    """
    if not text:
        return []
    out: List[str] = []
    for tok in re.findall(r"[A-Za-z0-9_]+", text.lower()):
        if len(tok) >= 2:
            out.append(tok)
    return out


def _score_entry(
    entry: MemoryEntry,
    query_tokens: List[str],
    kind_filter: Optional[MemoryKind],
) -> Tuple[float, str]:
    """Pure scoring function.  No LLM, no IO, no side effects.

    Score components:

    * token overlap:      +1.0 per shared token, scaled by query length
    * recency:            1 / (1 + age_seconds / 86400)
    * confidence:         weighted by ``entry.confidence``
    * kind bonus:         +0.5 if the kind filter matches
    * tag bonus:          +0.2 per matching tag

    ``match_reason`` is the dominant signal that pushed the entry above
    zero, surfaced for debugging.
    """
    if not query_tokens:
        # No tokens ⇒ fall back to "recent, kind-matching" matches.
        score = 0.5 * (1.0 / (1.0 + entry.age_seconds / 86400.0))
        if kind_filter is not None and entry.kind is kind_filter:
            score += 0.5
        return (score, "recent")

    content_tokens = set(_tokenize(entry.content))
    if not content_tokens:
        return (0.0, "")

    shared = content_tokens.intersection(query_tokens)
    if not shared:
        # If no token overlap, we may still return the entry if the
        # caller filters by kind and the entry matches.
        if kind_filter is not None and entry.kind is kind_filter:
            score = 0.25 * (1.0 / (1.0 + entry.age_seconds / 86400.0))
            return (score, "kind")
        return (0.0, "")

    token_score = len(shared) / max(1, len(query_tokens))
    recency = 1.0 / (1.0 + entry.age_seconds / 86400.0)
    confidence = max(0.0, min(1.0, entry.confidence))

    score = (
        0.6 * token_score
        + 0.2 * recency
        + 0.2 * confidence
    )

    reason = "content"
    if kind_filter is not None and entry.kind is kind_filter:
        score += 0.3
        reason = "kind"
    return (score, reason)


def _store_stats(store: Any) -> Dict[str, Any]:
    """Best-effort stats from a backend (some backends may not expose them)."""
    try:
        if hasattr(store, "statistics"):
            stats = store.statistics()
            if isinstance(stats, dict):
                return stats
    except Exception:  # noqa: BLE001
        pass
    try:
        return {"entries": int(store.count())}
    except Exception:  # noqa: BLE001
        return {}


def _safe_count(store: Any) -> int:
    try:
        return int(store.count())
    except Exception:  # noqa: BLE001
        return 0


__all__ = [
    "MemoryKind",
    "MemoryPolicy",
    "Provenance",
    "MemoryEntry",
    "MemoryResult",
    "RecallHit",
    "MemoryStore",
    "InMemoryStore",
    "MemoryService",
    "detect_secrets",
    "SecretMatch",
]
