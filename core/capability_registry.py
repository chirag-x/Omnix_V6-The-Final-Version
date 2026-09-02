"""
Omnix V6 — CapabilityRegistry.

The closed set of capabilities the engine knows about.  R-21 / AD-21:
the registry owns the truth about what the brain *can* invoke; the
brain cannot invent a new operation by sending a name that is not
registered.

Registry responsibilities (Phase 1):

    * register / unregister a :class:`Capability` (with version
      conflict detection)
    * lookup by name → spec + implementation
    * list / filter (by tag, by version, by requires)
    * detect missing required services / capabilities (used by
      ``check_availability``)

The registry does **not** execute; that is the router's job.
"""

from __future__ import annotations

import threading
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .capability import Capability, CapabilitySpec
from .errors import CapabilityError, ConfigurationError


class CapabilityRegistry:
    """Thread-safe registry of capabilities.

    Storage is a dict keyed by ``(name, version)`` so multiple versions
    of the same capability can coexist (R-15: upgrades are versioned).
    """

    def __init__(self) -> None:
        self._by_name: Dict[str, Capability] = {}
        self._by_versioned_key: Dict[Tuple[str, str], Capability] = {}
        self._lock = threading.RLock()

    # ====================================================== registration
    def register(self, capability: Capability, *, replace: bool = False) -> None:
        """Register ``capability`` keyed by its ``spec.name``.

        Raises :class:`ConfigurationError` if a capability with the
        same ``(name, version)`` is already registered and
        ``replace=False`` (the default).
        """
        if not isinstance(capability, Capability):
            raise ConfigurationError(
                "Object does not satisfy the Capability protocol",
                code="CAPABILITY_PROTOCOL",
                context={"type": type(capability).__name__},
            )
        spec = capability.spec
        if not spec.name:
            raise ConfigurationError(
                "Capability spec has empty name",
                code="CAPABILITY_NAME_EMPTY",
            )
        key = (spec.name, spec.version)
        with self._lock:
            if not replace and spec.name in self._by_name:
                existing = self._by_name[spec.name]
                if existing.spec.version == spec.version:
                    raise ConfigurationError(
                        f"Capability {spec.name!r} v{spec.version} already registered",
                        code="CAPABILITY_DUPLICATE",
                        context={"name": spec.name, "version": spec.version},
                    )
            self._by_name[spec.name] = capability
            self._by_versioned_key[key] = capability

    def unregister(self, name: str) -> bool:
        """Remove every version of ``name``.  Returns ``True`` if removed."""
        with self._lock:
            if name not in self._by_name:
                return False
            del self._by_name[name]
            for k in [k for k in self._by_versioned_key if k[0] == name]:
                self._by_versioned_key.pop(k, None)
            return True

    # ============================================================ lookup
    def get(self, name: str) -> Optional[Capability]:
        with self._lock:
            return self._by_name.get(name)

    def get_versioned(self, name: str, version: str) -> Optional[Capability]:
        with self._lock:
            return self._by_versioned_key.get((name, version))

    def has(self, name: str) -> bool:
        with self._lock:
            return name in self._by_name

    # ====================================================== enumeration
    def list_names(self) -> List[str]:
        with self._lock:
            return sorted(self._by_name)

    def list_specs(self) -> List[CapabilitySpec]:
        with self._lock:
            return [c.spec for c in self._by_name.values()]

    def by_tag(self, tag: str) -> List[Capability]:
        with self._lock:
            return [c for c in self._by_name.values() if tag in c.spec.tags]

    def by_requires_service(self, service_name: str) -> List[Capability]:
        with self._lock:
            return [
                c for c in self._by_name.values()
                if service_name in c.spec.requires_services
            ]

    # ==================================================== availability
    def check_availability(
        self,
        name: str,
        *,
        available_services: Optional[Iterable[str]] = None,
        available_capabilities: Optional[Iterable[str]] = None,
    ) -> Tuple[bool, str]:
        """Return ``(ok, reason)`` for whether ``name`` can run.

        ``available_services`` is the set of currently-READY service
        names; same for ``available_capabilities``.  Both default to
        empty if not supplied (so the caller must opt in).
        """
        with self._lock:
            cap = self._by_name.get(name)
        if cap is None:
            return False, f"unknown capability: {name!r}"
        for req in cap.spec.requires_services:
            if available_services is not None and req not in set(available_services):
                return False, f"required service missing: {req!r}"
        for req in cap.spec.requires_capabilities:
            if available_capabilities is not None and req not in set(available_capabilities):
                return False, f"required capability missing: {req!r}"
        try:
            live = cap.is_available()
        except Exception as exc:  # noqa: BLE001
            return False, f"availability probe raised: {exc!r}"
        if not live:
            return False, "capability reports unavailable"
        return True, ""

    # ========================================================== repr
    def __len__(self) -> int:
        with self._lock:
            return len(self._by_name)

    def __contains__(self, name: str) -> bool:
        return self.has(name)

    def __repr__(self) -> str:
        return f"CapabilityRegistry(size={len(self._by_name)})"

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "type": "CapabilityRegistry",
                "size": len(self._by_name),
                "names": sorted(self._by_name),
            }
