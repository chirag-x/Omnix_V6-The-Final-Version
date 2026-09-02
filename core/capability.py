"""
Omnix V6 — Capability contracts.

A *capability* is a named, parametric, validated side-effecting
operation the engine can dispatch.  Capabilities are the engine's
only legitimate way to touch the world (R-21 / AD-21): the Brain
cannot invent a new operation; it can only call a capability that
is registered in :class:`CapabilityRegistry`.

This module defines:

    * :class:`CapabilityParameter`  — typed parameter spec (name, type,
                                       required, default, validator)
    * :class:`CapabilitySpec`       — the immutable registration record
    * :class:`Capability` (Protocol) — the executable interface a
                                       capability must implement

The router (:mod:`core.capability_router`) consumes these.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, Mapping, Optional, Protocol, runtime_checkable

from .errors import ValidationError
from .results import (
    ActionResult,
    CapabilityResult,
    CapabilityStatus,
    VerificationResult,
)


# ---------------------------------------------------------------------------
# Parameter spec
# ---------------------------------------------------------------------------

class ParamType(str, Enum):
    STRING = "string"
    INTEGER = "integer"
    FLOAT = "float"
    BOOLEAN = "boolean"
    PATH = "path"
    ENUM = "enum"
    ANY = "any"


@dataclass(frozen=True)
class CapabilityParameter:
    """One named parameter a capability accepts."""

    name: str
    type: ParamType
    required: bool = True
    default: Any = None
    description: str = ""
    allowed_values: tuple = ()          # only consulted when type is ENUM
    min_value: Optional[float] = None
    max_value: Optional[float] = None

    def coerce(self, value: Any) -> Any:
        """Coerce ``value`` to the declared type or raise ``ValidationError``."""
        if value is None:
            if self.required and self.default is None:
                raise ValidationError(
                    f"Missing required parameter: {self.name!r}",
                    code="CAPABILITY_PARAM_MISSING",
                    context={"parameter": self.name},
                )
            return self.default
        if self.type is ParamType.STRING:
            return str(value)
        if self.type is ParamType.INTEGER:
            try:
                v = int(value)
            except (TypeError, ValueError) as exc:
                raise ValidationError(
                    f"Parameter {self.name!r} must be int",
                    code="CAPABILITY_PARAM_TYPE",
                    context={"parameter": self.name, "got": repr(value)},
                ) from exc
            if self.min_value is not None and v < self.min_value:
                raise ValidationError(
                    f"Parameter {self.name!r} below min",
                    code="CAPABILITY_PARAM_RANGE",
                    context={"parameter": self.name, "value": v, "min": self.min_value},
                )
            if self.max_value is not None and v > self.max_value:
                raise ValidationError(
                    f"Parameter {self.name!r} above max",
                    code="CAPABILITY_PARAM_RANGE",
                    context={"parameter": self.name, "value": v, "max": self.max_value},
                )
            return v
        if self.type is ParamType.FLOAT:
            try:
                v = float(value)
            except (TypeError, ValueError) as exc:
                raise ValidationError(
                    f"Parameter {self.name!r} must be float",
                    code="CAPABILITY_PARAM_TYPE",
                    context={"parameter": self.name, "got": repr(value)},
                ) from exc
            if self.min_value is not None and v < self.min_value:
                raise ValidationError(
                    f"Parameter {self.name!r} below min",
                    code="CAPABILITY_PARAM_RANGE",
                    context={"parameter": self.name, "value": v, "min": self.min_value},
                )
            if self.max_value is not None and v > self.max_value:
                raise ValidationError(
                    f"Parameter {self.name!r} above max",
                    code="CAPABILITY_PARAM_RANGE",
                    context={"parameter": self.name, "value": v, "max": self.max_value},
                )
            return v
        if self.type is ParamType.BOOLEAN:
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "on")
            return bool(value)
        if self.type is ParamType.PATH:
            import os
            return os.fspath(value)
        if self.type is ParamType.ENUM:
            if value not in self.allowed_values:
                raise ValidationError(
                    f"Parameter {self.name!r} not in allowed values",
                    code="CAPABILITY_PARAM_ENUM",
                    context={
                        "parameter": self.name,
                        "got": repr(value),
                        "allowed": list(self.allowed_values),
                    },
                )
            return value
        # ANY
        return value


# ---------------------------------------------------------------------------
# Spec
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CapabilitySpec:
    """The immutable registration record for one capability.

    A spec is the *declaration*; the ``Capability`` protocol (or a
    callable) is the *implementation*.
    """

    name: str
    version: str
    description: str
    parameters: tuple = ()
    requires_capabilities: tuple = ()
    requires_services: tuple = ()
    dangerous: bool = False
    tags: tuple = ()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "parameters": [
                {
                    "name": p.name,
                    "type": p.type.value,
                    "required": p.required,
                    "default": p.default,
                    "description": p.description,
                }
                for p in self.parameters
            ],
            "requires_capabilities": list(self.requires_capabilities),
            "requires_services": list(self.requires_services),
            "dangerous": self.dangerous,
            "tags": list(self.tags),
        }


# ---------------------------------------------------------------------------
# Capability executable
# ---------------------------------------------------------------------------

@runtime_checkable
class Capability(Protocol):
    """The contract every registered capability must satisfy.

    A capability is *pure* with respect to dispatch: it does not know
    about the router, the engine, or the brain.  It receives a
    dict of validated parameters and returns a :class:`CapabilityResult`.
    """

    spec: CapabilitySpec

    def is_available(self) -> bool:
        """Return ``True`` if the capability can run right now.

        For example, a ``close_app`` capability is unavailable if the
        target app is not running.  The router consults this in addition
        to the registry.
        """
        ...

    def execute(self, params: Mapping[str, Any]) -> CapabilityResult:
        """Run the capability and return a structured :class:`CapabilityResult`.

        Implementations MUST NOT raise on routine failure; they should
        return a :class:`CapabilityResult` with ``status=FAILED`` and
        an ``error``.  Raising is reserved for "I lost the ability to
        report" situations and the router wraps those in
        :class:`CapabilityError`.
        """
        ...


# ---------------------------------------------------------------------------
# Helper for callable-based capabilities
# ---------------------------------------------------------------------------

class CallableCapability:
    """Wrap a plain ``Callable[[Mapping[str, Any]], CapabilityResult]`` as a
    :class:`Capability`.

    Lets skill authors write a single function and register it
    directly, without subclassing anything.
    """

    spec: CapabilitySpec

    def __init__(
        self,
        spec: CapabilitySpec,
        fn: Callable[[Mapping[str, Any]], CapabilityResult],
        *,
        availability_fn: Optional[Callable[[], bool]] = None,
    ) -> None:
        self.spec = spec
        self._fn = fn
        self._availability_fn = availability_fn

    def is_available(self) -> bool:
        if self._availability_fn is None:
            return True
        try:
            return bool(self._availability_fn())
        except Exception:  # noqa: BLE001
            return False

    def execute(self, params: Mapping[str, Any]) -> CapabilityResult:
        return self._fn(params)


# ---------------------------------------------------------------------------
# Validation helpers (used by the router)
# ---------------------------------------------------------------------------

def _iter_parameters(spec: CapabilitySpec):
    """Yield :class:`CapabilityParameter` entries from ``spec.parameters``.

    ``CapabilitySpec.parameters`` may be expressed two ways:

    * a tuple/list of :class:`CapabilityParameter` (the canonical form);
    * a dict mapping ``name`` -> :class:`CapabilityParameter` (a
      convenient shorthand used by capability authors who want to
      reference a parameter by name).

    This helper normalises both to an iterable of
    :class:`CapabilityParameter`.
    """
    params = spec.parameters
    if isinstance(params, Mapping):
        for name, p in params.items():
            if p is None:
                continue
            if hasattr(p, "name") and getattr(p, "name", None):
                yield p
            else:
                # Best-effort fallback: if the value is a plain
                # ``CapabilityParameter``-like object that lost its
                # name (e.g. constructed via dict copy), attach the
                # dict key so the router still finds it.
                if hasattr(p, "name"):
                    try:
                        object.__setattr__(p, "name", name)
                    except Exception:  # noqa: BLE001
                        pass
                yield p
        return
    for p in params:
        yield p


def coerce_parameters(
    spec: CapabilitySpec,
    raw: Mapping[str, Any],
) -> Dict[str, Any]:
    """Coerce ``raw`` against ``spec.parameters``; raise ``ValidationError``."""
    out: Dict[str, Any] = {}
    params_list = list(_iter_parameters(spec))
    unknown = set(raw) - {p.name for p in params_list}
    if unknown:
        raise ValidationError(
            f"Unknown parameters for capability {spec.name!r}: {sorted(unknown)}",
            code="CAPABILITY_PARAM_UNKNOWN",
            context={"capability": spec.name, "unknown": sorted(unknown)},
        )
    for p in params_list:
        out[p.name] = p.coerce(raw.get(p.name, p.default))
    return out
