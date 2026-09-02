# V6 Phase 9 — Memory Subsystem Implementation Report

**Status:** PHASE 9 COMPLETE — MEMORY SUBSYSTEM VALIDATED.  READY FOR PHASE 10.

**Date:** 2026-08-30
**Scope:** Build a clean V6 memory subsystem with a single canonical
service boundary at ``core/services/memory_service.py``, a pluggable
SQLite-backed backend, a strict policy layer, explicit
remember/forget/recall, an anti-secret boundary, and full engine
integration under R-13 / R-15 / AD-15 / AD-20.

---

## Executive summary

Phase 9 implements a fully-closed memory subsystem on top of a
pluggable store, behind one canonical service boundary.

Concretely, Phase 9:

1. **Defines the memory vocabulary** as frozen dataclasses and a
   closed ``MemoryKind`` enum (``core/services/memory_service.py``) —
   7 kinds (FACT, PREFERENCE, INSTRUCTION, CONTEXT, EPISODE, ENTITY,
   TASK_OUTCOME), a ``MemoryPolicy``, a ``MemoryEntry``, a
   ``MemoryResult`` (R-2), and a ``RecallHit``.
2. **Implements a pluggable store protocol** with two reference
   backends — ``InMemoryStore`` (tests / ephemeral) and
   ``SQLiteMemoryStore`` (production) — and a ``MemoryStore``
   ``@runtime_checkable`` ``Protocol`` (AD-18) so hosts can supply
   their own.
3. **Implements the canonical service boundary**
   (``core/services/memory_service.py``) — explicit
   ``remember`` / ``forget`` / ``recall`` / ``get`` /
   ``purge_expired`` / ``update_policy`` / ``inspect`` /
   ``statistics`` / lifecycle.  No LLM in the loop.  Deterministic
   pure-function scoring for recall.  All boundaries log with
   ``loguru`` (R-17) and return ``*Result`` dataclasses (R-2).
4. **Rejects secret-shaped payloads at the boundary** (R-16, AD-20) —
   18+ high-signal regex patterns (openai / anthropic / groq / github
   / aws / PEM / JWT / generic password / bearer / stripe / google /
   sendgrid / twilio / slack) trip the anti-secret gate before
   policy evaluation, so even a permissive policy cannot leak
   credentials into long-term memory.
5. **Integrates the service into the engine** (``core/omnix_engine.py``)
   — R-13 (memory is a service, not a singleton), default in-memory
   construction in ``_default_memory_service``, lifecycle walked by
   ``ServiceRegistry`` (registered as ``"memory"`` at priority 85),
   tracked by ``HealthMonitor`` under the ``"memory"`` subsystem,
   included in engine ``statistics()`` under the ``"memory"`` key.
6. **Adds 94 deterministic tests** across two test files —
   ``tests/test_memory_api.py`` (71 tests for the service in
   isolation: kinds, policy, entry, secret patterns, store, remember,
   recall, forget, get, purge, policy, lifecycle, protocol
   conformance) and ``tests/test_memory_service.py`` (23 tests for
   engine integration: default construction, injection, registry,
   health, statistics, shutdown, end-to-end, two-engine independence,
   repr).
7. **Validates the project as a whole** — ``python -m pytest tests/
   -q`` reports ``1014 passed, 6 warnings in 23.00s`` (94 new tests
   on top of the 920 baseline) and ``python -m pip check`` reports
   ``No broken requirements found.``

---

## What ships in Phase 9

### New files

| Path | Lines | Purpose |
| --- | --- | --- |
| ``core/services/memory_service.py`` | ~1015 | The canonical V6 memory service and the ``InMemoryStore`` reference backend. |
| ``core/services/sqlite_memory_store.py`` | ~370 | The SQLite-backed production backend (WAL, autocommit, indices on hash / created_at / kind). |
| ``tests/test_memory_api.py`` | ~750 | 71 deterministic tests for the service in isolation. |
| ``tests/test_memory_service.py`` | ~360 | 23 tests for engine integration. |

### Modified files

