"""
Omnix V6 — Application domain models.

The discovery subsystem produces :class:`ApplicationRecord` objects that
represent the applications actually installed on the host.  These
records are the canonical, source-agnostic representation — Registry,
App Paths, Start Menu, PATH, and running processes all produce the same
shape.  The catalog, resolver, and application capability consume the
records without caring which source produced them.

The dataclasses here are intentionally small and immutable; new
discovery sources plug in by emitting these, not by mutating them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple


# ---------------------------------------------------------------------------
# ApplicationRecord
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ApplicationRecord:
    """A single application the system knows about.

    Attributes
    ----------
    display_name:
        The human-readable name (e.g. "Google Chrome", "Notepad").
    normalized_name:
        Lower-case, stripped of spaces and punctuation; the primary
        key the catalog indexes on (e.g. ``"googlechrome"``,
        ``"notepad"``).
    aliases:
        Additional normalized names this record also matches.  The
        catalog indexes these into the same primary record, so a
        lookup for any alias returns this record.
    executable:
        The executable file name (e.g. ``"chrome.exe"``).  Always a
        bare filename — never a path.
    executable_path:
        The absolute path to the executable when known, otherwise
        ``None``.  When present, this is the launch target.
    launch_command:
        The string the application service passes to ``subprocess``.
        For most apps this is the full path to the executable.
    source:
        Which discovery source produced the record
        (``"registry"`` / ``"app_paths"`` / ``"start_menu"`` /
        ``"path"`` / ``"process"``).
    installed:
        ``True`` for any record we believe is currently installed.
        Records for currently running processes have this set to
        ``True`` regardless of whether they are also on disk.
    confidence:
        0..1; higher is more confidence.  Running processes are 1.0;
        Registry Uninstall entries are ~0.9; PATH hits are 0.5.
    metadata:
        Free-form per-source data (DisplayVersion, publisher, etc.).
    """

    display_name: str
    normalized_name: str
    executable: str
    launch_command: str
    source: str
    installed: bool = True
    aliases: Tuple[str, ...] = ()
    executable_path: Optional[str] = None
    confidence: float = 0.5
    metadata: Dict[str, Any] = field(default_factory=dict)

    def matches(self, query: str) -> bool:
        """True if ``query`` (already normalized) matches this record."""
        if not query:
            return False
        if query == self.normalized_name:
            return True
        if query in self.aliases:
            return True
        # Match the executable stem without the ``.exe`` suffix so
        # ``"chrome"`` resolves to a record whose executable is
        # ``"chrome.exe"``.  This is the only place we hardcode
        # the .exe convention; everywhere else, the catalog is
        # the source of truth.
        if self.executable.lower().endswith(".exe"):
            stem = self.executable[:-4]
            if stem == query:
                return True
        return False


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Resolution:
    """The resolver's verdict for one user-facing name lookup.

    Exactly one of ``record`` / ``candidates`` is non-empty for a
    successful lookup.  ``not_found`` is the only honest answer for
    apps the system genuinely does not know about.
    """

    status: str                # "found" | "not_found" | "ambiguous"
    record: Optional[ApplicationRecord] = None
    candidates: Tuple[ApplicationRecord, ...] = ()
    reason: str = ""

    @property
    def is_found(self) -> bool:
        return self.status == "found" and self.record is not None

    @property
    def is_not_found(self) -> bool:
        return self.status == "not_found"

    @property
    def is_ambiguous(self) -> bool:
        return self.status == "ambiguous"
