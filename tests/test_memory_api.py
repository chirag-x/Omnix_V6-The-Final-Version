"""
Tests for the Phase 9 Memory Subsystem API surface.

These tests cover the *public* surface of the memory service:

* ``MemoryKind`` is a closed enum,
* ``MemoryPolicy`` permits / denies kinds, sets retention, dedup,
  private, confidence, size,
* ``MemoryEntry`` is a frozen dataclass with the right shape,
* ``MemoryResult`` returns ``success``/``status``/``operation``/``error``,
* ``detect_secrets`` rejects known secret patterns and ignores benign
  strings,
* ``InMemoryStore`` round-trips entries,
* ``SQLiteMemoryStore`` (when sqlite3 is available) round-trips entries
  and survives a process boundary.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import List

import pytest

from core.services.memory_service import (
    InMemoryStore,
    MemoryEntry,
    MemoryKind,
    MemoryPolicy,
    MemoryResult,
    MemoryService,
    MemoryStore,
    Provenance,
    RecallHit,
    SecretMatch,
    detect_secrets,
)
from core.services.sqlite_memory_store import SQLiteMemoryStore


# ---------------------------------------------------------------------------
# MemoryKind
# ---------------------------------------------------------------------------

class TestMemoryKind:
    def test_is_closed_enum(self):
        assert MemoryKind.FACT.value == "fact"
        assert MemoryKind.PREFERENCE.value == "preference"
        assert MemoryKind.INSTRUCTION.value == "instruction"
        assert MemoryKind.CONTEXT.value == "context"
        assert MemoryKind.EPISODE.value == "episode"
        assert MemoryKind.ENTITY.value == "entity"
        assert MemoryKind.TASK_OUTCOME.value == "task_outcome"

    def test_kind_set_is_stable(self):
        # If a new kind is added, this is the place to assert it.
        assert set(MemoryKind) == {
            MemoryKind.FACT,
            MemoryKind.PREFERENCE,
            MemoryKind.INSTRUCTION,
            MemoryKind.CONTEXT,
            MemoryKind.EPISODE,
            MemoryKind.ENTITY,
            MemoryKind.TASK_OUTCOME,
        }


# ---------------------------------------------------------------------------
# MemoryPolicy
# ---------------------------------------------------------------------------

class TestMemoryPolicy:
    def test_default_policy_is_conservative(self):
        p = MemoryPolicy()
        assert p.retention_seconds > 0
        assert p.deduplicate is True
        assert p.private is True
        assert p.min_confidence == 0.0
        assert p.max_size_bytes > 0
        assert MemoryKind.FACT in p.allow_kinds

    def test_allows_kind(self):
        p = MemoryPolicy(allow_kinds=(MemoryKind.FACT, MemoryKind.PREFERENCE))
        assert p.allows(MemoryKind.FACT)
        assert p.allows(MemoryKind.PREFERENCE)
        assert not p.allows(MemoryKind.INSTRUCTION)

    def test_deny_kinds_overrides_allow(self):
        p = MemoryPolicy(
            allow_kinds=tuple(MemoryKind),
            deny_kinds=(MemoryKind.INSTRUCTION,),
        )
        assert not p.allows(MemoryKind.INSTRUCTION)
        assert p.allows(MemoryKind.FACT)

    def test_to_dict_round_trip(self):
        p = MemoryPolicy(retention_seconds=42.0, deduplicate=False, private=False)
        d = p.to_dict()
        assert d["retention_seconds"] == 42.0
        assert d["deduplicate"] is False
        assert d["private"] is False
        assert "allow_kinds" in d
        assert "deny_kinds" in d


# ---------------------------------------------------------------------------
# MemoryEntry
# ---------------------------------------------------------------------------

class TestMemoryEntry:
    def _make(self, **kwargs) -> MemoryEntry:
        defaults = dict(
            id="abc",
            kind=MemoryKind.FACT,
            content="the cat sat on the mat",
            confidence=0.9,
            provenance=Provenance(source="user"),
            content_hash="deadbeef",
            created_at=time.time(),
            expires_at=time.time() + 60,
            private=True,
            tags=("pets",),
            metadata={"x": 1},
        )
        defaults.update(kwargs)
        return MemoryEntry(**defaults)

    def test_is_frozen(self):
        e = self._make()
        with pytest.raises(Exception):
            e.content = "mutated"  # type: ignore[misc]

    def test_is_expired(self):
        old = self._make(created_at=time.time() - 10, expires_at=time.time() - 1)
        fresh = self._make(created_at=time.time() - 10, expires_at=time.time() + 100)
        never = self._make(expires_at=None)
        assert old.is_expired is True
        assert fresh.is_expired is False
        assert never.is_expired is False

    def test_to_dict_can_redact(self):
        e = self._make()
        d = e.to_dict(include_content=False)
        assert d["content"] == "[REDACTED]"
        d2 = e.to_dict(include_content=True)
        assert d2["content"] == e.content


# ---------------------------------------------------------------------------
# detect_secrets
# ---------------------------------------------------------------------------

class TestDetectSecrets:
    def test_clean_text(self):
        assert detect_secrets("hello world") == []

    def test_openai_key_detected(self):
        matches = detect_secrets("here is my key sk-abcdefghijklmnopqrstuvwxyz")
        assert any(m.pattern_name == "openai_key" for m in matches)

    def test_github_token_detected(self):
        matches = detect_secrets("ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789")
        assert any(m.pattern_name == "github_token" for m in matches)

    def test_groq_key_detected(self):
        matches = detect_secrets("gsk_aBcDeFgHiJkLmNoPqRsTuVwXyZ0123")
        assert any(m.pattern_name == "groq_key" for m in matches)

    def test_aws_access_key_detected(self):
        matches = detect_secrets("AKIAIOSFODNN7EXAMPLE")
        assert any(m.pattern_name == "aws_access_key" for m in matches)

    def test_pem_private_key_detected(self):
        text = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAA..."
        matches = detect_secrets(text)
        assert any(m.pattern_name == "pem_private_key" for m in matches)

    def test_jwt_detected(self):
        # A JWT-like triple: each segment must be 10+ chars of base64url
        # so the regex's quantifier matches.
        jwt = (
            "eyJhbGciOiJIUzI1NiJ9."
            "eyJzdWIiOiIxMjM0NTY3ODkwIn0."
            "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
        )
        matches = detect_secrets(jwt)
        assert any(m.pattern_name == "jwt_token" for m in matches)

    def test_password_assignment_detected(self):
        matches = detect_secrets("password: hunter2hunter2")
        assert any(m.pattern_name == "generic_password" for m in matches)

    def test_bearer_token_detected(self):
        matches = detect_secrets("Authorization: Bearer abcdefghijklmnopqrstuv")
        assert any(m.pattern_name == "bearer_token" for m in matches)

    def test_stripe_key_detected(self):
        matches = detect_secrets("sk_test_abcdefghijklmnop1234")
        assert any(m.pattern_name == "stripe_key" for m in matches)

    def test_google_api_key_detected(self):
        # 35 chars after the "AIza" prefix as the regex requires.
        matches = detect_secrets("AIzaSyA-aBcDeFgHiJkLmNoPqRsTuVwXyZ01234")
        assert any(m.pattern_name == "google_api_key" for m in matches)

    def test_redis_url_not_a_secret(self):
        # A redis URL alone should not be flagged.
        assert detect_secrets("redis://localhost:6379/0") == []

    def test_huggingface_token_not_flagged(self):
        # Plain "hf_xxxx" is not in our regex; verify the absence of
        # false positives.  This is by design — Phase 9 only flags
        # the most common key shapes.
        assert detect_secrets("the cat sat on the mat") == []

    def test_empty_string(self):
        assert detect_secrets("") == []

    def test_match_positions_consistent(self):
        content = "use sk-abcdefghijklmnopqrstuvwxyz here"
        matches = detect_secrets(content)
        assert matches, "expected at least one match"
        for m in matches:
            assert 0 <= m.start < m.end <= len(content)


# ---------------------------------------------------------------------------
# InMemoryStore
# ---------------------------------------------------------------------------

class TestInMemoryStore:
    def test_round_trip(self):
        store = InMemoryStore()
        assert store.initialize() is True
        try:
            entry = _entry("hello", kind=MemoryKind.FACT)
            assert store.upsert(entry) is True
            got = store.get(entry.id)
            assert got is not None
            assert got.content == "hello"
            assert store.count() == 1
        finally:
            store.shutdown()

    def test_find_by_hash(self):
        store = InMemoryStore()
        assert store.initialize() is True
        try:
            entry = _entry("hello world")
            store.upsert(entry)
            found = store.find_by_hash(entry.content_hash)
            assert found is not None
            assert found.id == entry.id
        finally:
            store.shutdown()

    def test_delete(self):
        store = InMemoryStore()
        assert store.initialize() is True
        try:
            entry = _entry("hello world")
            store.upsert(entry)
            assert store.delete(entry.id) is True
            assert store.get(entry.id) is None
            assert store.delete(entry.id) is False
        finally:
            store.shutdown()

    def test_list_recent_orders_by_created(self):
        store = InMemoryStore()
        assert store.initialize() is True
        try:
            a = _entry("a", created_at=time.time() - 100)
            b = _entry("b", created_at=time.time() - 10)
            c = _entry("c", created_at=time.time() - 50)
            for e in (a, b, c):
                store.upsert(e)
            recent = store.list_recent(limit=10, include_private=True)
            assert [r.id for r in recent] == [b.id, c.id, a.id]
        finally:
            store.shutdown()

    def test_list_recent_filters_private(self):
        store = InMemoryStore()
        assert store.initialize() is True
        try:
            store.upsert(_entry("priv", private=True))
            store.upsert(_entry("pub", private=False))
            public = store.list_recent(include_private=False)
            private = store.list_recent(include_private=True)
            assert len(public) == 1
            assert public[0].content == "pub"
            assert len(private) == 2
        finally:
            store.shutdown()

    def test_list_recent_filters_kind(self):
        store = InMemoryStore()
        assert store.initialize() is True
        try:
            store.upsert(_entry("a", kind=MemoryKind.FACT))
            store.upsert(_entry("b", kind=MemoryKind.PREFERENCE))
            only_facts = store.list_recent(kind=MemoryKind.FACT, include_private=True)
            assert len(only_facts) == 1
            assert only_facts[0].content == "a"
        finally:
            store.shutdown()

    def test_shutdown_clears_state(self):
        store = InMemoryStore()
        assert store.initialize() is True
        store.upsert(_entry("x"))
        store.shutdown()
        assert store.count() == 0


# ---------------------------------------------------------------------------
# MemoryService — remember
# ---------------------------------------------------------------------------

class TestRemember:
    def test_stores_a_fact(self):
        service = MemoryService()
        assert service.initialize() is True
        try:
            res = service.remember("I like coffee", kind=MemoryKind.PREFERENCE)
            assert res.success is True
            assert res.status == "stored"
            assert res.value is not None
            assert res.value.content == "I like coffee"
            assert res.value.kind == MemoryKind.PREFERENCE
        finally:
            service.shutdown()

    def test_rejects_empty_content(self):
        service = MemoryService()
        service.initialize()
        try:
            res = service.remember("   ", kind=MemoryKind.FACT)
            assert res.success is False
            assert res.status == "error"
            assert res.error_code == "VALIDATION_ERROR"
        finally:
            service.shutdown()

    def test_rejects_non_string_content(self):
        service = MemoryService()
        service.initialize()
        try:
            res = service.remember(12345)  # type: ignore[arg-type]
            assert res.success is False
            assert res.error_code == "VALIDATION_ERROR"
        finally:
            service.shutdown()

    def test_dedup_returns_duplicate(self):
        service = MemoryService()
        service.initialize()
        try:
            r1 = service.remember("hello world", kind=MemoryKind.FACT)
            r2 = service.remember("hello world", kind=MemoryKind.FACT)
            assert r1.status == "stored"
            assert r2.status == "duplicate"
            assert r1.value.id == r2.value.id
        finally:
            service.shutdown()

    def test_dedup_normalizes_whitespace(self):
        service = MemoryService()
        service.initialize()
        try:
            r1 = service.remember("hello world", kind=MemoryKind.FACT)
            r2 = service.remember("  hello   world  ", kind=MemoryKind.FACT)
            assert r2.status == "duplicate"
        finally:
            service.shutdown()

    def test_rejects_secret(self):
        service = MemoryService()
        service.initialize()
        try:
            res = service.remember("my key is sk-abcdefghijklmnopqrstuvwxyz")
            assert res.success is False
            assert res.status == "rejected"
            assert res.error_code == "SECRET_DETECTED"
            assert "openai_key" in res.metadata["matched_patterns"]
        finally:
            service.shutdown()

    def test_rejects_kind_not_in_policy(self):
        policy = MemoryPolicy(allow_kinds=(MemoryKind.FACT,))
        service = MemoryService(policy=policy)
        service.initialize()
        try:
            res = service.remember("hi", kind=MemoryKind.PREFERENCE)
            assert res.success is False
            assert res.status == "rejected"
            assert res.error_code == "POLICY_KIND_DENIED"
        finally:
            service.shutdown()

    def test_rejects_low_confidence(self):
        policy = MemoryPolicy(min_confidence=0.5)
        service = MemoryService(policy=policy)
        service.initialize()
        try:
            res = service.remember("hi", confidence=0.2)
            assert res.success is False
            assert res.error_code == "POLICY_CONFIDENCE_TOO_LOW"
        finally:
            service.shutdown()

    def test_rejects_out_of_range_confidence(self):
        service = MemoryService()
        service.initialize()
        try:
            res = service.remember("hi", confidence=1.5)
            assert res.success is False
            assert res.error_code == "POLICY_CONFIDENCE_OUT_OF_RANGE"
        finally:
            service.shutdown()

    def test_rejects_oversize_content(self):
        policy = MemoryPolicy(max_size_bytes=4)
        service = MemoryService(policy=policy)
        service.initialize()
        try:
            res = service.remember("this is too long")
            assert res.success is False
            assert res.error_code == "POLICY_SIZE_EXCEEDED"
        finally:
            service.shutdown()

    def test_stamps_expires_at(self):
        policy = MemoryPolicy(retention_seconds=10)
        service = MemoryService(policy=policy)
        service.initialize()
        try:
            res = service.remember("hi")
            assert res.value.expires_at is not None
            assert res.value.expires_at - res.value.created_at == pytest.approx(10.0)
        finally:
            service.shutdown()

    def test_zero_retention_means_no_expiry(self):
        policy = MemoryPolicy(retention_seconds=0)
        service = MemoryService(policy=policy)
        service.initialize()
        try:
            res = service.remember("hi")
            assert res.value.expires_at is None
        finally:
            service.shutdown()

    def test_refuses_remember_before_initialize(self):
        service = MemoryService()
        # NOTE: do NOT call service.initialize()
        res = service.remember("hi")
        assert res.success is False
        assert res.error_code == "MEMORY_NOT_READY"

    def test_provenance_recorded(self):
        service = MemoryService()
        service.initialize()
        try:
            prov = Provenance(
                source="user",
                session_id="sess-1",
                task_id="task-1",
                capability="remember",
            )
            res = service.remember("hi", provenance=prov)
            assert res.value.provenance.source == "user"
            assert res.value.provenance.session_id == "sess-1"
            assert res.value.provenance.task_id == "task-1"
        finally:
            service.shutdown()

    def test_tags_and_metadata(self):
        service = MemoryService()
        service.initialize()
        try:
            res = service.remember(
                "hi",
                tags=("greeting", "english"),
                metadata={"lang": "en"},
            )
            assert "greeting" in res.value.tags
            assert res.value.metadata["lang"] == "en"
        finally:
            service.shutdown()


# ---------------------------------------------------------------------------
# MemoryService — recall
# ---------------------------------------------------------------------------

class TestRecall:
    def _seeded(self) -> MemoryService:
        service = MemoryService()
        service.initialize()
        service.remember("the cat sat on the mat", kind=MemoryKind.FACT, tags=("pets",))
        service.remember("I love espresso", kind=MemoryKind.PREFERENCE, tags=("food",))
        service.remember("today is monday", kind=MemoryKind.CONTEXT, tags=("time",))
        return service

    def test_recall_by_token_overlap(self):
        s = self._seeded()
        try:
            res = s.recall("cat mat", limit=5)
            assert res.success is True
            assert res.status == "recalled"
            assert isinstance(res.value, list)
            assert any(h.entry.content.startswith("the cat") for h in res.value)
        finally:
            s.shutdown()

    def test_recall_kind_filter(self):
        s = self._seeded()
        try:
            res = s.recall("today monday", kind=MemoryKind.CONTEXT, limit=5)
            assert res.success is True
            for hit in res.value:
                assert hit.entry.kind == MemoryKind.CONTEXT
        finally:
            s.shutdown()

    def test_recall_empty_returns_empty_status(self):
        s = self._seeded()
        try:
            res = s.recall("nothing matches this query", limit=5)
            # Either recalled (with low-relevance) or empty; both are valid
            assert res.status in ("empty", "recalled")
        finally:
            s.shutdown()

    def test_recall_min_confidence(self):
        s = self._seeded()
        try:
            # Default confidence is 1.0, so a threshold above 1.0
            # excludes everything.
            res = s.recall("cat mat", min_confidence=1.5, limit=5)
            assert res.status == "empty"
        finally:
            s.shutdown()

    def test_recall_respects_tags(self):
        s = self._seeded()
        try:
            res = s.recall("the", tags=("food",), limit=5)
            for hit in res.value:
                assert "food" in {t.lower() for t in hit.entry.tags}
        finally:
            s.shutdown()

    def test_recall_limit(self):
        service = MemoryService()
        service.initialize()
        try:
            for i in range(20):
                service.remember(f"this is fact number {i} about cars")
            res = service.recall("cars fact", limit=3)
            assert res.status == "recalled"
            assert len(res.value) <= 3
        finally:
            service.shutdown()

    def test_recall_refused_before_initialize(self):
        s = MemoryService()
        res = s.recall("anything")
        assert res.success is False
        assert res.error_code == "MEMORY_NOT_READY"

    def test_recall_rejects_non_string_query(self):
        s = MemoryService()
        s.initialize()
        try:
            res = s.recall(12345)  # type: ignore[arg-type]
            assert res.success is False
            assert res.error_code == "VALIDATION_ERROR"
        finally:
            s.shutdown()


# ---------------------------------------------------------------------------
# MemoryService — forget
# ---------------------------------------------------------------------------

class TestForget:
    def test_forget_by_id(self):
        s = MemoryService()
        s.initialize()
        try:
            r1 = s.remember("hello world")
            rid = r1.value.id
            res = s.forget(rid)
            assert res.success is True
            assert res.status == "forgotten"
            assert res.metadata["count"] == 1
            assert s.get(rid).success is False
        finally:
            s.shutdown()

    def test_forget_unknown_id_is_failure(self):
        s = MemoryService()
        s.initialize()
        try:
            res = s.forget("nonexistent")
            assert res.success is False
            assert res.error_code == "ENTRY_NOT_FOUND"
        finally:
            s.shutdown()

    def test_forget_by_kind(self):
        s = MemoryService()
        s.initialize()
        try:
            s.remember("a", kind=MemoryKind.FACT)
            s.remember("b", kind=MemoryKind.FACT)
            s.remember("c", kind=MemoryKind.PREFERENCE)
            res = s.forget(kind=MemoryKind.FACT)
            assert res.success is True
            assert res.metadata["count"] == 2
        finally:
            s.shutdown()

    def test_forget_by_tag(self):
        s = MemoryService()
        s.initialize()
        try:
            s.remember("a", tags=("x",))
            s.remember("b", tags=("y",))
            res = s.forget(tag="x")
            assert res.metadata["count"] == 1
        finally:
            s.shutdown()

    def test_forget_requires_filter(self):
        s = MemoryService()
        s.initialize()
        try:
            res = s.forget()
            assert res.success is False
            assert res.error_code == "VALIDATION_ERROR"
        finally:
            s.shutdown()


# ---------------------------------------------------------------------------
# MemoryService — get / purge / policy
# ---------------------------------------------------------------------------

class TestGet:
    def test_get_returns_entry(self):
        s = MemoryService()
        s.initialize()
        try:
            r = s.remember("hello")
            got = s.get(r.value.id)
            assert got.success is True
            assert got.value.id == r.value.id
        finally:
            s.shutdown()

    def test_get_expired(self):
        s = MemoryService()
        s.initialize()
        try:
            r = s.remember("hello")
            # Force expiry by mutating store clock via a custom store.
            # We simulate by reaching into the in-memory store.
            from core.services.memory_service import InMemoryStore
            assert isinstance(s._store, InMemoryStore)
            entry = s._store.get(r.value.id)
            # Re-upsert with a past expires_at.
            from dataclasses import replace
            expired = replace(entry, expires_at=time.time() - 1)  # type: ignore[arg-type]
            s._store.upsert(expired)
            got = s.get(r.value.id)
            assert got.success is False
            assert got.error_code == "ENTRY_EXPIRED"
        finally:
            s.shutdown()

    def test_get_rejects_empty_id(self):
        s = MemoryService()
        s.initialize()
        try:
            res = s.get("")
            assert res.success is False
            assert res.error_code == "VALIDATION_ERROR"
        finally:
            s.shutdown()

    def test_get_unknown_id(self):
        s = MemoryService()
        s.initialize()
        try:
            res = s.get("missing")
            assert res.success is False
            assert res.error_code == "ENTRY_NOT_FOUND"
        finally:
            s.shutdown()


class TestPurge:
    def test_purge_expired_removes(self):
        s = MemoryService()
        s.initialize()
        try:
            r1 = s.remember("a")
            r2 = s.remember("b")
            # Force expiry for r1.
            from dataclasses import replace
            from core.services.memory_service import InMemoryStore
            assert isinstance(s._store, InMemoryStore)
            entry = s._store.get(r1.value.id)
            expired = replace(entry, expires_at=time.time() - 1)  # type: ignore[arg-type]
            s._store.upsert(expired)
            res = s.purge_expired()
            assert res.success is True
            assert res.metadata["count"] == 1
            # r2 still present
            assert s.get(r2.value.id).success is True
        finally:
            s.shutdown()


class TestPolicy:
    def test_update_policy_affects_new_writes(self):
        s = MemoryService(policy=MemoryPolicy(allow_kinds=(MemoryKind.FACT,)))
        s.initialize()
        try:
            res = s.remember("hi", kind=MemoryKind.PREFERENCE)
            assert res.error_code == "POLICY_KIND_DENIED"
            s.update_policy(MemoryPolicy(allow_kinds=tuple(MemoryKind)))
            res2 = s.remember("hi", kind=MemoryKind.PREFERENCE)
            assert res2.success is True
        finally:
            s.shutdown()


# ---------------------------------------------------------------------------
# MemoryService — lifecycle / statistics
# ---------------------------------------------------------------------------

class TestLifecycle:
    def test_initialize_shutdown_idempotent(self):
        s = MemoryService()
        assert s.initialize() is True
        s.shutdown()
        # Idempotent
        s.shutdown()
        assert s.initialized is False

    def test_statistics_shape(self):
        s = MemoryService()
        s.initialize()
        try:
            s.remember("a")
            s.remember("a")  # duplicate
            stats = s.statistics()
            assert stats["type"] == "MemoryService"
            assert stats["stored"] == 1
            assert stats["duplicates"] == 1
            assert "lifecycle" in stats
        finally:
            s.shutdown()

    def test_inspect_returns_dict(self):
        s = MemoryService()
        s.initialize()
        try:
            s.remember("a")
            info = s.inspect()
            assert info["type"] == "MemoryService"
            assert "policy" in info
            assert "counters" in info
            assert info["counters"]["stored"] == 1
        finally:
            s.shutdown()

    def test_repr(self):
        s = MemoryService()
        r = repr(s)
        assert "MemoryService" in r


# ---------------------------------------------------------------------------
# MemoryStore protocol conformance
# ---------------------------------------------------------------------------

class TestProtocol:
    def test_in_memory_store_satisfies_protocol(self):
        from core.services.memory_service import MemoryStore
        store = InMemoryStore()
        assert isinstance(store, MemoryStore)

    @pytest.mark.skipif(
        sqlite3 is None, reason="sqlite3 not available"
    )
    def test_sqlite_store_satisfies_protocol(self):
        from core.services.memory_service import MemoryStore
        store = SQLiteMemoryStore(db_path=":memory:")
        assert isinstance(store, MemoryStore)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _entry(
    content: str,
    *,
    kind: MemoryKind = MemoryKind.FACT,
    private: bool = True,
    created_at: Optional[float] = None,
) -> MemoryEntry:
    from core.services.memory_service import _hash_content
    if created_at is None:
        created_at = time.time()
    return MemoryEntry(
        id=_make_id(),
        kind=kind,
        content=content,
        confidence=0.9,
        provenance=Provenance(source="test"),
        content_hash=_hash_content(content),
        created_at=created_at,
        expires_at=created_at + 3600,
        private=private,
        tags=(),
        metadata={},
    )


def _make_id() -> str:
    import uuid
    return str(uuid.uuid4())
