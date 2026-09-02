"""
Omnix V6 — Windows application discovery.

Five sources are scanned, each producing :class:`ApplicationRecord`
objects:

1. **Registry Uninstall** — ``HKLM\\SOFTWARE\\Microsoft\\Windows\\
   CurrentVersion\\Uninstall`` and the per-user sibling.  This is the
   richest source: DisplayName, DisplayVersion, InstallLocation,
   Publisher, DisplayIcon, EstimatedSize.

2. **App Paths** — ``HKLM\\SOFTWARE\\Microsoft\\Windows\\
   CurrentVersion\\App Paths\\<exe>``.  Each subkey has a ``(Default)``
   value pointing to the executable.  This is the source the Windows
   Run dialog uses, so it is reliable for the most common apps.

3. **Start Menu shortcuts** — walks the per-user and per-machine Start
   Menu directories and parses ``.lnk`` files.  When ``pywin32`` is
   available, the shortcut target is resolved; otherwise the link
   filename is used as the application name.

4. **PATH executables** — the directories listed in the ``PATH``
   environment variable, first level only.  This is the
   lowest-confidence source; the catalog treats it as a fallback.

5. **Running processes** — current process list via ``psutil``.
   This has the highest confidence because we *know* the binary is
   executable right now, but it does not tell us whether the app is
   *installed* in the durable sense.

Each source is independent; the catalog merges the records.  The
discovery does **not** scan the entire disk — PATH is bounded to the
declared directories, the Start Menu walk is bounded to its known
roots, and the registry walk is bounded to the two well-known hives.

Sources are pluggable.  New sources can be added by implementing
:func:`ApplicationSource.scan` and registering them with the catalog
in :meth:`ApplicationCatalog.refresh`.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

from loguru import logger

from .models import ApplicationRecord


# ---------------------------------------------------------------------------
# Normalization helpers
# ---------------------------------------------------------------------------

_PUNCT_RE = re.compile(r"[^a-z0-9]+")


def normalize_name(text: str) -> str:
    """Lower-case, strip punctuation, collapse whitespace. Used as the
    canonical key for catalog lookup."""
    if not text:
        return ""
    s = str(text).strip().lower()
    s = _PUNCT_RE.sub("", s)
    return s


def _executable_from_icon(icon_field: str) -> Optional[str]:
    """Extract the executable portion of a Registry ``DisplayIcon`` value.

    ``DisplayIcon`` is often ``"<path>\\\\icon.exe,0"`` — the trailing
    ``,0`` is the icon index.  Sometimes the path is quoted.  We only
    want the executable name, not the icon index.
    """
    if not icon_field:
        return None
    s = str(icon_field).strip().strip('"')
    if "," in s:
        s = s.split(",", 1)[0].strip().strip('"')
    base = os.path.basename(s)
    if base.lower().endswith((".exe", ".bat", ".cmd")):
        return base
    return None


def _executable_name(path: str) -> str:
    """Return the bare executable name from a path (with extension)."""
    return os.path.basename(path or "")


# ---------------------------------------------------------------------------
# Source protocol
# ---------------------------------------------------------------------------

@dataclass
class ApplicationSource:
    """A single source of :class:`ApplicationRecord` entries.

    Subclasses override :meth:`scan`; the catalog calls ``scan()`` and
    collects the records.  Each source is responsible for its own
    timeouts, retries, and error logging — a failed source must not
    crash the catalog refresh.
    """

    name: str = ""
    confidence: float = 0.5
    enabled: bool = True
    timeout_s: float = 10.0

    def scan(self) -> Iterator[ApplicationRecord]:  # pragma: no cover - abstract
        raise NotImplementedError

    # ---------------------------------------------------------- helpers
    def safe_scan(self) -> List[ApplicationRecord]:
        """Wrap :meth:`scan` with a soft timeout and exception guard."""
        out: List[ApplicationRecord] = []
        if not self.enabled:
            return out
        started = time.time()
        try:
            for rec in self.scan():
                if time.time() - started > self.timeout_s:
                    logger.warning(
                        f"application source {self.name!r} exceeded "
                        f"timeout_s={self.timeout_s}; truncating"
                    )
                    break
                out.append(rec)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"application source {self.name!r} raised: {exc!r}"
            )
        return out


# ---------------------------------------------------------------------------
# Registry Uninstall source
# ---------------------------------------------------------------------------

# Two well-known hives; we walk both because per-user installs live
# in HKCU even on shared machines.
_UNINSTALL_PATHS: Tuple[Tuple[int, str], ...] = (
    # (hkey, subkey)
    (0x80000002, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),  # HKLM
    (0x80000001, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),  # HKCU
    (0x80000002, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
    (0x80000001, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
)


class RegistryUninstallSource(ApplicationSource):
    """Walk the standard Windows Uninstall registry keys.

    Each subkey is one installed program.  We extract ``DisplayName``,
    ``DisplayIcon``, ``InstallLocation``, and ``Publisher``.  When
    ``DisplayIcon`` is missing but ``InstallLocation`` is present,
    we look for a single ``.exe`` directly inside that location.
    """

    name = "registry"
    confidence = 0.9

    def __init__(self) -> None:
        super().__init__(name=self.name, confidence=self.confidence, timeout_s=8.0)
        self._winreg = None
        try:
            import winreg  # type: ignore
            self._winreg = winreg
        except Exception:  # noqa: BLE001
            logger.debug("RegistryUninstallSource: winreg unavailable")

    def scan(self) -> Iterator[ApplicationRecord]:
        if self._winreg is None:
            return
        winreg = self._winreg
        seen_keys: set = set()
        for hive, sub in _UNINSTALL_PATHS:
            try:
                with winreg.OpenKey(hive, sub) as parent:
                    idx = 0
                    while True:
                        try:
                            child_name = winreg.EnumKey(parent, idx)
                        except OSError:
                            break
                        idx += 1
                        if child_name in seen_keys:
                            continue
                        seen_keys.add(child_name)
                        try:
                            with winreg.OpenKey(parent, child_name) as child:
                                rec = self._record_from_key(child)
                                if rec is not None:
                                    yield rec
                        except OSError:
                            continue
            except OSError:
                continue

    def _record_from_key(self, key: Any) -> Optional[ApplicationRecord]:
        winreg = self._winreg
        if winreg is None:
            return None
        try:
            display_name, _ = winreg.QueryValueEx(key, "DisplayName")
        except OSError:
            return None
        if not display_name or not str(display_name).strip():
            return None
        display_name = str(display_name).strip()
        if display_name.lower() in ("update", "security update"):
            return None
        install_location: Optional[str] = None
        try:
            install_location, _ = winreg.QueryValueEx(key, "InstallLocation")
            install_location = str(install_location).strip() or None
        except OSError:
            install_location = None
        icon: Optional[str] = None
        try:
            icon, _ = winreg.QueryValueEx(key, "DisplayIcon")
            icon = str(icon).strip() or None
        except OSError:
            icon = None
        publisher: Optional[str] = None
        try:
            publisher, _ = winreg.QueryValueEx(key, "Publisher")
            publisher = str(publisher).strip() or None
        except OSError:
            publisher = None
        # Resolve an executable: prefer DisplayIcon; fall back to
        # ``<InstallLocation>\\<one>.exe``.
        executable: Optional[str] = None
        executable_path: Optional[str] = None
        if icon:
            executable = _executable_from_icon(icon)
            if executable and os.path.isabs(icon.split(",", 1)[0].strip('"')):
                executable_path = icon.split(",", 1)[0].strip('"')
        if executable is None and install_location:
            try:
                for entry in os.listdir(install_location):
                    if entry.lower().endswith(".exe"):
                        executable = entry
                        executable_path = os.path.join(install_location, entry)
                        break
            except OSError:
                pass
        if executable is None:
            return None
        # Some DisplayIcons point at install_location\uninst.exe; we
        # try to pick the more plausible launch target by checking
        # for a sibling with the display name.
        if (
            executable_path
            and install_location
            and os.path.basename(executable_path).lower() == "uninst.exe"
        ):
            try:
                for entry in os.listdir(install_location):
                    low = entry.lower()
                    if low.endswith(".exe") and low != "uninst.exe":
                        executable = entry
                        executable_path = os.path.join(install_location, entry)
                        break
            except OSError:
                pass
        return ApplicationRecord(
            display_name=display_name,
            normalized_name=normalize_name(display_name),
            executable=executable,
            executable_path=executable_path,
            launch_command=executable_path or executable,
            source=self.name,
            installed=True,
            confidence=self.confidence,
            metadata={"publisher": publisher} if publisher else {},
        )


# ---------------------------------------------------------------------------
# App Paths source
# ---------------------------------------------------------------------------

_APP_PATHS_KEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"


class AppPathsSource(ApplicationSource):
    """Walk ``HKLM\\...\\App Paths\\<exe>``.

    Each subkey is one executable the Windows Run dialog knows about.
    The ``(Default)`` value is the full path to the executable.
    """

    name = "app_paths"
    confidence = 0.95

    def __init__(self) -> None:
        super().__init__(name=self.name, confidence=self.confidence, timeout_s=4.0)
        self._winreg = None
        try:
            import winreg  # type: ignore
            self._winreg = winreg
        except Exception:  # noqa: BLE001
            logger.debug("AppPathsSource: winreg unavailable")

    def scan(self) -> Iterator[ApplicationRecord]:
        if self._winreg is None:
            return
        winreg = self._winreg
        try:
            with winreg.OpenKey(0x80000002, _APP_PATHS_KEY) as parent:
                idx = 0
                while True:
                    try:
                        child = winreg.EnumKey(parent, idx)
                    except OSError:
                        break
                    idx += 1
                    try:
                        with winreg.OpenKey(parent, child) as sub:
                            try:
                                path_val, _ = winreg.QueryValueEx(sub, None)
                            except OSError:
                                continue
                            path_val = str(path_val).strip().strip('"')
                            if not path_val or not os.path.isabs(path_val):
                                continue
                            exe = _executable_name(path_val)
                            if not exe.lower().endswith(".exe"):
                                continue
                            display = os.path.splitext(exe)[0]
                            yield ApplicationRecord(
                                display_name=display,
                                normalized_name=normalize_name(display),
                                executable=exe,
                                executable_path=path_val,
                                launch_command=path_val,
                                source=self.name,
                                installed=os.path.isfile(path_val),
                                confidence=self.confidence,
                            )
                    except OSError:
                        continue
        except OSError:
            return


# ---------------------------------------------------------------------------
# Start Menu source
# ---------------------------------------------------------------------------

_START_MENU_ROOTS: Tuple[str, ...] = (
    # Per-user Start Menu (highest signal)
    os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows", "Start Menu", "Programs"),
    # Per-machine Start Menu
    os.path.join(os.environ.get("ProgramData", ""), "Microsoft", "Windows", "Start Menu", "Programs"),
)


class StartMenuSource(ApplicationSource):
    """Walk the Start Menu directories for ``.lnk`` shortcuts.

    Resolves ``.lnk`` files via ``pywin32`` when available; otherwise
    falls back to the shortcut filename as the application name.
    The walker is bounded to the Start Menu roots — it does **not**
    recurse into arbitrary user directories.
    """

    name = "start_menu"
    confidence = 0.7

    def __init__(self) -> None:
        super().__init__(name=self.name, confidence=self.confidence, timeout_s=4.0)
        self._shell = None
        try:
            import win32com.client  # type: ignore  # noqa: F401
            self._shell = win32com.client.Dispatch("WScript.Shell")
        except Exception:  # noqa: BLE001
            self._shell = None

    def scan(self) -> Iterator[ApplicationRecord]:
        for root in _START_MENU_ROOTS:
            if not root or not os.path.isdir(root):
                continue
            for dirpath, _dirnames, filenames in os.walk(root):
                for fname in filenames:
                    if not fname.lower().endswith((".lnk", ".url")):
                        continue
                    full = os.path.join(dirpath, fname)
                    target_path: Optional[str] = None
                    display_name = os.path.splitext(fname)[0]
                    if self._shell is not None and fname.lower().endswith(".lnk"):
                        try:
                            shortcut = self._shell.CreateShortCut(full)
                            target_path = getattr(shortcut, "Targetpath", None)
                        except Exception:  # noqa: BLE001
                            target_path = None
                    if target_path and os.path.isabs(target_path):
                        exe = _executable_name(target_path)
                        if exe.lower().endswith(".exe"):
                            yield ApplicationRecord(
                                display_name=display_name,
                                normalized_name=normalize_name(display_name),
                                executable=exe,
                                executable_path=target_path,
                                launch_command=target_path,
                                source=self.name,
                                installed=os.path.isfile(target_path),
                                confidence=self.confidence,
                            )
                    else:
                        # No pywin32 or .url shortcut; produce a name
                        # only record.  The resolver will still match
                        # it if the user uses the friendly name, but
                        # launch will fall back to PATH.
                        yield ApplicationRecord(
                            display_name=display_name,
                            normalized_name=normalize_name(display_name),
                            executable=display_name + ".exe",
                            executable_path=None,
                            launch_command=display_name + ".exe",
                            source=self.name,
                            installed=False,
                            confidence=0.3,
                            metadata={"shortcut": full, "unresolved": True},
                        )


# ---------------------------------------------------------------------------
# PATH source
# ---------------------------------------------------------------------------

class PathSource(ApplicationSource):
    """List executables in each ``PATH`` directory.

    Bounded to the directories in the ``PATH`` environment variable,
    first level only.  This is the lowest-confidence source; the
    catalog treats it as a fallback when nothing better is available.
    """

    name = "path"
    confidence = 0.5

    def __init__(self) -> None:
        super().__init__(name=self.name, confidence=self.confidence, timeout_s=4.0)
        self._seen: set = set()

    def scan(self) -> Iterator[ApplicationRecord]:
        path_env = os.environ.get("PATH", "") or os.environ.get("Path", "")
        if not path_env:
            return
        for d in path_env.split(os.pathsep):
            d = d.strip()
            if not d or not os.path.isdir(d):
                continue
            try:
                for entry in os.listdir(d):
                    if not entry.lower().endswith(".exe"):
                        continue
                    key = entry.lower()
                    if key in self._seen:
                        continue
                    self._seen.add(key)
                    full = os.path.join(d, entry)
                    if not os.path.isfile(full):
                        continue
                    display = os.path.splitext(entry)[0]
                    yield ApplicationRecord(
                        display_name=display,
                        normalized_name=normalize_name(display),
                        executable=entry,
                        executable_path=full,
                        launch_command=full,
                        source=self.name,
                        installed=True,
                        confidence=self.confidence,
                    )
            except OSError:
                continue


# ---------------------------------------------------------------------------
# Process source
# ---------------------------------------------------------------------------

class ProcessSource(ApplicationSource):
    """Currently running processes, as an :class:`ApplicationRecord`.

    This is the highest-confidence source because we *know* the
    binary is on disk and launchable right now.  It does not, on its
    own, prove that the application is "installed" in the durable
    sense; the catalog only uses it as a confidence booster for
    records that already exist.
    """

    name = "process"
    confidence = 1.0

    def __init__(self) -> None:
        super().__init__(name=self.name, confidence=self.confidence, timeout_s=2.0)

    def scan(self) -> Iterator[ApplicationRecord]:
        try:
            import psutil
        except Exception:  # noqa: BLE001
            return
        seen: set = set()
        for proc in psutil.process_iter(["name", "exe"]):
            try:
                info = proc.info
            except Exception:  # noqa: BLE001
                continue
            name = (info.get("name") or "").strip()
            if not name or not name.lower().endswith(".exe"):
                continue
            key = name.lower()
            if key in seen:
                continue
            seen.add(key)
            exe_path = info.get("exe")
            display = os.path.splitext(name)[0]
            yield ApplicationRecord(
                display_name=display,
                normalized_name=normalize_name(display),
                executable=name,
                executable_path=exe_path if exe_path else None,
                launch_command=exe_path or name,
                source=self.name,
                installed=True,
                confidence=self.confidence,
                metadata={"running_pid_count": 1},
            )


# ---------------------------------------------------------------------------
# Default source list
# ---------------------------------------------------------------------------

def default_sources() -> List[ApplicationSource]:
    """The ordered list of sources used at boot.

    Order matters: the catalog merges later sources into earlier ones,
    so a Registry record (high confidence, has DisplayName) is
    enriched by a Process record (high confidence, has the running
    path) and supplemented by PATH hits for things the Registry
    doesn't enumerate (e.g. portable tools).
    """
    return [
        RegistryUninstallSource(),
        AppPathsSource(),
        StartMenuSource(),
        PathSource(),
        ProcessSource(),
    ]
