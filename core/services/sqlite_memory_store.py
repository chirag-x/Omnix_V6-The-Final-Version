"""
Omnix V6 — SQLite-backed memory store (Phase 9).

This is the *production* backend for :class:`MemoryService`.  It is
intentionally minimal:

* a single ``memory_entries`` table,
* indices on ``content_hash`` (for dedup) and ``created_at`` (for
  recent-first listing),
* a thread-safe :class:`threading.RLock` around all access,
* zero external dependencies beyond the Python standard library.

The store is *not* a singleton (R-13).  The engine owns one and
hands it to the service.  The store is *not* a place to put
business logic (R-15) — the service enforces policy, the store
just persists.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from loguru import logger as _loguru

from .memory_service import (
    InMemoryStore,  # for _read_entry / _write_entry serialization parity
    MemoryEntry,
    MemoryKind,
    MemoryStore,
    Provenance,
)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_entries (
    id            TEXT PRIMARY KEY,
    kind          TEXT NOT NULL,
    content       TEXT NOT NULL,
    confidence    REAL NOT NULL,
    content_hash  TEXT NOT NULL,
    created_at    REAL NOT NULL,
    expires_at    REAL,
    private       INTEGER NOT NULL DEFAULT 1,
    tags          TEXT NOT NULL DEFAULT '[]',
    metadata      TEXT NOT NULL DEFAULT '{}',
    provenance    TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_memory_entries_hash
    ON memory_entries(content_hash);

CREATE INDEX IF NOT EXISTS idx_memory_entries_created
    ON memory_entries(created_at DESC);

CREATE INDEX IF NOT EXISTS idx_memory_entries_kind
    ON memory_entries(kind, created_at DESC);
"""


class SQLiteMemoryStore(MemoryStore):
    """SQLite-backed memory store.

    Parameters
    ----------
    db_path:
        Filesystem path of the SQLite database.  Use ``":memory:"`` for
        a transient database (tests).
    journal_mode:
        SQLite journal mode (``"WAL"`` by default, which is safe for
        multi-thread access from a single process).
    """

    def __init__(
        self,
        db_path: str = "omnix_memory.sqlite3",
        *,
        journal_mode: str = "WAL",
    ) -> None:
        self._db_path = db_path
        self._journal_mode = journal_mode
        self._lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = None
        self._initialized = False

        # counters for diagnostics
        self._writes = 0
        self._reads = 0
        self._deletes = 0

    # ===================================================== lifecycle
    def initialize(self) -> bool:
        with self._lock:
            if self._initialized:
                return True
            try:
                if self._db_path != ":memory:":
                    parent = Path(self._db_path).parent
                    if parent and not parent.exists():
                        parent.mkdir(parents=True, exist_ok=True)
                conn = sqlite3.connect(
                    self._db_path,
                    check_same_thread=False,
                    detect_types=sqlite3.PARSE_DECLTYPES,
                    isolation_level=None,  # autocommit; we manage transactions explicitly
                )
                conn.row_factory = sqlite3.Row
                conn.execute(f"PRAGMA journal_mode={self._journal_mode}")
                conn.execute("PRAGMA synchronous=NORMAL")
                conn.execute("PRAGMA foreign_keys=ON")
                conn.executescript(_SCHEMA)
                self._conn = conn
                self._initialized = True
            except Exception as exc:  # noqa: BLE001
                _loguru.error(f"SQLiteMemoryStore.initialize failed: {exc!r}")
                self._initialized = False
                return False
        return True

    def shutdown(self) -> None:
        with self._lock:
            conn, self._conn = self._conn, None
            self._initialized = False
            if conn is not None:
                try:
                    conn.close()
                except Exception as exc:  # noqa: BLE001
                    _loguru.warning(f"SQLiteMemoryStore.close raised: {exc!r}")

    # ============================================================ CRUD
    def upsert(self, entry: MemoryEntry) -> bool:
        conn = self._require_conn()
        with self._lock:
            try:
                conn.execute(
                    """
                    INSERT INTO memory_entries
                        (id, kind, content, confidence, content_hash,
                         created_at, expires_at, private, tags, metadata,
                         provenance)
                    VALUES
                        (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        kind=excluded.kind,
                        content=excluded.content,
                        confidence=excluded.confidence,
                        content_hash=excluded.content_hash,
                        created_at=excluded.created_at,
                        expires_at=excluded.expires_at,
                        private=excluded.private,
                        tags=excluded.tags,
                        metadata=excluded.metadata,
                        provenance=excluded.provenance
                    """,
                    _entry_to_row(entry),
                )
                self._writes += 1
                return True
            except Exception as exc:  # noqa: BLE001
                _loguru.error(f"SQLiteMemoryStore.upsert failed: {exc!r}")
                return False

    def delete(self, entry_id: str) -> bool:
        conn = self._require_conn()
        with self._lock:
            try:
                cur = conn.execute(
                    "DELETE FROM memory_entries WHERE id = ?",
                    (entry_id,),
                )
                self._deletes += 1
                return cur.rowcount > 0
            except Exception as exc:  # noqa: BLE001
                _loguru.error(f"SQLiteMemoryStore.delete failed: {exc!r}")
                return False

    def get(self, entry_id: str) -> Optional[MemoryEntry]:
        conn = self._require_conn()
        with self._lock:
            try:
                cur = conn.execute(
                    "SELECT * FROM memory_entries WHERE id = ?",
                    (entry_id,),
                )
                self._reads += 1
                row = cur.fetchone()
                if row is None:
                    return None
                return _row_to_entry(row)
            except Exception as exc:  # noqa: BLE001
                _loguru.error(f"SQLiteMemoryStore.get failed: {exc!r}")
                return None

    def find_by_hash(self, content_hash: str) -> Optional[MemoryEntry]:
        conn = self._require_conn()
        with self._lock:
            try:
                cur = conn.execute(
                    "SELECT * FROM memory_entries WHERE content_hash = ? LIMIT 1",
                    (content_hash,),
                )
                self._reads += 1
                row = cur.fetchone()
                if row is None:
                    return None
                return _row_to_entry(row)
            except Exception as exc:  # noqa: BLE001
                _loguru.error(f"SQLiteMemoryStore.find_by_hash failed: {exc!r}")
                return None

    def list_recent(
        self,
        *,
        kind: Optional[MemoryKind] = None,
        include_private: bool = False,
        limit: int = 50,
    ) -> List[MemoryEntry]:
        conn = self._require_conn()
        where: List[str] = []
        params: List[Any] = []
        if kind is not None:
            where.append("kind = ?")
            params.append(kind.value)
        if not include_private:
            where.append("private = 0")
        where.append("(expires_at IS NULL OR expires_at > ?)")
        params.append(time.time())

        sql = "SELECT * FROM memory_entries"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(int(limit))

        with self._lock:
            try:
                cur = conn.execute(sql, params)
                self._reads += 1
                rows = cur.fetchall()
            except Exception as exc:  # noqa: BLE001
                _loguru.error(f"SQLiteMemoryStore.list_recent failed: {exc!r}")
                return []
        return [_row_to_entry(r) for r in rows]

    def all_hashes(self) -> Iterable[str]:
        conn = self._require_conn()
        with self._lock:
            try:
                cur = conn.execute("SELECT content_hash FROM memory_entries")
                self._reads += 1
                rows = cur.fetchall()
            except Exception as exc:  # noqa: BLE001
                _loguru.error(f"SQLiteMemoryStore.all_hashes failed: {exc!r}")
                return []
        return [r["content_hash"] for r in rows]

    def iter_all(self) -> Iterable[MemoryEntry]:
        conn = self._require_conn()
        with self._lock:
            try:
                cur = conn.execute("SELECT * FROM memory_entries")
                self._reads += 1
                rows = cur.fetchall()
            except Exception as exc:  # noqa: BLE001
                _loguru.error(f"SQLiteMemoryStore.iter_all failed: {exc!r}")
                return []
        return [_row_to_entry(r) for r in rows]

    def count(self) -> int:
        conn = self._require_conn()
        with self._lock:
            try:
                cur = conn.execute("SELECT COUNT(*) AS c FROM memory_entries")
                self._reads += 1
                row = cur.fetchone()
                return int(row["c"]) if row else 0
            except Exception as exc:  # noqa: BLE001
                _loguru.error(f"SQLiteMemoryStore.count failed: {exc!r}")
                return 0

    # ===================================================== diagnostics
    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "type": "SQLiteMemoryStore",
                "db_path": self._db_path,
                "journal_mode": self._journal_mode,
                "initialized": self._initialized,
                "writes": self._writes,
                "reads": self._reads,
                "deletes": self._deletes,
                "entries": self.count(),
            }

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"SQLiteMemoryStore(path={self._db_path!r}, entries={self.count()})"

    # ===================================================== helpers
    def _require_conn(self) -> sqlite3.Connection:
        if not self._initialized or self._conn is None:
            raise RuntimeError("SQLiteMemoryStore is not initialized")
        return self._conn


