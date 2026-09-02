"""
Omnix V6 — System 8: dependency DAG validation for :class:`Plan`.

The existing :class:`PlanExecutor` already enforces dependency
ordering at *execution* time (it walks the plan in topological
order and blocks any step whose ``depends_on`` is not yet
``COMPLETED``).  This module adds a *static* validator that
catches the four classes of broken dependency before the Agent
even dispatches the first step:

  1. **Unknown dependency** — a step lists a ``depends_on`` step
     that does not exist in the plan.
  2. **Self-dependency**    — a step depends on itself.
  3. **Cycle**              — the dependency graph has at least
     one cycle, so no topological order exists.
  4. **Duplicate step id**  — two steps share the same ``step_id``,
     which would silently collapse their outputs.

The validator is a *pure* function: it takes a :class:`Plan` (or
a sequence of :class:`PlanStep`) and returns a structured
:class:`DAGValidationResult`.  It does not mutate the plan, does
not call any subsystem, and is safe to invoke from any layer
(planner, agent, test).

Architectural rules honored here:

- R-8   — every status is a typed enum, never a bare bool.
- R-10  — results are ``frozen=True``.
- R-13  — the validator does not invent capability names; it
          only inspects ``depends_on`` graph topology.
- R-21  — the validator never calls a Capability.
- R-23  — the validator does not mutate the plan.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Sequence, Set, Tuple

from .models import Plan, PlanStep


# ===========================================================================
# DAGIssue — one structured defect
# ===========================================================================

class DAGIssueKind(str, Enum):
    """The class of dependency-DAG defect."""

    UNKNOWN_DEPENDENCY = "unknown_dependency"
    SELF_DEPENDENCY = "self_dependency"
    CYCLE = "cycle"
    DUPLICATE_STEP_ID = "duplicate_step_id"
    EMPTY_DEPENDS_ON = "empty_depends_on"   # informational, not a defect


@dataclass(frozen=True)
class DAGIssue:
    """A single defect found in the plan's dependency graph."""

    kind: DAGIssueKind
    step_id: str = ""
    other_step_id: str = ""
    cycle_path: Tuple[str, ...] = ()
    message: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": "DAGIssue",
            "kind": self.kind.value,
            "step_id": self.step_id,
            "other_step_id": self.other_step_id,
            "cycle_path": list(self.cycle_path),
            "message": self.message,
        }


# ===========================================================================
# DAGValidationResult
# ===========================================================================

@dataclass(frozen=True)
class DAGValidationResult:
    """The outcome of validating a plan's dependency graph.

    ``ok`` is ``True`` only when *no* defects were found.  The
    cycle field is always present; it is ``()`` when no cycle
    exists.  The ``topological_order`` is a stable list of step
    ids in dependency order; if the plan is cyclic it is ``()``.
    """

    ok: bool
    issues: Tuple[DAGIssue, ...] = ()
    topological_order: Tuple[str, ...] = ()
    step_count: int = 0

    @property
    def issue_count(self) -> int:
        return len(self.issues)

    def issues_of(self, kind: DAGIssueKind) -> List[DAGIssue]:
        return [i for i in self.issues if i.kind is kind]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": "DAGValidationResult",
            "ok": self.ok,
            "issue_count": self.issue_count,
            "step_count": self.step_count,
            "topological_order": list(self.topological_order),
            "issues": [i.to_dict() for i in self.issues],
        }


# ===========================================================================
# Public API
# ===========================================================================

def validate_plan(plan: Plan) -> DAGValidationResult:
    """Validate the dependency graph of a :class:`Plan`.

    The validator runs the four checks in order:

      1. Duplicate step ids.
      2. Unknown / self / empty ``depends_on`` entries.
      3. Cycle detection (Kahn's algorithm + DFS fallback).
      4. Topological order reconstruction.

    The returned :class:`DAGValidationResult` always carries the
    topological order (when the graph is acyclic) so the Agent can
    skip recomputing it.
    """
    if plan is None:
        return DAGValidationResult(
            ok=False,
            issues=(DAGIssue(
                kind=DAGIssueKind.UNKNOWN_DEPENDENCY,
                message="plan is None",
            ),),
        )
    steps: Sequence[PlanStep] = tuple(plan.steps or ())
    return validate_steps(steps)