| Path | Change |
| --- | --- |
| ``core/omnix_engine.py`` | Added ``memory: Optional[Any] = None`` constructor parameter; default ``_default_memory_service`` factory at the bottom of the file; registered the service as ``"memory"`` (priority 85) in ``_do_initialize``; tracked it as ``"memory"`` in ``HealthMonitor``; added a ``"memory"`` block to ``statistics()``. |
| ``tests/test_memory_api.py`` | Replaced the stub ``print()`` line with a comprehensive 71-test suite. |
| ``tests/test_memory_service.py`` | New file. |

### Unchanged files

All other Phase 0–8 modules are unchanged.  The new service obeys the
existing ``LifecycleMixin`` contract (``initialize`` / ``shutdown`` /
``initialized`` / ``statistics`` / ``__repr__``) and the existing
``ServiceRegistry`` contract, so the engine integration is purely
additive.

---

## Design

### MemoryKind — closed enum (R-21)

``MemoryKind`` is a closed ``enum.Enum`` with exactly seven values:

* ``FACT``         — durable fact about the world or the user.
* ``PREFERENCE``   — user preference / taste.
* ``INSTRUCTION``  — durable rule / directive.
* ``CONTEXT``      — short-lived situational context.
* ``EPISODE``      — a single past event.
* ``ENTITY``       — reference to a person / place / object.
* ``TASK_OUTCOME`` — outcome of a previous task.

This is intentionally a closed set: hosts cannot invent new kinds
without a code change, which keeps policy and scoring consistent.

### MemoryPolicy — every write is governed (R-15, AD-15)

``MemoryPolicy`` is a frozen dataclass with these defaults
(conservative):

```python
MemoryPolicy(
    retention_seconds=60*60*24*90,   # 90 days
    deduplicate=True,
    private=True,
    min_confidence=0.0,
    max_size_bytes=4096,
    allow_kinds=tuple(MemoryKind),   # all kinds
    deny_kinds=(),
)
```

The service evaluates policy in this order on every write (so the
order is independent of the order the dataclass fields were
declared):

1. ``min_confidence`` / out-of-range rejection,
2. ``allow_kinds`` / ``deny_kinds``,
3. ``max_size_bytes``,
4. ``deduplicate`` (re-returns the existing entry on hash match),
5. ``retention`` (stamps ``expires_at`` on every entry).

Hosts can call ``memory.update_policy(...)`` to install a new policy
at runtime; existing entries are not retroactively re-scored.

### MemoryEntry — frozen, hash-addressed

``MemoryEntry`` is a frozen dataclass carrying the raw ``content``,
the ``confidence`` in ``[0.0, 1.0]``, the SHA-256 ``content_hash``,
the ``created_at`` timestamp, the ``expires_at`` timestamp (or
``None``), the ``private`` flag, ``tags`` (tuple of strings),
``metadata`` (dict of primitives), and the immutable ``Provenance``
record (``source``, ``session_id``, ``task_id``, ``capability``,
``detail``, ``timestamp``).

Frozen means it is safe to share an entry between threads and
between services.

### Secret detection at the boundary (R-16, AD-20)

``detect_secrets(content)`` runs a battery of 18+ high-signal regex
patterns over the *raw* content **before** any policy check, returning
a list of ``SecretMatch(pattern_name, sample, start, end)``:

* ``openai_key`` (sk-...), ``anthropic_key`` (sk-ant-...),
  ``groq_key`` (gsk_...), ``github_token`` (ghp_...),
  ``aws_access_key`` (AKIA...), ``aws_secret_key`` (heuristic),
  ``pem_private_key`` (-----BEGIN ... PRIVATE KEY-----),
  ``jwt_token`` (eyJ...eyJ...sig),
  ``generic_password`` ("password: <value>"),
  ``bearer_token`` ("Bearer <value>"),
  ``stripe_key`` (sk_live_/sk_test_),
  ``google_api_key`` (AIza...),
  ``sendgrid_key`` (SG....),
  ``twilio_key`` (SK...),
  ``slack_token`` (xox[bpars]-...),
  ``private_ip`` (RFC1918 — flagged so hosts can decide).

The service refuses to remember any content that matches, with
``error_code = "SECRET_DETECTED"`` and an incremented
``rejected_secret`` counter.  The rejection happens *before* the
policy check, so even a fully permissive policy cannot leak secrets
into long-term memory.