# ===========================================================================
# Module-level helpers (row <-> entry)
# ===========================================================================

def _entry_to_row(entry: MemoryEntry) -> Tuple[Any, ...]:
    return (
        entry.id,
        entry.kind.value,
        entry.content,
        float(entry.confidence),
        entry.content_hash,
        float(entry.created_at),
        entry.expires_at,
        1 if entry.private else 0,
        json.dumps(list(entry.tags)),
        json.dumps(dict(entry.metadata)),
        json.dumps(entry.provenance.to_dict()),
    )


def _row_to_entry(row: sqlite3.Row) -> MemoryEntry:
    kind = MemoryKind(row["kind"])
    try:
        tags = tuple(json.loads(row["tags"] or "[]"))
    except Exception:  # noqa: BLE001
        tags = ()
    try:
        metadata = json.loads(row["metadata"] or "{}")
    except Exception:  # noqa: BLE001
        metadata = {}
    try:
        prov_data = json.loads(row["provenance"] or "{}")
    except Exception:  # noqa: BLE001
        prov_data = {}
    provenance = Provenance(
        source=str(prov_data.get("source", "engine")),
        session_id=str(prov_data.get("session_id", "")),
        task_id=str(prov_data.get("task_id", "")),
        capability=str(prov_data.get("capability", "")),
        detail=str(prov_data.get("detail", "")),
        timestamp=float(prov_data.get("timestamp", row["created_at"])),
    )
    return MemoryEntry(
        id=str(row["id"]),
        kind=kind,
        content=str(row["content"]),
        confidence=float(row["confidence"]),
        content_hash=str(row["content_hash"]),
        created_at=float(row["created_at"]),
        expires_at=row["expires_at"],
        private=bool(row["private"]),
        tags=tags,
        metadata=metadata,
        provenance=provenance,
    )


__all__ = ["SQLiteMemoryStore"]
