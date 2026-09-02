"""
Omnix V6 - Filesystem Capabilities.

Provides implementations for common filesystem operations such as reading
and writing files, checking existence, listing directories, creating files
and folders, and deleting paths.

Phase 12 (REAL AUTOMATION EXECUTION LAYER) extends this module with:

    * :class:`FileCreateCapability`  -- create an empty file (destructive
      if the path already exists; refuses to overwrite by default).
    * :class:`FolderCreateCapability` -- create a directory tree
      (``mkdir -p`` semantics; ``destructive=False`` when the folder
      already exists and matches expectations).
    * :class:`FileDeleteCapability`   -- delete a file or empty directory;
      gated ``dangerous=True`` so the safety policy must approve it.
    * :class:`DirectoryListCapability` -- list the entries of a directory
      (read-only, no destructive side effects).
"""

import os
import asyncio
from typing import Any, Mapping

from core.capability import CapabilitySpec, CapabilityParameter, ParamType
from core.results import CapabilityResult, CapabilityStatus, ActionResult, ActionStatus
from .base import BaseCapability
from core.errors import OmnixError

class FileReadCapability(BaseCapability):
    """Capability to read the contents of a file."""
    
    @property
    def spec(self) -> CapabilitySpec:
        return CapabilitySpec(
            name="file.read",
            version="1.0.0",
            description="Reads the contents of a file from the filesystem.",
            parameters={
                "path": CapabilityParameter(
                    name="path",
                    type=ParamType.PATH,
                    description="Absolute path to the file to read.",
                    required=True
                )
            },
            tags={"filesystem", "read"}
        )

    async def execute(self, params: Mapping[str, Any]) -> CapabilityResult:
        path = params.get("path")
        if not path:
            return CapabilityResult(
                capability_name=self.spec.name,
                status=CapabilityStatus.FAILED,
                failed=True,
                error=OmnixError("Path parameter is required.")
            )
            
        if not os.path.isabs(path):
            return CapabilityResult(
                capability_name=self.spec.name,
                status=CapabilityStatus.FAILED,
                failed=True,
                error=OmnixError(f"Path must be absolute: {path}")
            )
            
        if not os.path.exists(path) or not os.path.isfile(path):
            return CapabilityResult(
                capability_name=self.spec.name,
                status=CapabilityStatus.FAILED,
                failed=True,
                error=OmnixError(f"File not found or is not a file: {path}")
            )
            
        try:
            # Using loop.run_in_executor for async file reading without aiofiles
            loop = asyncio.get_running_loop()
            
            def _read_file():
                with open(path, mode='r', encoding='utf-8') as f:
                    return f.read()
                    
            content = await loop.run_in_executor(None, _read_file)
                
            return CapabilityResult(
                capability_name=self.spec.name,
                status=CapabilityStatus.VERIFIED,
                attempted=True,
                executed=True,
                verified=True,
                action=ActionResult(status=ActionStatus.EXECUTED, action_name=self.spec.name),
                details={"content": content}
            )
        except Exception as e:
            return CapabilityResult(
                capability_name=self.spec.name,
                status=CapabilityStatus.FAILED,
                failed=True,
                error=OmnixError(f"Failed to read file: {str(e)}")
            )


class FileWriteCapability(BaseCapability):
    """Capability to write content to a file."""
    
    @property
    def spec(self) -> CapabilitySpec:
        return CapabilitySpec(
            name="file.write",
            version="1.0.0",
            description="Writes content to a file on the filesystem.",
            parameters={
                "path": CapabilityParameter(
                    name="path",
                    type=ParamType.PATH,
                    description="Absolute path to the file to write.",
                    required=True
                ),
                "content": CapabilityParameter(
                    name="content",
                    type=ParamType.STRING,
                    description="The string content to write to the file.",
                    required=True
                )
            },
            tags={"filesystem", "write", "destructive"}
        )

    async def execute(self, params: Mapping[str, Any]) -> CapabilityResult:
        path = params.get("path")
        content = params.get("content")
        
        if not path:
            return CapabilityResult(
                capability_name=self.spec.name,
                status=CapabilityStatus.FAILED,
                failed=True,
                error=OmnixError("Path parameter is required.")
            )
            
        if content is None:
            return CapabilityResult(
                capability_name=self.spec.name,
                status=CapabilityStatus.FAILED,
                failed=True,
                error=OmnixError("Content parameter is required.")
            )
            
        if not os.path.isabs(path):
            return CapabilityResult(
                capability_name=self.spec.name,
                status=CapabilityStatus.FAILED,
                failed=True,
                error=OmnixError(f"Path must be absolute: {path}")
            )
            
        try:
            # Using loop.run_in_executor for async file writing without aiofiles
            loop = asyncio.get_running_loop()
            
            def _write_file():
                # Ensure directory exists
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, mode='w', encoding='utf-8') as f:
                    f.write(str(content))
                return len(str(content))
                
            bytes_written = await loop.run_in_executor(None, _write_file)
                
            return CapabilityResult(
                capability_name=self.spec.name,
                status=CapabilityStatus.VERIFIED,
                attempted=True,
                executed=True,
                verified=True,
                action=ActionResult(status=ActionStatus.EXECUTED, action_name=self.spec.name),
                details={"path": path, "bytes_written": bytes_written}
            )
        except Exception as e:
            return CapabilityResult(
                capability_name=self.spec.name,
                status=CapabilityStatus.FAILED,
                failed=True,
                error=OmnixError(f"Failed to write file: {str(e)}")
            )


