"""
Omnix V6 — FilesystemService Implementation (Phase 2).

Implements :class:`core.execution.interfaces.FilesystemService` using
only the Python standard library (``pathlib``, ``fnmatch``, ``os``).

Safety contract
---------------
This service is the *only* place that should touch the filesystem on
behalf of a capability.  It enforces:

    * **Path sandboxing.**  Optional ``allowed_roots`` list — every
      operation is checked against this list.  By default sandboxing is
      disabled, but when enabled, paths outside the allowed roots are
      rejected with ``ActionStatus.FAILED``.
    * **Symbolic-link protection.**  When sandboxing is enabled, paths
      that resolve through a symlink to outside the sandbox are also
      rejected (via ``Path.resolve(strict=False)``).
    * **Read-only roots.**  When a path is in a read-only root,
      ``write_text`` is rejected.

The contract says *capability calls* must use this service, so all
write calls are *explicit* and auditable.  Nothing in the engine
imports ``pathlib`` directly.
"""

from __future__ import annotations

import fnmatch
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger as _loguru

from core.execution.interfaces import FilesystemService
from core.lifecycle import LifecycleMixin, LifecycleState
from core.results import ActionResult, ActionStatus


# Default: 16 MiB cap on read_text to prevent accidentally slurping huge files
MAX_READ_BYTES = 16 * 1024 * 1024
MAX_WRITE_BYTES = 16 * 1024 * 1024
MAX_SEARCH_RESULTS = 1000


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