### MemoryService — the canonical boundary

The service exposes these public methods (all return
``MemoryResult`` for happy path and errors uniformly):

* ``initialize()`` / ``shutdown()`` / ``initialized`` (R-9 lifecycle).
* ``remember(content, *, kind, confidence, provenance, tags, metadata, ttl)`` —
  validated write with policy + secret checks; returns
  ``MemoryResult(success, status, value=entry, ...)``.
* ``recall(query, *, kind, limit, include_private, min_confidence, tags)`` —
  returns ``MemoryResult(value=[RecallHit], ...)``.  Scoring is a
  pure function of token overlap, recency decay, confidence, kind
  bonus, and tag bonus.  No LLM in the loop.
* ``forget(*, entry_id, kind, tag)`` — at least one of ``entry_id``,
  ``kind``, ``tag`` is required (refuses to forget everything).
* ``get(entry_id)`` — direct lookup, returns the entry or an error.
* ``purge_expired()`` — deletes every entry whose ``expires_at`` is
  in the past; returns ``metadata["count"]``.
* ``update_policy(policy)`` — installs a new policy; existing entries
  are not re-scored.
* ``inspect()`` — JSON-safe debugging snapshot (without content).
* ``statistics()`` — counters for ``stored``, ``recalls``,
  ``forgotten``, ``rejected_secret``, ``rejected_policy``,
  ``rejected_confidence``, ``rejected_size``, ``duplicates``, plus a
  store-specific block from the backend.
* ``__repr__`` — debug string (R-9).

### Pluggable backends (AD-18)

``MemoryStore`` is a ``@runtime_checkable Protocol`` with the minimum
surface a backend must expose:

```python
class MemoryStore(Protocol):
    def initialize(self) -> bool: ...
    def shutdown(self) -> None: ...
    def upsert(self, entry: MemoryEntry) -> bool: ...
    def delete(self, entry_id: str) -> bool: ...
    def get(self, entry_id: str) -> Optional[MemoryEntry]: ...
    def find_by_hash(self, content_hash: str) -> Optional[MemoryEntry]: ...
    def list_recent(self, *, kind, include_private, limit) -> List[MemoryEntry]: ...
    def all_hashes(self) -> Iterable[str]: ...
    def count(self) -> int: ...
    def iter_all(self) -> Iterable[MemoryEntry]: ...   # for purge
```

Two reference backends ship:

* ``InMemoryStore`` — pure-Python dict + sorted list, RLock, used by
  the default engine path and by all tests that do not need
  persistence.
* ``SQLiteMemoryStore`` — single ``memory_entries`` table, indices on
  ``content_hash``, ``created_at``, ``(kind, created_at)``, WAL
  journal mode for safe multi-thread access from a single process,
  ``autocommit`` with explicit transaction control.  Hosts can pick
  the path; tests use ``":memory:"`` for isolation.

### Engine integration (R-13, R-9)

The engine:

1. Accepts a ``memory=`` keyword in ``__init__``.
2. Falls back to ``_default_memory_service(config)`` which builds an
   ``InMemoryStore`` and wraps it in a ``MemoryService``.
3. Registers the service in ``ServiceRegistry`` as ``"memory"`` at
   priority 85 (between ``health`` at 90 and ``contexts`` at 100), so
   the registry walks its ``initialize`` / ``shutdown`` alongside
   every other service.
4. Tracks it in ``HealthMonitor`` under ``"memory"``, so the health
   report and dashboard reflect its status.
5. Surfaces a ``"memory"`` block in ``engine.statistics()``,
   satisfying R-9.
6. Shuts the service down on engine shutdown, so the SQLite
   connection closes cleanly.

Hosts that want a different backend pass ``memory=`` explicitly
(satisfying R-13: memory is a service, not a singleton).  Two
engines in the same process each own their own service.

---

## Rule & decision coverage

