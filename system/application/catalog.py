"""
Omnix V6 — Application catalog (cache + miss path).

The catalog is the *single in-memory* representation of what is
installed on the host.  The discovery subsystem produces a stream of
:class:`ApplicationRecord` objects; the catalog merges them into a
normalized, indexed, deduplicated store.

Indexing strategy
-----------------

Each record contributes up to three keys:

1. ``normalized_name`` (primary) — the main lookup key.
2. ``aliases`` — alternative normalized names.
3. The executable **stem** (``"chrome"`` for ``"chrome.exe"``) — so
   ``"open chrome"`` matches even if no record has a display name
   "chrome".

The catalog is a plain :class:`dict` of primary key → :class:`ApplicationRecord`.
A second dict maps each alias / executable-stem → primary key.  Both
are rebuilt on every :meth:`refresh`.

Lifecycle
---------

The catalog is a :class:`LifecycleMixin` so the engine can register
it with the service registry and call ``initialize()`` at boot, at
which point :meth:`refresh` is called.  A subsequent
:meth:`ApplicationCatalog.lookup` is O(1) (one dict lookup + one
alias lookup) so it is safe to call inside hot paths.

Cache miss
----------

For a name that has no entry, the catalog may attempt a *targeted*
re-scan: just the registry, just the app-paths, just the path
directories.  This avoids re-doing a full discovery for a single
miss.  The targeted scan is bounded and falls back to ``None`` on
failure.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

from loguru import logger

from core.lifecycle import LifecycleMixin, LifecycleState

from .discovery import (
    ApplicationSource,
    PathSource,
    RegistryUninstallSource,
    AppPathsSource,
    default_sources,
    normalize_name,
)
from .models import ApplicationRecord, Resolution


@dataclass
class CatalogStats:
    """Snapshot of catalog state for diagnostics and the readiness report."""

    record_count: int = 0
    source_counts: Dict[str, int] = field(default_factory=dict)
    last_refresh_s: float = 0.0
    last_refresh_ms: float = 0.0
    miss_count: int = 0
    miss_targeted_hits: int = 0


class ApplicationCatalog(LifecycleMixin):
    """In-memory cache of discovered applications.

    Owns the merged record dict; the application service / resolver
    consult it via :meth:`lookup` and :meth:`is_installed`.  The
    catalog is **read-only** from the perspective of consumers;
    refresh is initiated by the engine at boot.
    """

    def __init__(
        self,
        *,
        sources: Optional[List[ApplicationSource]] = None,
    ) -> None:
        self._lifecycle_state: LifecycleState = LifecycleState.CREATED
        self._initialization_error: Optional[str] = None
        self._sources: List[ApplicationSource] = (
            list(sources) if sources is not None else default_sources()
        )
        # primary key -> record (highest confidence wins)
        self._records: Dict[str, ApplicationRecord] = {}
        # secondary key (alias or exe-stem) -> primary key
        self._aliases: Dict[str, str] = {}
        # cache miss tracking
        self._stats = CatalogStats()

    # ---------------------------------------------------------- lifecycle
    def _do_initialize(self) -> bool:
        """Run a full discovery refresh at boot."""
        try:
            self.refresh()
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error(f"ApplicationCatalog refresh failed: {exc!r}")
            return False

    def _do_shutdown(self) -> None:
        return None

    def statistics(self) -> Dict[str, Any]:
        return {
            "type": "ApplicationCatalog",
            "lifecycle": self._lifecycle_state.value,
            "record_count": self._stats.record_count,
            "source_counts": dict(self._stats.source_counts),
            "last_refresh_ms": self._stats.last_refresh_ms,
            "miss_count": self._stats.miss_count,
            "miss_targeted_hits": self._stats.miss_targeted_hits,
        }

    # ---------------------------------------------------------- refresh
    def refresh(self) -> None:
        """Re-run all sources and rebuild the index.

        Sources are scanned in order.  For each record the catalog
        keeps the **highest-confidence** version and merges
        metadata.  The full index is rebuilt from scratch so a
        record that disappears (e.g. an uninstall) is properly
        forgotten.
        """
        started = time.time()
        new_records: Dict[str, ApplicationRecord] = {}
        source_counts: Dict[str, int] = {}
        for source in self._sources:
            records = source.safe_scan()
            source_counts[source.name] = len(records)
            for rec in records:
                self._merge(new_records, rec)
        # Build alias index
        new_aliases: Dict[str, str] = {}
        for key, rec in new_records.items():
            new_aliases[key] = key
            for alias in rec.aliases:
                a = normalize_name(alias)
                if a and a not in new_aliases:
                    new_aliases[a] = key
            # executable stem alias
            if rec.executable.lower().endswith(".exe"):
                stem = normalize_name(rec.executable[:-4])
                if stem and stem not in new_aliases:
                    new_aliases[stem] = key
        self._records = new_records
        self._aliases = new_aliases
        self._stats.record_count = len(new_records)
        self._stats.source_counts = source_counts
        self._stats.last_refresh_s = time.time()
        self._stats.last_refresh_ms = (time.time() - started) * 1000.0
        logger.info(
            f"ApplicationCatalog refresh: "
            f"{self._stats.record_count} records in "
            f"{self._stats.last_refresh_ms:.1f}ms"
        )

    def _merge(
        self,
        records: Dict[str, ApplicationRecord],
        rec: ApplicationRecord,
    ) -> None:
        key = rec.normalized_name
        if not key:
            return
        existing = records.get(key)
        if existing is None:
            records[key] = rec
            return
        # Higher confidence wins; equal-confidence newer source wins.
        if rec.confidence > existing.confidence:
            records[key] = rec
        elif (
            rec.confidence == existing.confidence
            and rec.executable_path
            and not existing.executable_path
        ):
            records[key] = rec
        # Always merge the running-pid metadata if present.
        if rec.metadata.get("running_pid_count") and existing.metadata:
            existing_conf = existing
            merged_meta = dict(existing.metadata)
            merged_meta["running_pid_count"] = rec.metadata["running_pid_count"]
            # Replace with a copy that has the merged metadata.
            records[key] = ApplicationRecord(
                display_name=existing_conf.display_name,
                normalized_name=existing_conf.normalized_name,
                executable=existing_conf.executable,
                executable_path=existing_conf.executable_path or rec.executable_path,
                launch_command=existing_conf.launch_command,
                source=existing_conf.source,
                installed=existing_conf.installed or rec.installed,
                aliases=existing_conf.aliases,
                confidence=max(existing_conf.confidence, rec.confidence),
                metadata=merged_meta,
            )

    # ---------------------------------------------------------- lookup
    def lookup(self, name: str) -> Optional[ApplicationRecord]:
        """Resolve a (normalized) name to a record, or ``None``.

        Performs an O(1) primary-key lookup; on a miss, runs a
        *targeted* scan of the registry + app-paths + PATH and
        updates the index if it finds the name.
        """
        if not name:
            return None
        n = normalize_name(name)
        if not n:
            return None
        primary = self._aliases.get(n)
        if primary:
            rec = self._records.get(primary)
            if rec is not None:
                return rec
        # Targeted re-scan for the miss
        self._stats.miss_count += 1
        rec = self._targeted_scan(n)
        if rec is not None:
            self._stats.miss_targeted_hits += 1
            self._merge(self._records, rec)
            # refresh the alias for this lookup
            self._aliases[n] = rec.normalized_name
            if rec.executable.lower().endswith(".exe"):
                self._aliases.setdefault(
                    normalize_name(rec.executable[:-4]),
                    rec.normalized_name,
                )
        return rec

    def _targeted_scan(self, normalized: str) -> Optional[ApplicationRecord]:
        """Run a quick re-scan of registry + app-paths + PATH for one
        name.  Bounded to a fraction of a second."""
        # Registry
        try:
            for source in (RegistryUninstallSource(), AppPathsSource()):
                for rec in source.safe_scan():
                    if rec.matches(normalized):
                        return rec
        except Exception:  # noqa: BLE001
            pass
        # PATH (filename-only match)
        try:
            ps = PathSource()
            for rec in ps.safe_scan():
                if rec.matches(normalized):
                    return rec
        except Exception:  # noqa: BLE001
            pass
        return None

    # ---------------------------------------------------------- enumeration
    def all_records(self) -> List[ApplicationRecord]:
        return list(self._records.values())

    def __len__(self) -> int:
        return len(self._records)

    def __contains__(self, name: str) -> bool:
        return self.lookup(name) is not None

    def is_installed(self, name: str) -> bool:
        return self.lookup(name) is not None