# ---------------------------------------------------------------------------
# Phase 12: filesystem create / list / delete capabilities
# ---------------------------------------------------------------------------

def _is_under_reserved_dir(abs_path: str) -> bool:
    """Return ``True`` when ``abs_path`` is inside a path we refuse to touch.

    The V6 standard capability set treats certain root locations as
    reserved because the user-facing surface of a real desktop
    automation agent must never silently nuke system state.  The
    router / safety layer is the primary gate; this helper is the
    second line of defence for :class:`FileDeleteCapability`.

    Windows file system paths are case-insensitive; the comparison
    is performed case-insensitively as well, so
    ``C:\\Windows\\System32`` and ``C:\\WINDOWS\\System32`` both
    match.  The reserved roots are sourced from the environment
    (``%SystemRoot%``, ``%ProgramFiles%``, ``%ProgramFiles(x86)%``)
    and fall back to canonical defaults when the env vars are not
    set (e.g. running under ``pytest`` on a Linux CI host).
    """
    if not abs_path:
        return True
    norm = os.path.normpath(os.path.abspath(abs_path))
    norm_lower = norm.lower() if os.name == "nt" else norm
    reserved_roots = (
        os.path.normpath(os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "")),
        os.path.normpath(os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "")),
        os.path.normpath(os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"), "")),
    )
    for root in reserved_roots:
        if not root:
            continue
        cmp_root = root.lower() if os.name == "nt" else root
        cmp_norm = norm_lower
        if cmp_norm == cmp_root or cmp_norm.startswith(cmp_root + os.sep):
            return True
    return False


class FileCreateCapability(BaseCapability):
    """Create an empty file at ``path``.

    The capability refuses to overwrite an existing file unless
    ``overwrite=True`` is supplied.  It does NOT delete a directory
    that happens to live at the path -- it fails instead, so the
    caller can correct the input rather than lose state by accident.
    """

    @property
    def spec(self) -> CapabilitySpec:
        return CapabilitySpec(
            name="file.create",
            version="1.0.0",
            description="Create an empty file (or replace it when overwrite=True).",
            parameters={
                "path": CapabilityParameter(
                    name="path",
                    type=ParamType.PATH,
                    description="Absolute path of the file to create.",
                    required=True,
                ),
                "overwrite": CapabilityParameter(
                    name="overwrite",
                    type=ParamType.BOOLEAN,
                    required=False,
                    default=False,
                    description="If true, replace an existing file at path.",
                ),
            },
            tags={"filesystem", "create", "destructive"},
        )

    async def execute(self, params: Mapping[str, Any]) -> CapabilityResult:
        path = params.get("path")
        overwrite = bool(params.get("overwrite", False))
        if not path:
            return CapabilityResult(
                capability_name=self.spec.name,
                status=CapabilityStatus.FAILED,
                failed=True,
                error=OmnixError("Path parameter is required."),
            )
        if not os.path.isabs(path):
            return CapabilityResult(
                capability_name=self.spec.name,
                status=CapabilityStatus.FAILED,
                failed=True,
                error=OmnixError(f"Path must be absolute: {path}"),
            )
        if os.path.isdir(path):
            return CapabilityResult(
                capability_name=self.spec.name,
                status=CapabilityStatus.FAILED,
                failed=True,
                error=OmnixError(
                    f"Cannot create file over a directory: {path}"
                ),
            )
        if os.path.exists(path) and not overwrite:
            return CapabilityResult(
                capability_name=self.spec.name,
                status=CapabilityStatus.FAILED,
                failed=True,
                error=OmnixError(
                    f"File already exists and overwrite=False: {path}"
                ),
            )

        loop = asyncio.get_running_loop()

        def _create() -> str:
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write("")
            return os.path.abspath(path)

        try:
            created = await loop.run_in_executor(None, _create)
        except Exception as exc:  # noqa: BLE001
            return CapabilityResult(
                capability_name=self.spec.name,
                status=CapabilityStatus.FAILED,
                failed=True,
                error=OmnixError(f"Failed to create file: {exc!r}"),
            )

        return CapabilityResult(
            capability_name=self.spec.name,
            status=CapabilityStatus.VERIFIED,
            attempted=True,
            executed=True,
            verified=True,
            action=ActionResult(
                status=ActionStatus.EXECUTED, action_name=self.spec.name
            ),
            details={"path": created, "bytes_written": 0, "overwrite": overwrite},
        )