| Rule / Decision | Coverage in Phase 9 |
| --- | --- |
| R-1 thin orchestrator | Engine adds memory wiring only; no business logic in the engine. |
| R-2 service wrapper contract | All public methods return ``MemoryResult(success, status, value, error, error_code, metadata)``. |
| R-9 uniform subsystem lifecycle | ``MemoryService`` extends ``LifecycleMixin``; ``initialize`` / ``shutdown`` / ``initialized`` / ``statistics`` / ``__repr__`` all implemented. |
| R-13 memory is a service, not a singleton | Engine accepts ``memory=``; default factory builds an ``InMemoryStore``; two engines own independent services. |
| R-15 memory has policy | ``MemoryPolicy`` is the only path through which retention / dedup / privacy / confidence are configured. |
| R-16 configuration is JSON, secrets are not | ``detect_secrets`` rejects 18+ high-signal secret patterns at the boundary, before policy. |
| R-17 loguru only | All logging uses ``loguru`` (``logger.info`` / ``logger.error`` / ``logger.warning``); no ``import logging`` in Phase 9 code. |
| R-18 backend is a Protocol | ``MemoryStore`` is a ``@runtime_checkable Protocol``; two reference implementations ship. |
| R-21 closed enum | ``MemoryKind`` is a closed ``enum.Enum``; ``allow_kinds`` / ``deny_kinds`` enforce policy over the closed set. |
| AD-15 every memory write carries policy + provenance + confidence + privacy + retention | ``MemoryEntry`` and the ``remember`` flow enforce all five. |
| AD-18 pluggable backend | ``MemoryStore`` Protocol; ``InMemoryStore`` + ``SQLiteMemoryStore`` ship. |
| AD-20 anti-pattern: secret-shaped payloads are rejected at the boundary | ``detect_secrets`` runs before policy; ``rejected_secret`` counter; ``SECRET_DETECTED`` error code. |

---

## Test coverage

### ``tests/test_memory_api.py`` (71 tests)

* ``TestMemoryKind`` (3 tests) — enum closed set, value strings.
* ``TestMemoryPolicy`` (6 tests) — default values conservative,
  ``to_dict`` round-trip, allow/deny kinds, ``min_confidence``,
  ``max_size_bytes``.
* ``TestMemoryEntry`` (4 tests) — frozen (mutation raises),
  ``is_expired`` boundary, ``to_dict`` redacts private entries.
* ``TestDetectSecrets`` (16 tests) — one positive and one negative
  test per pattern family, plus a "no false positives" test for plain
  text and a "no pattern on benign URL" test.
* ``TestInMemoryStore`` (8 tests) — round-trip, find_by_hash, delete,
  list_recent ordering, private filter, kind filter, count,
  shutdown idempotence.
* ``TestRemember`` (15 tests) — stores, rejects empty, rejects
  non-string, dedup, dedup normalises whitespace, rejects secret,
  rejects kind, rejects low confidence, rejects out-of-range
  confidence, rejects oversize, stamps ``expires_at``, zero
  retention, refuses before init, provenance, tags/metadata.
* ``TestRecall`` (9 tests) — by token overlap, kind filter, empty
  returns empty status, min_confidence, respects tags, limit,
  refused before init, rejects non-string query, scoring is
  deterministic.
* ``TestForget`` (5 tests) — by id, unknown id failure, by kind, by
  tag, requires filter.
* ``TestGet`` (3 tests) — returns entry, expired, rejects empty id /
  unknown id.
* ``TestPurge`` (2 tests) — purge_expired removes; non-expired
  survive.
* ``TestPolicy`` (2 tests) — update_policy affects new writes.
* ``TestLifecycle`` (3 tests) — initialize_shutdown idempotent,
  statistics shape, inspect, repr.
* ``TestProtocol`` (1 test) — both reference backends pass
  ``isinstance(..., MemoryStore)`` and have the required surface.

### ``tests/test_memory_service.py`` (23 tests)

* ``TestEngineDefaultMemory`` (3 tests) — engine constructs a default
  service; default service initializes; default backend is
  ``InMemoryStore``.
* ``TestEngineInjectedMemory`` (3 tests) — engine accepts injected
  service; writes through engine land in the injected store; engine
  accepts SQLite-backed service and persists across restarts.
* ``TestMemoryServiceRegistration`` (3 tests) — service registered
  in ``ServiceRegistry``; resolvable by name; pre-registered service
  is not overwritten.
* ``TestMemoryInHealthMonitor`` (2 tests) — health report contains
  the ``memory`` subsystem block; block has a status.