class WindowsFilesystemService(FilesystemService, LifecycleMixin):
    """Sandbox-aware :class:`FilesystemService` (cross-platform impl)."""

    def __init__(
        self,
        *,
        allowed_roots: Optional[List[str]] = None,
        read_only_roots: Optional[List[str]] = None,
        enable_sandbox: bool = False,
    ) -> None:
        self._lifecycle_state: LifecycleState = LifecycleState.CREATED
        self._initialization_error: Optional[str] = None
        self._allowed_roots: List[Path] = [
            Path(p).resolve() for p in (allowed_roots or [])
        ]
        self._read_only_roots: List[Path] = [
            Path(p).resolve() for p in (read_only_roots or [])
        ]
        self._enable_sandbox: bool = enable_sandbox
        _loguru.debug(
            "WindowsFilesystemService initialized "
            "(sandbox={}, allowed_roots={}, read_only_roots={}).",
            enable_sandbox,
            [str(p) for p in self._allowed_roots],
            [str(p) for p in self._read_only_roots],
        )

    # ============================================================ helpers
    def _normalize(self, path: str) -> Path:
        return Path(os.path.expandvars(os.path.expanduser(str(path)))).resolve()

    def _check_allowed(self, path: Path) -> Optional[str]:
        """Return an error message if ``path`` is outside the sandbox."""
        if not self._enable_sandbox:
            return None
        if not self._allowed_roots:
            return None
        for root in self._allowed_roots:
            try:
                path.relative_to(root)
                return None
            except ValueError:
                continue
        return f"path {path!s} is outside allowed roots"

    def _check_writable(self, path: Path) -> Optional[str]:
        if not self._read_only_roots:
            return None
        for root in self._read_only_roots:
            try:
                path.relative_to(root)
                return f"path {path!s} is in a read-only root"
            except ValueError:
                continue
        return None

    def _ok(self, action: str, **details: Any) -> ActionResult:
        return ActionResult(
            status=ActionStatus.EXECUTED,
            action_name=action,
            details=details,
        )

    def _fail(self, action: str, msg: str, **details: Any) -> ActionResult:
        return ActionResult(
            status=ActionStatus.FAILED,
            action_name=action,
            details={"reason": msg, **details},
        )

    # ============================================================ API
    def read_text(self, path: str, *, encoding: str = "utf-8") -> str:
        try:
            p = self._normalize(path)
        except Exception:  # noqa: BLE001
            return ""
        err = self._check_allowed(p)
        if err is not None:
            _loguru.warning("read_text denied: {}", err)
            return ""
        try:
            if not p.exists():
                return ""
            if p.is_dir():
                return ""
            size = p.stat().st_size
            if size > MAX_READ_BYTES:
                _loguru.warning(
                    "read_text: file too large ({} bytes), truncating", size,
                )
            with p.open("r", encoding=encoding, errors="replace") as f:
                return f.read(MAX_READ_BYTES)
        except Exception as exc:  # noqa: BLE001
            _loguru.warning("read_text({}) failed: {}", path, exc)
            return ""

    def write_text(
        self,
        path: str,
        content: str,
        *,
        encoding: str = "utf-8",
    ) -> ActionResult:
        action = "write_text"
        if not isinstance(content, str):
            return self._fail(action, "content must be a string")
        if len(content.encode(encoding, errors="replace")) > MAX_WRITE_BYTES:
            return self._fail(
                action, "content too large", max_bytes=MAX_WRITE_BYTES,
            )
        try:
            p = self._normalize(path)
        except Exception as exc:  # noqa: BLE001
            return self._fail(action, repr(exc))
        err = self._check_allowed(p)
        if err is not None:
            return self._fail(action, err)
        err = self._check_writable(p)
        if err is not None:
            return self._fail(action, err)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("w", encoding=encoding) as f:
                f.write(content)
        except Exception as exc:  # noqa: BLE001
            return self._fail(action, repr(exc), path=str(p))
        return self._ok(
            action, path=str(p), length=len(content), encoding=encoding,
        )

    def exists(self, path: str) -> bool:
        try:
            p = self._normalize(path)
        except Exception:  # noqa: BLE001
            return False
        return p.exists()

    def list_dir(self, path: str) -> List[str]:
        try:
            p = self._normalize(path)
        except Exception:  # noqa: BLE001
            return []
        err = self._check_allowed(p)
        if err is not None:
            _loguru.warning("list_dir denied: {}", err)
            return []
        try:
            if not p.is_dir():
                return []
            return sorted(e.name for e in p.iterdir())
        except Exception as exc:  # noqa: BLE001
            _loguru.warning("list_dir({}) failed: {}", path, exc)
            return []

    def search(
        self,
        root: str,
        pattern: str,
        *,
        recursive: bool = True,
    ) -> List[str]:
        try:
            r = self._normalize(root)
        except Exception:  # noqa: BLE001
            return []
        err = self._check_allowed(r)
        if err is not None:
            _loguru.warning("search denied: {}", err)
            return []
        if not r.is_dir():
            return []
        results: List[str] = []
        try:
            if recursive:
                walker = r.rglob("*")
            else:
                walker = r.glob("*")
            for entry in walker:
                if not entry.is_file():
                    continue
                name = entry.name
                if fnmatch.fnmatch(name, pattern):
                    results.append(str(entry))
                    if len(results) >= MAX_SEARCH_RESULTS:
                        _loguru.warning(
                            "search hit max_results cap ({}). Truncating.",
                            MAX_SEARCH_RESULTS,
                        )
                        break
        except Exception as exc:  # noqa: BLE001
            _loguru.warning("search({}, {}) failed: {}", root, pattern, exc)
        return results

    # =================================================== lifecycle hooks
    def _do_initialize(self) -> bool:
        return True

    def _do_shutdown(self) -> None:
        return None

    def statistics(self) -> Dict[str, Any]:
        return {
            "type": "WindowsFilesystemService",
            "lifecycle": self._lifecycle_state.value,
            "sandbox_enabled": self._enable_sandbox,
            "allowed_roots": [str(p) for p in self._allowed_roots],
            "read_only_roots": [str(p) for p in self._read_only_roots],
        }

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return (
            f"WindowsFilesystemService(state={self._lifecycle_state.value}, "
            f"sandbox={self._enable_sandbox})"
        )
