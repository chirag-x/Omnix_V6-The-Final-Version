"""
Omnix V6 — Windows UWP / Microsoft Store application discovery.

UWP applications are listed in the Windows Start Apps index
(``Get-StartApps``) but their binary launcher is *not* a regular
``.exe`` in a PATH directory.  Instead, the canonical launch path
is:

    explorer.exe shell:AppsFolder\\<AppID>

where ``<AppID>`` is the package-relative application identifier
returned by ``Get-StartApps``.

This source enumerates the Start Apps index via PowerShell and
emits an :class:`ApplicationRecord` per entry.  The record's
``launch_command`` is the full ``explorer.exe shell:AppsFolder\\…``
string; the launch path passes that through ``subprocess.Popen`` with
``shell=True`` so Windows handles the protocol.

UWP process names
-----------------

A UWP record's ``executable`` field is a synthetic placeholder
(``"<name>.uwp"``) — the UWP runtime spawns a *real* .exe
inside ``C:\\Program Files\\WindowsApps\\<PackageFullName>\\…``
when the AppID is activated.  For example, Calculator's AppID
``Microsoft.WindowsCalculator_8wekyb3d8bbwe!App`` launches
``CalculatorApp.exe``; Terminal's launches ``WindowsTerminal.exe``.

To make ``is_running`` (which compares against ``record.executable``)
work for UWP apps, we record the *real* exe basenames in
``record.metadata["process_names"]``.  The application service's
``is_running`` consults that list for UWP records.  The names come
from two sources, in order:

1. ``Get-AppxPackageManifest`` (per package): the ``Application``
   element's ``Executable`` attribute.  This is the authoritative
   answer for AUMIDs (AppIDs of the form ``<PackageFamilyName>!App``).
2. Heuristic extraction from the AppID itself, for non-AUMID
   entries (Start Menu shortcut relocations, Chrome PWAs, Auto-
   Generated protocol launches, etc.): if the AppID contains a
   ``.exe`` substring, the path is parsed and the basename is
   recorded as a candidate.

The PowerShell call to ``Get-AppxPackageManifest`` is bounded to
one invocation per *unique PackageFamilyName*.  The mapping is
cached for the duration of the scan, so a catalog with 130 UWP
records (≈40 unique packages) finishes in well under 3 seconds.

Performance
-----------

``Get-StartApps`` returns in 200–500 ms; ``Get-AppxPackageManifest``
adds another 1–2 s the first time, but is cached.  The combined
scan completes in ~2.5 s, after which the indexed records resolve
in O(1).

This is a Windows-only source; on other platforms it is a no-op.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from typing import Dict, Iterator, List, Optional, Set, Tuple

from loguru import logger

from .discovery import ApplicationSource, normalize_name
from .models import ApplicationRecord


# PowerShell script to enumerate Start Apps.  We use ``Get-StartApps``
# (the cmdlet) and emit Name/AppID pairs as plain text, one per line.
# We avoid JSON because it requires the JSON cmdlet to be loaded on
# every invocation.
_PS_ENUMERATE = (
    "Get-StartApps "
    "| ForEach-Object { \"$($_.Name)`t$($_.AppID)\" }"
)

# PowerShell script to look up the actual Application executable(s)
# for a list of package family names.  We pass the names in as a
# single argument (a comma-joined string) and emit a tab-separated
# row per (PackageFamilyName, AppId, Executable) tuple.  The
# manifest is read once per *unique* package, so the cost is
# bounded by the number of unique packages rather than the number
# of Start Apps.
_PS_RESOLVE_EXES_TEMPLATE = (
    # We enumerate the package list *once* and index it in a
    # hashtable by both Name and PackageFamilyName.  Iterating
    # ``Get-AppxPackage | Where-Object`` once per input would
    # otherwise dominate the runtime (≈7s for ~50 names).
    "$lookup = @{}; "
    "Get-AppxPackage -ErrorAction SilentlyContinue | ForEach-Object { "
    "if (-not $lookup.ContainsKey($_.Name)) { $lookup[$_.Name] = $_ }; "
    "if (-not $lookup.ContainsKey($_.PackageFamilyName)) { $lookup[$_.PackageFamilyName] = $_ }; "
    "}; "
    "$Names = @(__NAMES__); "
    "$Names | Select-Object -Unique | ForEach-Object { "
    "$key = $_; "
    "$pkg = $null; "
    "if ($lookup.ContainsKey($key)) { $pkg = $lookup[$key] }; "
    "if ($pkg) { "
    "try { "
    "$apps = (Get-AppxPackageManifest -Package $pkg).Package.Applications.Application; "
    "foreach ($a in $apps) { "
    "Write-Output (\"$($pkg.PackageFamilyName)`t$($a.Id)`t$($a.Executable)\") "
    "} "
    "} catch {} "
    "} "
    "}"
)

# Pattern: AUMID (the canonical UWP AppID).
_AUMID_RE = re.compile(r"^([A-Za-z0-9.\-]+)_([a-z0-9]+)!(.+)$")

# Pattern: a few legacy / hand-crafted AppIDs omit the trailing
# ``!ApplicationId`` segment.  We still want to attempt a manifest
# lookup for these — PowerShell ``Get-AppxPackage`` accepts a
# package name with or without a trailing ``!App``.  The
# pattern matches ``Microsoft.VisualStudioCode``,
# ``Microsoft.WindowsCalculator`` (no underscore), etc.
_PACKAGE_NAME_RE = re.compile(r"^([A-Za-z0-9.\-]+)$")

# Pattern: extract a `.exe` from a non-AUMID AppID (paths).
_EXE_IN_PATH_RE = re.compile(r"([^\\/]+\.exe)", re.IGNORECASE)


def _build_launch_command(app_id: str) -> str:
    return f'explorer.exe shell:AppsFolder\\{app_id}'


def _basename_of_exe(exe_field: str) -> Optional[str]:
    """Strip a path/folder prefix and return the bare ``.exe`` name.

    AppxManifest executables may look like ``"Notepad\\Notepad.exe"``
    or ``"SnippingTool\\SnippingTool.exe"`` — a sub-folder plus the
    exe — so we want the trailing ``.exe``.  We don't want the .exe
    of a parent folder (e.g. for ``"C:\\foo\\bar.exe\\baz"`` the
    answer is ``None``).
    """
    if not exe_field:
        return None
    s = str(exe_field).strip().strip('"')
    if not s:
        return None
    # Take the last path component.
    base = s.replace("\\", "/").rstrip("/").split("/")[-1]
    if base.lower().endswith(".exe"):
        return base
    return None


def _fallback_exe_from_appid(app_id: str) -> List[str]:
    """For non-AUMID AppIDs (paths, PWAs, auto-generated), try to
    recover the launch target's basename."""
    out: List[str] = []
    m = _EXE_IN_PATH_RE.findall(app_id)
    for x in m:
        if x.lower().endswith(".exe"):
            out.append(x)
    # Dedupe (case-insensitive, preserve first).
    seen: Set[str] = set()
    deduped: List[str] = []
    for x in out:
        k = x.lower()
        if k not in seen:
            seen.add(k)
            deduped.append(x)
    return deduped