class FolderCreateCapability(BaseCapability):
    """Create a directory tree (``mkdir -p`` semantics).

    Returns VERIFIED regardless of whether the directory already
    existed: an idempotent mkdir is a successful *no-op*, not a
    failure.
    """

    @property
    def spec(self) -> CapabilitySpec:
        return CapabilitySpec(
            name="folder.create",
            version="1.0.0",
            description="Create a directory (recursively). Idempotent.",
            parameters={
                "path": CapabilityParameter(
                    name="path",
                    type=ParamType.PATH,
                    description="Absolute path of the directory to create.",
                    required=True,
                ),
            },
            tags={"filesystem", "create", "destructive"},
        )

    async def execute(self, params: Mapping[str, Any]) -> CapabilityResult:
        path = params.get("path")
        if not path:
            return CapabilityResult(
                capability_name=self.spec.name,
                status=CapabilityStatus.FAILED,
                failed=True,
                error=OmnixError("Path parameter is required."),
            )
        if not os.path.isabs(path):
            return CapabilityResult(
                capability_name=self.spec.name,
                status=CapabilityStatus.FAILED,
                failed=True,
                error=OmnixError(f"Path must be absolute: {path}"),
            )
        if os.path.exists(path) and not os.path.isdir(path):
            return CapabilityResult(
                capability_name=self.spec.name,
                status=CapabilityStatus.FAILED,
                failed=True,
                error=OmnixError(
                    f"Cannot create folder over an existing file: {path}"
                ),
            )

        loop = asyncio.get_running_loop()

        def _create() -> str:
            os.makedirs(path, exist_ok=True)
            return os.path.abspath(path)

        try:
            created = await loop.run_in_executor(None, _create)
        except Exception as exc:  # noqa: BLE001
            return CapabilityResult(
                capability_name=self.spec.name,
                status=CapabilityStatus.FAILED,
                failed=True,
                error=OmnixError(f"Failed to create folder: {exc!r}"),
            )

        return CapabilityResult(
            capability_name=self.spec.name,
            status=CapabilityStatus.VERIFIED,
            attempted=True,
            executed=True,
            verified=True,
            action=ActionResult(
                status=ActionStatus.EXECUTED, action_name=self.spec.name
            ),
            details={"path": created, "existed": False},
        )