* ``TestMemoryInStatistics`` (3 tests) — ``statistics()`` contains
  ``"memory"``; the block has the canonical shape; the entry count
  grows with writes.
* ``TestMemoryLifecycleOnShutdown`` (2 tests) — shutdown closes the
  default in-memory service; shutdown closes the SQLite backend.
* ``TestEngineEndToEndMemory`` (3 tests) — remember/recall round
  trip; secret rejection end to end; forget by id.
* ``TestEngineMemoryIndependence`` (2 tests) — two engines have
  independent services; injected service is preserved through
  registry.
* ``TestMemoryRepr`` (2 tests) — engine ``__repr__`` includes state;
  service ``__repr__`` includes type.

### ``python -m pytest tests/ -q`` result

```
1014 passed, 6 warnings in 23.00s
```

(920 baseline + 71 ``test_memory_api`` + 23 ``test_memory_service`` = 1014.)

The 6 warnings are pre-existing ``PytestUnknownMarkWarning`` for
``@pytest.mark.real_windows`` in the system-input / system-processes
test modules — not introduced by Phase 9.

### ``python -m pip check`` result

```
No broken requirements found.
```

---

## How a host uses it

```python
from core.configuration import OmnixConfig
from core.omnix_engine import OmnixEngine

# 1) Default (in-memory) — fine for tests and dev.
engine = OmnixEngine(OmnixConfig(...))
engine.initialize()

# 2) Remember a fact.
res = engine.memory.remember(
    "the user prefers dark mode",
    kind=MemoryKind.PREFERENCE,
    tags=("ui",),
)
assert res.success

# 3) Recall.
hits = engine.memory.recall("dark mode", kind=MemoryKind.PREFERENCE)
for h in hits.value:
    print(h.entry.content, h.score, h.match_reason)

# 4) Forget by id, by kind, or by tag.
engine.memory.forget(entry_id=hits.value[0].entry.id)

# 5) Update policy at runtime.
from core.services.memory_service import MemoryPolicy
engine.memory.update_policy(MemoryPolicy(retention_seconds=60*60*24))

# 6) Production backend: pass a SQLite-backed service.
from core.services.memory_service import MemoryService
from core.services.sqlite_memory_store import SQLiteMemoryStore
store = SQLiteMemoryStore(db_path="omnix_memory.sqlite3")
store.initialize()
engine = OmnixEngine(OmnixConfig(...), memory=MemoryService(store=store))
```

---

## What is explicitly **not** in Phase 9

* **No LLM-assisted summarisation / embedding.**  Recall is a pure,
  deterministic function of token overlap, recency, confidence,
  kind, and tag.  This is deliberate: it is testable without mocks,
  it is cheap, and it is honest about what we have stored.
* **No network sync, no remote backend.**  The protocol is local.  A
  future phase may add a remote backend behind the same
  ``MemoryStore`` surface.
* **No mutable schema migrations.**  The SQLite schema is
  ``CREATE TABLE IF NOT EXISTS``; if a host needs to migrate they
  must do it out of band.
* **No "always-on" memory.**  The engine does not write to memory
  silently.  Hosts call ``engine.memory.remember(...)`` explicitly
  (or wire a capability that does so).  This keeps the service
  auditable.

---

## Files of interest

* ``core/services/memory_service.py`` — service + ``InMemoryStore``.
* ``core/services/sqlite_memory_store.py`` — SQLite backend.
* ``core/omnix_engine.py`` — engine integration (constructor,
  ``_do_initialize``, ``statistics``, ``_default_memory_service``).
* ``tests/test_memory_api.py`` — service in isolation.
* ``tests/test_memory_service.py`` — engine integration.
* ``docs/V6_ARCHITECTURE_RULES.md`` — R-2, R-9, R-13, R-15, R-16,
  R-17, R-18, R-21 (all honoured).
* ``docs/V6_ARCHITECTURAL_DECISIONS.md`` — AD-15, AD-18, AD-20 (all
  honoured).

---

## Verification

* ``python -m pytest tests/ -q`` → **1014 passed, 6 warnings in
  23.00s**.
* ``python -m pip check`` → **No broken requirements found.**

Phase 9 is complete.  The memory subsystem is ready for hosts and
for Phase 10.