def validate_steps(steps: Sequence[PlanStep]) -> DAGValidationResult:
    """Validate the dependency graph of a sequence of :class:`PlanStep`.

    Useful when the plan object is not available (e.g. inside the
    planner before a :class:`Plan` is constructed).
    """
    issues: List[DAGIssue] = []

    # 1. Duplicate step ids.
    seen: Set[str] = set()
    seen_first: Dict[str, str] = {}  # for nicer error messages
    for idx, step in enumerate(steps):
        sid = getattr(step, "step_id", "") or f"<idx{idx}>"
        if sid in seen:
            issues.append(
                DAGIssue(
                    kind=DAGIssueKind.DUPLICATE_STEP_ID,
                    step_id=sid,
                    other_step_id=seen_first.get(sid, ""),
                    message=f"step_id {sid!r} appears more than once",
                )
            )
        else:
            seen.add(sid)
            seen_first[sid] = sid

    # 2. Unknown / self / empty ``depends_on`` entries.
    for step in steps:
        sid = getattr(step, "step_id", "") or ""
        deps = tuple(getattr(step, "depends_on", ()) or ())
        if not deps:
            continue
        if any(not isinstance(d, str) or not d for d in deps):
            issues.append(
                DAGIssue(
                    kind=DAGIssueKind.EMPTY_DEPENDS_ON,
                    step_id=sid,
                    message=f"step {sid!r} has an empty/None depends_on entry",
                )
            )
        if sid in deps:
            issues.append(
                DAGIssue(
                    kind=DAGIssueKind.SELF_DEPENDENCY,
                    step_id=sid,
                    other_step_id=sid,
                    message=f"step {sid!r} depends on itself",
                )
            )
        for d in deps:
            if not isinstance(d, str) or not d:
                continue
            if d not in seen:
                issues.append(
                    DAGIssue(
                        kind=DAGIssueKind.UNKNOWN_DEPENDENCY,
                        step_id=sid,
                        other_step_id=d,
                        message=(
                            f"step {sid!r} depends on unknown step {d!r}"
                        ),
                    )
                )

    # 3. Cycle detection — Kahn's algorithm.
    in_degree: Dict[str, int] = {sid: 0 for sid in seen}
    adj: Dict[str, List[str]] = {sid: [] for sid in seen}
    for step in steps:
        sid = getattr(step, "step_id", "") or ""
        if sid not in adj:
            continue
        for d in getattr(step, "depends_on", ()) or ():
            if not isinstance(d, str) or not d:
                continue
            if d in adj:
                # Edge: d -> sid  (sid depends on d, so d must come first)
                adj[d].append(sid)
                in_degree[sid] += 1

    # Kahn: queue of nodes with no incoming edges.
    ready: List[str] = sorted(
        sid for sid, deg in in_degree.items() if deg == 0
    )
    topo: List[str] = []
    while ready:
        ready.sort()  # stable order
        n = ready.pop(0)
        topo.append(n)
        for m in adj.get(n, ()):
            in_degree[m] -= 1
            if in_degree[m] == 0:
                ready.append(m)

    cycle = len(topo) != len(seen)
    if cycle:
        # Reconstruct a concrete cycle path for the message.
        cycle_path = _find_cycle_path(adj, set(seen) - set(topo))
        issues.append(
            DAGIssue(
                kind=DAGIssueKind.CYCLE,
                cycle_path=tuple(cycle_path),
                message=(
                    f"dependency graph has a cycle: "
                    f"{' -> '.join(cycle_path) or 'unresolvable'}"
                ),
            )
        )

    return DAGValidationResult(
        ok=not any(
            i.kind in (
                DAGIssueKind.UNKNOWN_DEPENDENCY,
                DAGIssueKind.SELF_DEPENDENCY,
                DAGIssueKind.CYCLE,
                DAGIssueKind.DUPLICATE_STEP_ID,
            )
            for i in issues
        ),
        issues=tuple(issues),
        topological_order=tuple(topo) if not cycle else (),
        step_count=len(seen),
    )


# ===========================================================================
# Cycle-path reconstruction (DFS)
# ===========================================================================

def _find_cycle_path(
    adj: Dict[str, List[str]],
    candidates: Set[str],
) -> List[str]:
    """Return one concrete cycle path through ``candidates``.

    Uses iterative DFS so we do not blow the recursion limit on
    large plans.  Returns the empty list when no cycle is found.
    """
    WHITE, GRAY, BLACK = 0, 1, 2
    color: Dict[str, int] = {n: WHITE for n in candidates}
    parent: Dict[str, str] = {}

    for start in sorted(candidates):
        if color[start] != WHITE:
            continue
        # Iterative DFS from ``start``.
        stack: List[Tuple[str, int]] = [(start, 0)]
        color[start] = GRAY
        while stack:
            node, i = stack[-1]
            children = adj.get(node, ())
            if i >= len(children):
                color[node] = BLACK
                stack.pop()
                continue
            stack[-1] = (node, i + 1)
            nxt = children[i]
            if nxt not in color:
                # Edge points outside the unresolved set; skip.
                continue
            if color[nxt] == GRAY:
                # Found a back-edge: reconstruct the cycle.
                path = [nxt, node]
                while node in parent and node != nxt:
                    node = parent[node]
                    path.append(node)
                path.reverse()
                return path
            if color[nxt] == WHITE:
                color[nxt] = GRAY
                parent[nxt] = node
                stack.append((nxt, 0))
    return []


__all__ = [
    "DAGIssueKind",
    "DAGIssue",
    "DAGValidationResult",
    "validate_plan",
    "validate_steps",
]
