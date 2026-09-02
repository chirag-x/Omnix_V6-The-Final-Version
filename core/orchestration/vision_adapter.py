"""
Omnix V6 — Vision → executor adapter (Phase 7.2).

This module is the *only* place that turns a
:class:`vision.observations.targets.GroundedTarget` into an
:class:`core.orchestration.models.ActionRequest`.  The Agent must
not invent ``x, y`` coordinates on its own; if it needs to click
on a target, it must ground the target first and route the
resulting :class:`TargetGroundingContract` through this adapter.

Why a separate module
---------------------
The closed capability set is the only path to execution
(R-21).  The adapter knows which capability names are valid for
each pre-action target type:

  * :func:`adapt_click`         -> ``desktop.mouse.click``
  * :func:`adapt_double_click`  -> ``desktop.mouse.double_click``
  * :func:`adapt_right_click`   -> ``desktop.mouse.right_click``
  * :func:`adapt_focus`         -> (no-op shell; the planner
                                    declares ``focus`` as an
                                    observation-only step)
  * :func:`adapt_type_into`     -> ``desktop.keyboard.type`` *after*
                                    focusing the resolved target

These are *pure* adapters: they do not call the executor, do not
call the vision service, and do not inspect the screen.  They
take a :class:`TargetGroundingContract` and return an
:class:`ActionRequest` whose ``capability_name`` is in the closed
registry.

R-21 enforcement
----------------
The adapters construct an :class:`ActionRequest` directly.  The
constructor's static shell-token check (R-21) means a malformed
target cannot escape through this seam.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from loguru import logger

from core.orchestration.grounding import GroundingStatus, TargetGroundingContract
from core.orchestration.models import ActionRequest, ExpectedEffect


# The closed capability names we are allowed to dispatch through
# this adapter.  Anything else is rejected at adapter time.  This
# is the *static* half of R-21 enforcement.
_CLICK_CAPABILITY = "desktop.mouse.click"
_DOUBLE_CLICK_CAPABILITY = "desktop.mouse.double_click"
_RIGHT_CLICK_CAPABILITY = "desktop.mouse.right_click"
_MOVE_CAPABILITY = "desktop.mouse.move"
_TYPE_CAPABILITY = "desktop.keyboard.type"
_PRESS_CAPABILITY = "desktop.keyboard.press"
_HOTKEY_CAPABILITY = "desktop.keyboard.hotkey"

_KNOWN_CAPABILITIES = frozenset({
    _CLICK_CAPABILITY,
    _DOUBLE_CLICK_CAPABILITY,
    _RIGHT_CLICK_CAPABILITY,
    _MOVE_CAPABILITY,
    _TYPE_CAPABILITY,
    _PRESS_CAPABILITY,
    _HOTKEY_CAPABILITY,
})


# ----------------------------------------------------------- errors

class GroundingNotGroundableError(ValueError):
    """Raised when an adapter receives a non-GROUNDED contract.

    The Agent catches this and routes to recovery; it is never
    surfaced to the user as a free-form message.
    """


# ----------------------------------------------------------- helpers

def _validate_grounded(
    contract: TargetGroundingContract,
    *,
    what: str,
) -> None:
    """Common precondition: a contract must be GROUNDED to be adapted."""
    if contract.status is not GroundingStatus.GROUNDED:
        raise GroundingNotGroundableError(
            f"cannot adapt to {what!r}: grounding status is "
            f"{contract.status.value!r} (target={contract.target_query!r})"
        )
    if contract.center is None:
        raise GroundingNotGroundableError(
            f"cannot adapt to {what!r}: grounded contract has no center "
            f"(target={contract.target_query!r})"
        )


def _expected_effect_for_click(
    contract: TargetGroundingContract,
) -> ExpectedEffect:
    """Build a structural :class:`ExpectedEffect` for a click."""
    return ExpectedEffect(
        check_name="vision_target_clicked",
        expected={
            "source": contract.source.value if contract.source else None,
            "bbox": contract.bbox,
            "center": contract.center,
        },
        timeout_s=2.0,
        description=(
            f"click on {contract.target_query!r} at {contract.center} "
            f"(source={contract.source.value if contract.source else 'unknown'})"
        ),
    )


# ----------------------------------------------------------- adapters

@dataclass(frozen=True)
class AdaptedAction:
    """The adapter's output: an :class:`ActionRequest` plus audit info."""

    request: ActionRequest
    capability_name: str
    target_query: str
    confidence: float