class UWPSource(ApplicationSource):
    """Enumerate Microsoft Store / UWP applications.

    Each record has ``executable="<name>.uwp"`` (synthetic) and
    ``launch_command`` that uses ``explorer.exe shell:AppsFolder``
    to start the app.  The real process name(s) are recorded in
    ``metadata["process_names"]`` so ``is_running`` can verify
    that the underlying .exe is actually live.
    """

    name = "uwp"
    confidence = 0.85

    def __init__(self) -> None:
        super().__init__(name=self.name, confidence=self.confidence, timeout_s=10.0)
        self._ps_path: Optional[str] = shutil.which("powershell") or shutil.which("powershell.exe")

    def scan(self) -> Iterator[ApplicationRecord]:
        if self._ps_path is None or os.name != "nt":
            return

        # 1. Enumerate Start Apps.
        try:
            proc = subprocess.run(
                [self._ps_path, "-NoProfile", "-Command", _PS_ENUMERATE],
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
            logger.debug(f"UWPSource: powershell failed: {exc!r}")
            return
        if proc.returncode != 0:
            logger.debug(f"UWPSource: powershell rc={proc.returncode} stderr={proc.stderr[:200]!r}")
            return

        # 2. Parse Name / AppID and group AppIDs by PackageFamilyName.
        #    We need this for the manifest lookup.
        records: List[ApplicationRecord] = []
        pfns: Set[str] = set()
        for line in proc.stdout.splitlines():
            line = line.rstrip()
            if not line:
                continue
            parts = line.split("\t", 1)
            if len(parts) != 2:
                continue
            name, app_id = parts[0].strip(), parts[1].strip()
            if not name or not app_id:
                continue
            if any(c in app_id for c in ("\n", "\r", '"')):
                continue
            m = _AUMID_RE.match(app_id)
            if m:
                pfns.add(f"{m.group(1)}_{m.group(2)}")
            else:
                # Plain package name (``Microsoft.VisualStudioCode``,
                # ``Microsoft.WindowsCalculator``) — the manifest
                # lookup will find the package by Name as well.
                if _PACKAGE_NAME_RE.match(app_id):
                    pfns.add(app_id)
            # Non-AUMID AppIDs (paths, auto-generated) are handled
            # in the enrichment step below.
            records.append(self._record_placeholder(name, app_id))

        # 3. Look up the real executables for the AUMIDs.
        #    ``process_names_by_aumid`` maps "<PFN>!<AppId>" -> list of
        #    real .exe basenames.  Empty list if the manifest can't be
        #    parsed.
        exes_by_aumid: Dict[str, List[str]] = {}
        if pfns:
            exes_by_aumid = self._resolve_executables(sorted(pfns))

        # 4. Enrich the records with the real process names.
        for rec in records:
            app_id = rec.metadata.get("app_id", "")
            real_names: List[str] = []
            m = _AUMID_RE.match(app_id)
            if m:
                aumid = f"{m.group(1)}_{m.group(2)}!{m.group(3)}"
                real_names.extend(exes_by_aumid.get(aumid, []))
            else:
                # Plain package name (e.g. ``Microsoft.VisualStudioCode``).
                # The real AUMID is ``<PackageName>_<8wekyb3d8bbwe>!<App>``
                # so we look up by prefix in the result map.
                if _PACKAGE_NAME_RE.match(app_id):
                    for key, names in exes_by_aumid.items():
                        # Match either ``<appid>_<...>!<...>``
                        # (full AUMID) or ``<appid>`` (rare).
                        if key.startswith(app_id + "_") or key == app_id:
                            real_names.extend(names)
            real_names.extend(_fallback_exe_from_appid(app_id))
            # Dedupe.
            seen: Set[str] = set()
            deduped: List[str] = []
            for n in real_names:
                k = n.lower()
                if k and k not in seen:
                    seen.add(k)
                    deduped.append(n)
            if deduped:
                rec.metadata["process_names"] = tuple(deduped)
            yield rec

    # ---------------------------------------------------------- helpers
    def _record_placeholder(
        self, name: str, app_id: str
    ) -> ApplicationRecord:
        """Build the synthetic UWP record before manifest enrichment."""
        display = name
        executable = f"{name}.uwp"
        launch_command = _build_launch_command(app_id)
        return ApplicationRecord(
            display_name=display,
            normalized_name=normalize_name(display),
            executable=executable,
            executable_path=None,
            launch_command=launch_command,
            source=self.name,
            installed=True,
            confidence=self.confidence,
            metadata={"app_id": app_id, "uwp": True},
        )

    def _resolve_executables(
        self, pfns: List[str]
    ) -> Dict[str, List[str]]:
        """Call PowerShell to look up ``Application.Executable`` for
        each unique package family name.  Returns a map keyed by
        the full AUMID (``<PFN>!<AppId>``) with the list of real
        .exe basenames for that application.
        """
        out: Dict[str, List[str]] = {}
        if not pfns or self._ps_path is None:
            return out
        # Build the PowerShell script with the names inlined as a
        # PowerShell array literal.  Inlining is more robust than
        # passing the names via ``-Command`` parameters — PowerShell
        # parses the whole script as a single AST and a bare array
        # is unambiguous.  We escape single quotes inside names
        # just in case, although PFNs never contain them.
        quoted = ",".join(
            f"'{n.replace(chr(39), chr(39) + chr(39))}'" for n in pfns
        )
        # Use a placeholder ``__NAMES__`` and string-replace to avoid
        # the PowerShell script's ``$variable`` and curly braces
        # being interpreted by Python's ``str.format``.
        script = _PS_RESOLVE_EXES_TEMPLATE.replace("__NAMES__", quoted)
        try:
            proc = subprocess.run(
                [self._ps_path, "-NoProfile", "-Command", script],
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
            logger.debug(f"UWPSource: manifest lookup failed: {exc!r}")
            return out
        if proc.returncode != 0:
            logger.debug(
                f"UWPSource: manifest lookup rc={proc.returncode} "
                f"stderr={proc.stderr[:200]!r}"
            )
            return out
        for line in proc.stdout.splitlines():
            line = line.rstrip()
            if not line:
                continue
            # Each line is "PFN\tAppId\tExecutable".
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            pfn, app_id, exe_field = parts[0].strip(), parts[1].strip(), parts[2].strip()
            base = _basename_of_exe(exe_field)
            if not base:
                continue
            key = f"{pfn}!{app_id}"
            out.setdefault(key, []).append(base)
        return out


__all__ = ["UWPSource"]
