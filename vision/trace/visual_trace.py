"""
Omnix V6 — System 3 (Vision) visual trace.

An append-only JSONL writer that records every vision-grounding
decision for forensic analysis.  The trace captures the full
grounding context — query, strategy, candidates, selected
element, confidence, latency, status, and screenshot path —
so a developer or the Brain can answer "why did Vision pick
this element?" after the fact.

Design
------
* Off by default.  The new public API only writes to the
  trace when the env var ``OMNIX_VISUAL_TRACE=1`` is set, or
  the in-process toggle is flipped via
  :func:`set_visual_trace_enabled`.  Default-off keeps test
  runs lean and avoids any I/O on a system without a writable
  log directory.
* Append-only.  Records are written one per line as JSON
  objects.  Concurrent writers are serialised through a
  process-local lock.
* Size-based rotation.  When the file exceeds
  ``rotation_bytes`` the writer renames the current file with
  a ``.1`` suffix and starts a new one.  Keeps long-running
  test sessions from filling the disk.
* Pure-data records.  The writer takes a :class:`VisualTraceRecord`
  dataclass and turns it into a JSON-serialisable dict; it
  never reaches into a GroundedElement or its provenance.
  Call sites stay explicit about what they are tracing.
* Legacy-safe.  The legacy :class:`core.services.vision_service.VisionService`
  path does **not** write trace records; only the new public
  API in :mod:`vision.api` does.  This keeps the 12 currently
  passing Phase 16 / Part 3 tests fully unchanged.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


# Default location of the trace file.  Resolved relative to
# the working directory at first write; the env var
# ``OMNIX_VISUAL_TRACE_PATH`` overrides it.
DEFAULT_TRACE_PATH = "logs/visual_trace.jsonl"
_ROTATION_BYTES_DEFAULT = 8 * 1024 * 1024  # 8 MiB
_ENV_FLAG = "OMNIX_VISUAL_TRACE"
_ENV_PATH = "OMNIX_VISUAL_TRACE_PATH"

_lock = threading.Lock()
_enabled: bool = False
_initialised: bool = False
_current_path: Optional[str] = None


@dataclass(frozen=True)
class VisualTraceRecord:
    """One row in the visual trace JSONL file.

    Every field is documented and defaulted so call sites can
    build a record from partial data in tests.  The record is
    frozen (R-10) so it cannot be mutated after the call site
    commits it to the trace.
    """

    record_id: str
    timestamp: float
    event: str  # "observe" | "find" | "locate" | "wait_for" | "verify" | "recover" | ...
    query: str
    strategy: Optional[str]
    candidates: int
    selected_id: Optional[str]
    confidence: float
    latency_ms: float
    status: str
    screenshot_path: Optional[str] = None
    monitor_id: Optional[str] = None
    extras: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "record_id": self.record_id,
            "timestamp": self.timestamp,
            "event": self.event,
            "query": self.query,
            "strategy": self.strategy,
            "candidates": self.candidates,
            "selected_id": self.selected_id,
            "confidence": float(self.confidence),
            "latency_ms": float(self.latency_ms),
            "status": self.status,
            "screenshot_path": self.screenshot_path,
            "monitor_id": self.monitor_id,
        }
        if self.extras:
            out["extras"] = dict(self.extras)
        return out


class VisualTrace:
    """The singleton visual-trace writer.

    The class is process-local.  Use :func:`get_visual_trace`
    to access the instance.  The writer is intentionally
    thread-safe so the multi-threaded agent loop can call
    :meth:`append` from any worker.
    """

    def __init__(
        self,
        path: Optional[str] = None,
        *,
        rotation_bytes: int = _ROTATION_BYTES_DEFAULT,
    ) -> None:
        self._path = path or os.environ.get(_ENV_PATH) or DEFAULT_TRACE_PATH
        self._rotation_bytes = max(int(rotation_bytes), 1024)
        self._lock = threading.Lock()
        self._open = False

    @property
    def path(self) -> str:
        return self._path

    def _ensure_open(self) -> None:
        if self._open:
            return
        directory = os.path.dirname(self._path)
        if directory and not os.path.isdir(directory):
            try:
                os.makedirs(directory, exist_ok=True)
            except OSError:
                # Fall back to a temp file in cwd.  The trace
                # is best-effort; never crash a grounding call
                # because the log directory is unwritable.
                self._path = os.path.basename(self._path)
        self._open = True

    def _maybe_rotate(self) -> None:
        try:
            if os.path.isfile(self._path) and os.path.getsize(self._path) >= self._rotation_bytes:
                backup = f"{self._path}.1"
                try:
                    if os.path.isfile(backup):
                        os.remove(backup)
                    os.rename(self._path, backup)
                except OSError:
                    # Rotation is best-effort.  If it fails we
                    # continue appending to the existing file.
                    pass
        except OSError:
            pass

    def append(self, record: VisualTraceRecord) -> bool:
        """Append ``record`` to the trace.  Returns ``True`` on
        success, ``False`` if the write failed (the trace is
        best-effort and never raises into the call site).
        """
        with self._lock:
            try:
                self._ensure_open()
                self._maybe_rotate()
                line = json.dumps(record.to_dict(), ensure_ascii=False, sort_keys=True)
                with open(self._path, "a", encoding="utf-8") as fp:
                    fp.write(line)
                    fp.write("\n")
                return True
            except Exception:  # noqa: BLE001
                return False

    def reset(self) -> None:
        """Truncate the trace file.  Test helper."""
        with self._lock:
            try:
                self._ensure_open()
                with open(self._path, "w", encoding="utf-8") as fp:
                    fp.write("")
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _env_flag_enabled() -> bool:
    raw = os.environ.get(_ENV_FLAG, "")
    return raw.strip().lower() in ("1", "true", "yes", "on")


def is_visual_trace_enabled() -> bool:
    """Return ``True`` when the visual trace is enabled.

    The flag is resolved lazily from the environment the
    first time this function is called; subsequent
    :func:`set_visual_trace_enabled` calls override it.
    """
    global _enabled, _initialised
    if not _initialised:
        _enabled = _env_flag_enabled()
        _initialised = True
    return _enabled


def set_visual_trace_enabled(enabled: bool) -> None:
    """Override the visual-trace flag for the current process.

    The override is process-local; it does not modify the
    environment.  Useful in tests that need to enable or
    disable the trace without touching the env.
    """
    global _enabled, _initialised
    _enabled = bool(enabled)
    _initialised = True


def _trace_instance() -> VisualTrace:
    global _current_path
    path = os.environ.get(_ENV_PATH) or DEFAULT_TRACE_PATH
    if _current_path is None or _current_path != path:
        _current_path = path
    return VisualTrace(path=_current_path)


def get_visual_trace() -> VisualTrace:
    """Return the process-local :class:`VisualTrace` instance.

    The instance is created lazily on first access so the
    trace file is never opened until the first record is
    actually written.
    """
    return _trace_instance()


def trace_event(
    *,
    event: str,
    query: str,
    strategy: Optional[str] = None,
    candidates: int = 0,
    selected_id: Optional[str] = None,
    confidence: float = 0.0,
    latency_ms: float = 0.0,
    status: str = "OK",
    screenshot_path: Optional[str] = None,
    monitor_id: Optional[str] = None,
    extras: Optional[Dict[str, Any]] = None,
) -> Optional[VisualTraceRecord]:
    """Convenience wrapper around :meth:`VisualTrace.append`.

    Builds a :class:`VisualTraceRecord`, writes it to the
    trace, and returns the record.  Returns ``None`` when the
    trace is disabled — the call site can then use the
    return value as a "did the trace accept the event?" flag.
    """
    if not is_visual_trace_enabled():
        return None
    record = VisualTraceRecord(
        record_id=uuid.uuid4().hex,
        timestamp=time.time(),
        event=event,
        query=query or "",
        strategy=strategy,
        candidates=int(candidates),
        selected_id=selected_id,
        confidence=float(confidence),
        latency_ms=float(latency_ms),
        status=str(status),
        screenshot_path=screenshot_path,
        monitor_id=monitor_id,
        extras=dict(extras) if extras else {},
    )
    get_visual_trace().append(record)
    return record


__all__ = [
    "VisualTraceRecord",
    "VisualTrace",
    "is_visual_trace_enabled",
    "set_visual_trace_enabled",
    "get_visual_trace",
    "trace_event",
    "DEFAULT_TRACE_PATH",
]
