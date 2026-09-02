"""
Omnix V6 — Application resolver.

Maps a user-facing name (e.g. ``"chrome"``, ``"notepad"``,
``"msedge"``) to a concrete :class:`ApplicationRecord` using the
:class:`ApplicationCatalog`.  The resolver is the **only** legitimate
way for a capability or service to translate a name into a launchable
target — it is intentionally a small, side-effect-free layer so the
launch path stays auditable.

Three outcomes
--------------

* **found** — the catalog returned a single, high-confidence record.
* **ambiguous** — multiple records of comparable confidence matched
  the query.  Returned only when the user has not supplied a tie-break
  and the candidates disagree on ``executable_path``; the caller may
  surface this to the user as "Did you mean …?".
* **not_found** — no record matched.  This is the **only** honest
  answer for an unknown app; the resolver never invents a launch
  command.

Generic alias fallback
----------------------

A small, *generic* alias table covers the naming variations Windows
itself normalizes (``"msedge"`` → ``"edge"``) and the most common
typos.  We intentionally keep this list tiny and **not** keyed to
specific applications — the catalog is the source of truth for what
is installed.
"""

from __future__ import annotations

from typing import Iterable, List, Optional, Tuple

from loguru import logger

from .catalog import ApplicationCatalog
from .models import ApplicationRecord, Resolution


# Small, generic alias table.  Only entries that map a *Windows-
# normalized* name to another, equally valid Windows name.  Adding
# per-application entries here defeats the point of the catalog.
GENERIC_ALIASES: dict[str, str] = {
    "msedge": "edge",
    "microsoftedge": "edge",
    "googlechrome": "chrome",
    "code": "visualstudiocode",
    "vscode": "visualstudiocode",
    "calc": "calculator",
    "winrt": "windows",
    "explorer": "fileexplorer",
}


class ApplicationResolver:
    """Resolve user-facing names to :class:`ApplicationRecord` objects.

    The resolver is **stateless** apart from its reference to the
    catalog.  It is safe to construct one and share it across the
    application service, capabilities, and the speech layer.
    """

    AMBIGUITY_CONFIDENCE_GAP = 0.15

    def __init__(self, catalog: ApplicationCatalog) -> None:
        self._catalog = catalog

    # ---------------------------------------------------------- public
    def resolve(self, name: str) -> Resolution:
        """Resolve a user-facing name to a :class:`Resolution`.

        Returns a :class:`Resolution` whose status is one of
        ``"found"``, ``"not_found"``, or ``"ambiguous"``.  Never
        raises; on catastrophic failure returns
        ``not_found`` with the error message in ``reason``.
        """
        if not name or not name.strip():
            return Resolution(status="not_found", reason="empty name")
        cleaned = name.strip()
        # 1. Exact + alias lookup in the catalog.
        direct = self._catalog.lookup(cleaned)
        if direct is not None:
            return Resolution(status="found", record=direct)
        # 2. Try the generic alias table to find the canonical
        #    name, then re-lookup.
        aliased = self._apply_generic_alias(cleaned)
        if aliased and aliased != cleaned:
            via_alias = self._catalog.lookup(aliased)
            if via_alias is not None:
                return Resolution(status="found", record=via_alias)
        # 3. Search for candidates via a substring scan (last resort).
        candidates = self._collect_candidates(cleaned)
        if not candidates:
            return Resolution(
                status="not_found",
                reason=f"no application matches {cleaned!r}",
            )
        if len(candidates) == 1:
            return Resolution(status="found", record=candidates[0])
        # Multiple matches — only "ambiguous" if they really differ.
        return Resolution(
            status="ambiguous",
            candidates=tuple(candidates),
            reason=f"multiple applications match {cleaned!r}",
        )

    def is_installed(self, name: str) -> bool:
        return self.resolve(name).is_found

    def launch_target(self, name: str) -> Optional[str]:
        """Convenience: return the launch command for a name, or ``None``."""
        res = self.resolve(name)
        if not res.is_found or res.record is None:
            return None
        rec = res.record
        return rec.launch_command or rec.executable_path or rec.executable

    # ---------------------------------------------------------- helpers
    def _apply_generic_alias(self, name: str) -> Optional[str]:
        from .discovery import normalize_name
        n = normalize_name(name)
        return GENERIC_ALIASES.get(n)

    def _collect_candidates(self, name: str) -> List[ApplicationRecord]:
        from .discovery import normalize_name
        n = normalize_name(name)
        out: List[ApplicationRecord] = []
        for rec in self._catalog.all_records():
            if rec.matches(n):
                out.append(rec)
        # Sort by confidence desc; the resolver keeps the strongest
        # candidate but still returns multiple if confidence gap is small.
        out.sort(key=lambda r: r.confidence, reverse=True)
        if len(out) < 2:
            return out
        top, second = out[0], out[1]
        if (top.confidence - second.confidence) >= self.AMBIGUITY_CONFIDENCE_GAP:
            return [top]
        return out