def adapt_click(contract: TargetGroundingContract) -> AdaptedAction:
    """Adapt a GROUNDED contract into a left-click :class:`ActionRequest`."""
    _validate_grounded(contract, what="click")
    x, y = contract.center  # type: ignore[misc]
    request = ActionRequest(
        capability_name=_CLICK_CAPABILITY,
        parameters={"x": int(x), "y": int(y)},
        expected_effect=_expected_effect_for_click(contract),
        metadata={
            "grounding_target": contract.target_query,
            "grounding_source": (
                contract.source.value if contract.source else None
            ),
            "grounding_confidence": contract.confidence,
            "grounding_resolution_method": contract.resolution_method,
        },
    )
    return AdaptedAction(
        request=request,
        capability_name=_CLICK_CAPABILITY,
        target_query=contract.target_query,
        confidence=contract.confidence,
    )


def adapt_double_click(contract: TargetGroundingContract) -> AdaptedAction:
    """Adapt a GROUNDED contract into a double-click :class:`ActionRequest`."""
    _validate_grounded(contract, what="double_click")
    x, y = contract.center  # type: ignore[misc]
    request = ActionRequest(
        capability_name=_DOUBLE_CLICK_CAPABILITY,
        parameters={"x": int(x), "y": int(y)},
        expected_effect=_expected_effect_for_click(contract),
        metadata={
            "grounding_target": contract.target_query,
            "grounding_source": (
                contract.source.value if contract.source else None
            ),
            "grounding_confidence": contract.confidence,
            "grounding_resolution_method": contract.resolution_method,
        },
    )
    return AdaptedAction(
        request=request,
        capability_name=_DOUBLE_CLICK_CAPABILITY,
        target_query=contract.target_query,
        confidence=contract.confidence,
    )


def adapt_right_click(contract: TargetGroundingContract) -> AdaptedAction:
    """Adapt a GROUNDED contract into a right-click :class:`ActionRequest`."""
    _validate_grounded(contract, what="right_click")
    x, y = contract.center  # type: ignore[misc]
    request = ActionRequest(
        capability_name=_RIGHT_CLICK_CAPABILITY,
        parameters={"x": int(x), "y": int(y)},
        expected_effect=_expected_effect_for_click(contract),
        metadata={
            "grounding_target": contract.target_query,
            "grounding_source": (
                contract.source.value if contract.source else None
            ),
            "grounding_confidence": contract.confidence,
            "grounding_resolution_method": contract.resolution_method,
        },
    )
    return AdaptedAction(
        request=request,
        capability_name=_RIGHT_CLICK_CAPABILITY,
        target_query=contract.target_query,
        confidence=contract.confidence,
    )


