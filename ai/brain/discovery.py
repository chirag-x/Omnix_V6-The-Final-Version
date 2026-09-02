"""
Omnix V6 — Brain capability discovery (Phase 5C+5D).

The Brain / Planner must reference capabilities that *actually exist* in
the canonical :class:`core.capability_registry.CapabilityRegistry`.
It must NEVER invent a new operation.  The closed capability set is
the only valid action surface (R-21 / AD-21).

This module is a *read-only* projection over the registry: it produces
a list of lightweight, planner-friendly :class:`CapabilitySummary`
records.  The summary is small enough to be embedded in an LLM
prompt, and large enough that the model can pick the right capability
for a goal.

Isolation rule (mirrors ``ai.provider``):
    This module MUST NOT import or call any of:

    * :mod:`subprocess`
    * :mod:`pyautogui`
    * :mod:`win32gui` / :mod:`win32api`
    * :mod:`ctypes`
    * :mod:`core.capability_router`
    * :mod:`core.omnix_engine`
    * any V6 *Windows service* (e.g. ``system.windows.*``,
      ``system.applications.*``)

The discovery helper produces *data only*.  The Brain and the Planner
must use it; they must never import or call a capability directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from core.capability import CapabilitySpec
from core.capability_registry import CapabilityRegistry


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

# Maximum number of bytes the discovery summary will produce for a
# single capability.  This is a soft bound the prompt-builder uses
# when serialising the summary; the LLM never sees anything larger.
MAX_SUMMARY_BYTES_PER_CAPABILITY: int = 2_048

# Maximum number of bytes the *total* summary may produce.  This is a
# guard against a registry explosion sending tens of thousands of
# capabilities into the prompt.  The planner can override it via
# ``summarize_for_prompt(..., max_total_bytes=...)`` if the registered
# set is genuinely large.
MAX_TOTAL_SUMMARY_BYTES: int = 64 * 1024


@dataclass(frozen=True)
class CapabilitySummary:
    """A planner-friendly projection of a :class:`CapabilitySpec`.

    The summary intentionally *omits* anything that would help the LLM
    smuggle a side effect: no executable code, no service URLs, no
    internal implementation hints.  It is just enough for the model
    to:

        * know the capability exists,
        * know what it does in user terms,
        * know what parameters it accepts,
        * know whether it is dangerous.
    """

    name: str
    version: str
    description: str
    parameters: Tuple[Dict[str, Any], ...] = field(default_factory=tuple)
    tags: Tuple[str, ...] = ()
    dangerous: bool = False
    requires_capabilities: Tuple[str, ...] = ()
    requires_services: Tuple[str, ...] = ()

    def to_prompt_dict(self) -> Dict[str, Any]:
        """Return a small dict suitable for embedding in a JSON prompt.

        The dict mirrors the ``to_dict`` shape of :class:`CapabilitySpec`
        but uses *tuples* (not the original objects) for safety: the
        planner will re-validate everything against the live registry
        before producing an :class:`ActionRequest`.
        """
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "parameters": [dict(p) for p in self.parameters],
            "tags": list(self.tags),
            "dangerous": self.dangerous,
            "requires_capabilities": list(self.requires_capabilities),
            "requires_services": list(self.requires_services),
        }


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------

def _safe_tags(spec: CapabilitySpec) -> Tuple[str, ...]:
    """Return a stable, sorted tuple of tags from a spec.

    The spec stores tags as an arbitrary iterable; tests and planners
    expect a deterministic, hashable shape.
    """
    try:
        return tuple(sorted(str(t) for t in (spec.tags or ())))
    except TypeError:
        return ()


def _safe_requires(spec: CapabilitySpec, attr: str) -> Tuple[str, ...]:
    """Read a ``requires_*`` attribute and coerce it to a tuple of strings."""
    raw: Any = getattr(spec, attr, ()) or ()
    try:
        return tuple(str(x) for x in raw)
    except TypeError:
        return (str(raw),)


def _summary_from_spec(spec: CapabilitySpec) -> CapabilitySummary:
    """Project a :class:`CapabilitySpec` to a :class:`CapabilitySummary`."""
    param_dicts: List[Dict[str, Any]] = []
    for p in spec.parameters or ():
        # p is a CapabilityParameter; the public shape lives on its
        # fields.  We deliberately exclude callable defaults.
        param_dicts.append(
            {
                "name": str(p.name),
                "type": p.type.value if hasattr(p.type, "value") else str(p.type),
                "required": bool(p.required),
                "default": p.default if not callable(p.default) else None,
                "description": str(p.description or ""),
            }
        )
    return CapabilitySummary(
        name=str(spec.name),
        version=str(spec.version),
        description=str(spec.description or ""),
        parameters=tuple(param_dicts),
        tags=_safe_tags(spec),
        dangerous=bool(spec.dangerous),
        requires_capabilities=_safe_requires(spec, "requires_capabilities"),
        requires_services=_safe_requires(spec, "requires_services"),
    )


def discover_capabilities(
    registry: CapabilityRegistry,
    *,
    names: Optional[Sequence[str]] = None,
    tags: Optional[Sequence[str]] = None,
) -> List[CapabilitySummary]:
    """Return a list of :class:`CapabilitySummary` records.

    Args:
        registry: The canonical :class:`CapabilityRegistry` to inspect.
        names: If given, only summaries for these names are returned
            (in the order given).  Unknown names are silently dropped;
            the caller is expected to handle the "unknown" case with
            its own error path.  ``None`` means "every registered
            capability".
        tags: If given, only summaries whose tag set is a superset of
            at least one of these tags are returned.  The filter is
            OR-semantics, not AND.  This lets the Brain scope the
            discovery to e.g. ``{"desktop", "filesystem"}``.

    Returns:
        A new list of immutable :class:`CapabilitySummary` records,
        sorted by ``name`` for determinism.
    """
    if not isinstance(registry, CapabilityRegistry):
        raise TypeError(
            f"discover_capabilities expected a CapabilityRegistry, "
            f"got {type(registry).__name__}"
        )

    if names is not None:
        chosen: List[CapabilitySpec] = []
        for n in names:
            cap = registry.get(str(n))
            if cap is not None:
                chosen.append(cap.spec)
    else:
        chosen = list(registry.list_specs())

    if tags is not None:
        tag_set = {str(t) for t in tags}
        chosen = [s for s in chosen if tag_set.intersection(_safe_tags(s))]

    summaries = [_summary_from_spec(s) for s in chosen]
    summaries.sort(key=lambda c: c.name)
    return summaries


def summarize_for_prompt(
    summaries: Sequence[CapabilitySummary],
    *,
    max_total_bytes: int = MAX_TOTAL_SUMMARY_BYTES,
) -> List[Dict[str, Any]]:
    """Render a list of summaries as prompt-safe dicts.

    The function never raises; it truncates gracefully and stamps a
    metadata flag on the dict if the budget was exceeded so the prompt
    builder can decide what to do (e.g. switch to a smaller
    filter or split the request into multiple planner calls).
    """
    if max_total_bytes <= 0:
        raise ValueError("max_total_bytes must be positive")

    out: List[Dict[str, Any]] = []
    running = 0
    truncated = False
    for s in summaries:
        d = s.to_prompt_dict()
        # Approximate the serialised size; the real prompt builder may
        # add a header per capability, so we leave a 64-byte cushion.
        approx_size = len(str(d)) + 64
        if running + approx_size > max_total_bytes:
            truncated = True
            break
        out.append(d)
        running += approx_size

    # The truncation flag is encoded into the FIRST dict as a sidecar
    # so the caller sees it.  We do not mutate the original summary
    # (which is frozen).
    if truncated and out:
        out[0] = dict(out[0])
        out[0]["_truncated"] = True  # type: ignore[index]
    return out


# ---------------------------------------------------------------------------
# Selection helpers (used by the planner validation layer, not the LLM)
# ---------------------------------------------------------------------------

def find_capability(
    registry: CapabilityRegistry,
    name: str,
) -> Optional[CapabilitySummary]:
    """Look up a single capability by name.

    Returns ``None`` if the registry has no capability with that name.
    The summary is the same shape the planner prompt would receive;
    it is safe to compare against.
    """
    cap = registry.get(name)
    if cap is None:
        return None
    return _summary_from_spec(cap.spec)


def required_parameter_names(
    summary: CapabilitySummary,
) -> Tuple[str, ...]:
    """Return the set of parameter names that are ``required=True``."""
    return tuple(
        str(p.get("name"))
        for p in summary.parameters
        if bool(p.get("required"))
    )