class FileDeleteCapability(BaseCapability):
    """Delete a file or empty directory.

    This capability is marked ``dangerous=True`` so the V6 safety
    policy must explicitly authorise the call.  The implementation
    also refuses to touch reserved system paths
    (``%SystemRoot%``, ``%ProgramFiles%``, ``%ProgramFiles(x86%)``)
    regardless of any safety override -- the user is the only
    party allowed to delete those.
    """

    @property
    def spec(self) -> CapabilitySpec:
        return CapabilitySpec(
            name="file.delete",
            version="1.0.0",
            description="Delete a file or empty directory at the given path.",
            parameters={
                "path": CapabilityParameter(
                    name="path",
                    type=ParamType.PATH,
                    description="Absolute path of the file/folder to delete.",
                    required=True,
                ),
            },
            dangerous=True,
            tags={"filesystem", "delete", "destructive"},
        )

    async def execute(self, params: Mapping[str, Any]) -> CapabilityResult:
        path = params.get("path")
        if not path:
            return CapabilityResult(
                capability_name=self.spec.name,
                status=CapabilityStatus.FAILED,
                failed=True,
                error=OmnixError("Path parameter is required."),
            )
        if not os.path.isabs(path):
            return CapabilityResult(
                capability_name=self.spec.name,
                status=CapabilityStatus.FAILED,
                failed=True,
                error=OmnixError(f"Path must be absolute: {path}"),
            )
        if _is_under_reserved_dir(path):
            return CapabilityResult(
                capability_name=self.spec.name,
                status=CapabilityStatus.FAILED,
                failed=True,
                error=OmnixError(
                    f"Refusing to delete reserved system path: {path}"
                ),
            )
        if not os.path.exists(path):
            return CapabilityResult(
                capability_name=self.spec.name,
                status=CapabilityStatus.FAILED,
                failed=True,
                error=OmnixError(f"Path does not exist: {path}"),
            )

        loop = asyncio.get_running_loop()

        def _delete() -> str:
            if os.path.isdir(path):
                # Refuse to recursively delete: only empty directories
                # are accepted here.  The capability name reflects the
                # safety ceiling.
                entries = os.listdir(path)
                if entries:
                    raise IsADirectoryError(
                        f"Directory is not empty: {path}"
                    )
                os.rmdir(path)
                return "directory"
            os.remove(path)
            return "file"

        try:
            kind = await loop.run_in_executor(None, _delete)
        except IsADirectoryError as exc:
            return CapabilityResult(
                capability_name=self.spec.name,
                status=CapabilityStatus.FAILED,
                failed=True,
                error=OmnixError(str(exc)),
            )
        except Exception as exc:  # noqa: BLE001
            return CapabilityResult(
                capability_name=self.spec.name,
                status=CapabilityStatus.FAILED,
                failed=True,
                error=OmnixError(f"Failed to delete path: {exc!r}"),
            )

        return CapabilityResult(
            capability_name=self.spec.name,
            status=CapabilityStatus.VERIFIED,
            attempted=True,
            executed=True,
            verified=True,
            action=ActionResult(
                status=ActionStatus.EXECUTED, action_name=self.spec.name
            ),
            details={"path": os.path.abspath(path), "kind": kind},
        )


class DirectoryListCapability(BaseCapability):
    """List the entries of a directory (non-recursive, read-only)."""

    @property
    def spec(self) -> CapabilitySpec:
        return CapabilitySpec(
            name="directory.list",
            version="1.0.0",
            description="List the immediate entries of a directory.",
            parameters={
                "path": CapabilityParameter(
                    name="path",
                    type=ParamType.PATH,
                    description="Absolute path of the directory to list.",
                    required=True,
                ),
                "include_hidden": CapabilityParameter(
                    name="include_hidden",
                    type=ParamType.BOOLEAN,
                    required=False,
                    default=False,
                    description="If true, include dotfiles / hidden entries.",
                ),
            },
            tags={"filesystem", "read", "observation"},
        )

    async def execute(self, params: Mapping[str, Any]) -> CapabilityResult:
        path = params.get("path")
        include_hidden = bool(params.get("include_hidden", False))
        if not path:
            return CapabilityResult(
                capability_name=self.spec.name,
                status=CapabilityStatus.FAILED,
                failed=True,
                error=OmnixError("Path parameter is required."),
            )
        if not os.path.isabs(path):
            return CapabilityResult(
                capability_name=self.spec.name,
                status=CapabilityStatus.FAILED,
                failed=True,
                error=OmnixError(f"Path must be absolute: {path}"),
            )
        if not os.path.isdir(path):
            return CapabilityResult(
                capability_name=self.spec.name,
                status=CapabilityStatus.FAILED,
                failed=True,
                error=OmnixError(f"Not a directory: {path}"),
            )

        loop = asyncio.get_running_loop()

        def _list() -> list:
            entries = os.listdir(path)
            if not include_hidden:
                entries = [e for e in entries if not e.startswith(".")]
            entries.sort(key=str.lower)
            return entries

        try:
            entries = await loop.run_in_executor(None, _list)
        except Exception as exc:  # noqa: BLE001
            return CapabilityResult(
                capability_name=self.spec.name,
                status=CapabilityStatus.FAILED,
                failed=True,
                error=OmnixError(f"Failed to list directory: {exc!r}"),
            )

        return CapabilityResult(
            capability_name=self.spec.name,
            status=CapabilityStatus.VERIFIED,
            attempted=True,
            executed=True,
            verified=True,
            action=ActionResult(
                status=ActionStatus.EXECUTED, action_name=self.spec.name
            ),
            details={"path": os.path.abspath(path), "entries": entries, "count": len(entries)},
        )