def adapt_focus(contract: TargetGroundingContract) -> AdaptedAction:
    """Adapt a GROUNDED contract into a *focus* :class:`ActionRequest`.

    "Focus" in V6 means: move the mouse to the resolved center so a
    subsequent :func:`adapt_type_into` lands in the right place.
    We use ``desktop.mouse.move`` -- the cheapest capability that
    records the destination without performing a side-effect that
    would alter the UI.
    """
    _validate_grounded(contract, what="focus")
    x, y = contract.center  # type: ignore[misc]
    request = ActionRequest(
        capability_name=_MOVE_CAPABILITY,
        parameters={"x": int(x), "y": int(y)},
        expected_effect=ExpectedEffect(
            check_name="vision_target_focused",
            expected={"center": contract.center},
            timeout_s=2.0,
            description=f"focus {contract.target_query!r} at {contract.center}",
        ),
        metadata={
            "grounding_target": contract.target_query,
            "grounding_source": (
                contract.source.value if contract.source else None
            ),
            "grounding_confidence": contract.confidence,
            "grounding_resolution_method": contract.resolution_method,
            "pre_action_kind": "focus",
        },
    )
    return AdaptedAction(
        request=request,
        capability_name=_MOVE_CAPABILITY,
        target_query=contract.target_query,
        confidence=contract.confidence,
    )


def adapt_type_into(
    contract: TargetGroundingContract,
    *,
    text: str,
    focus_request: Optional[ActionRequest] = None,
) -> AdaptedAction:
    """Adapt a GROUNDED contract + text payload into a *type-into* flow.

    Returns the *type* :class:`ActionRequest` (the final
    user-visible side-effect).  If ``focus_request`` is supplied,
    the caller must dispatch it first so the keystrokes land on
    the right target.  The adapter does not chain requests
    itself -- chaining is the executor's job, not the adapter's.
    """
    _validate_grounded(contract, what="type_into")
    if not isinstance(text, str) or not text:
        raise ValueError("adapt_type_into requires non-empty 'text'")
    request = ActionRequest(
        capability_name=_TYPE_CAPABILITY,
        parameters={"text": text, "interval_s": 0.0},
        expected_effect=ExpectedEffect(
            check_name="text_typed",
            expected={"length": len(text)},
            timeout_s=2.0,
            description=f"type into {contract.target_query!r}",
        ),
        metadata={
            "grounding_target": contract.target_query,
            "grounding_source": (
                contract.source.value if contract.source else None
            ),
            "grounding_confidence": contract.confidence,
            "grounding_resolution_method": contract.resolution_method,
            "pre_action_kind": "type_into",
        },
    )
    if focus_request is not None:
        request.metadata["pre_focus_request_id"] = focus_request.request_id
    return AdaptedAction(
        request=request,
        capability_name=_TYPE_CAPABILITY,
        target_query=contract.target_query,
        confidence=contract.confidence,
    )


def adapt_pre_action(
    contract: TargetGroundingContract,
    *,
    kind: str,
    text: Optional[str] = None,
) -> AdaptedAction:
    """Dispatch to the right adapter for a pre-action ``kind``.

    ``kind`` must be one of ``"click"``, ``"double_click"``,
    ``"right_click"``, ``"focus"``, ``"type_into"``.  Anything
    else is rejected.
    """
    if kind == "click":
        return adapt_click(contract)
    if kind == "double_click":
        return adapt_double_click(contract)
    if kind == "right_click":
        return adapt_right_click(contract)
    if kind == "focus":
        return adapt_focus(contract)
    if kind == "type_into":
        return adapt_type_into(contract, text=text or "")
    raise ValueError(
        f"unsupported pre-action kind: {kind!r}; expected one of "
        f"click, double_click, right_click, focus, type_into."
    )


# ----------------------------------------------------------- helpers

def is_known_capability(name: str) -> bool:
    """True when ``name`` is one of the adapter-known capabilities."""
    return name in _KNOWN_CAPABILITIES


__all__ = [
    "AdaptedAction",
    "GroundingNotGroundableError",
    "adapt_click",
    "adapt_double_click",
    "adapt_right_click",
    "adapt_focus",
    "adapt_type_into",
    "adapt_pre_action",
    "is_known_capability",
    "_CLICK_CAPABILITY",
    "_DOUBLE_CLICK_CAPABILITY",
    "_RIGHT_CLICK_CAPABILITY",
    "_MOVE_CAPABILITY",
    "_TYPE_CAPABILITY",
]
