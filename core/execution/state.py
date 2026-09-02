"""
Omnix V6 — Execution State Model for Stage 19.2.

Defines the ExecutionState model that represents the relevant known state
surrounding an execution step, enabling precondition checking and state
transition tracking.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import uuid4

# Import existing types where appropriate
from vision.perception_contract import ScreenInfo, WindowContext, PerceptionResult
from core.grounding.target_resolver import ResolvedTarget
from vision.grounded_element import GroundedElement


@dataclass(frozen=True)
class ExecutionState:
    """
    Lightweight representation of the relevant known state surrounding
    an execution step.

    This model captures what we know about the system state at a point in time
    to enable precondition checking and state transition verification.

    The state remains lightweight - it does not duplicate the entire desktop
    or perception model, but focuses on execution-relevant information.
    """

    # Unique identifier for this state snapshot
    state_id: str = field(default_factory=lambda: str(uuid4()))

    # Timestamp when this state was captured
    timestamp: datetime = field(default_factory=datetime.now)

    # Observation that this state is based on (can be None for initial state)
    observation_id: Optional[str] = None

    # Screen/context information at the time of observation
    screen_info: Optional[ScreenInfo] = None

    # Window context at the time of observation
    window_context: Optional[WindowContext] = None

    # Resolved targets that were known at this state
    resolved_targets: List[ResolvedTarget] = field(default_factory=list)

    # Grounded elements that were known at this state
    grounded_elements: List[GroundedElement] = field(default_factory=list)

    # Focus context - what was focused/interactable
    focus_context: Optional[str] = None  # e.g., window title, element ID

    # Additional metadata for extensibility
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        # Ensure state_id is set
        if not self.state_id:
            object.__setattr__(self, 'state_id', str(uuid4()))

        # Ensure timestamp is set
        if self.timestamp is None:
            object.__setattr__(self, 'timestamp', datetime.now())

    @classmethod
    def from_observation(
        cls,
        observation: PerceptionResult,
        resolved_targets: Optional[List[ResolvedTarget]] = None,
        grounded_elements: Optional[List[GroundedElement]] = None,
        focus_context: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> "ExecutionState":
        """
        Create an ExecutionState from a perception observation.

        Args:
            observation: The perception result to base state on
            resolved_targets: List of resolved targets known at this time
            grounded_elements: List of grounded elements known at this time
            focus_context: What was focused/interactable at this time
            metadata: Additional metadata to include

        Returns:
            ExecutionState populated from the observation
        """
        return cls(
            state_id=str(uuid4()),
            timestamp=observation.timestamp,
            observation_id=observation.observation_id,
            screen_info=observation.screen,
            window_context=observation.window_context,
            resolved_targets=resolved_targets or [],
            grounded_elements=grounded_elements or [],
            focus_context=focus_context,
            metadata=metadata or {},
        )

    @classmethod
    def initial_state(cls) -> "ExecutionState":
        """
        Create an initial execution state with no prior observation.

        Returns:
            ExecutionState representing initial/unknown state
        """
        return cls(
            state_id=str(uuid4()),
            timestamp=datetime.now(),
            observation_id=None,
            screen_info=None,
            window_context=None,
            resolved_targets=[],
            grounded_elements=[],
            focus_context=None,
            metadata={},
        )

    def is_stale(self, max_age_seconds: float = 5.0) -> bool:
        """
        Check if this state is stale based on age.

        Args:
            max_age_seconds: Maximum age in seconds before considering stale

        Returns:
            True if state is older than max_age_seconds
        """
        age = (datetime.now() - self.timestamp).total_seconds()
        return age > max_age_seconds

    def with_updated_metadata(self, additional_metadata: Dict[str, Any]) -> "ExecutionState":
        """
        Return a new ExecutionState with updated metadata.

        Args:
            additional_metadata: Additional metadata to merge in

        Returns:
            New ExecutionState with updated metadata
        """
        new_metadata = dict(self.metadata)
        new_metadata.update(additional_metadata)
        return ExecutionState(
            state_id=self.state_id,
            timestamp=self.timestamp,
            observation_id=self.observation_id,
            screen_info=self.screen_info,
            window_context=self.window_context,
            resolved_targets=self.resolved_targets,
            grounded_elements=self.grounded_elements,
            focus_context=self.focus_context,
            metadata=new_metadata,
        )

    def to_dict(self) -> Dict[str, Any]:
        """
        Convert to dictionary for serialization/logging.

        Returns:
            Dictionary representation of the ExecutionState
        """
        return {
            "state_id": self.state_id,
            "timestamp": self.timestamp.timestamp(),
            "observation_id": self.observation_id,
            "screen_info": self.screen_info.__dict__ if self.screen_info else None,
            "window_context": self.window_context.__dict__ if self.window_context else None,
            "resolved_targets": [target.to_dict() if hasattr(target, 'to_dict') else str(target) for target in self.resolved_targets],
            "grounded_elements": [element.to_dict() if hasattr(element, 'to_dict') else str(element) for element in self.grounded_elements],
            "focus_context": self.focus_context,
            "metadata": dict(self.metadata),
        }


# Convenience functions for state comparison
def states_equivalent(state1: ExecutionState, state2: ExecutionState, ignore_fields: Optional[List[str]] = None) -> bool:
    """
    Check if two ExecutionStates are equivalent for practical purposes.

    Args:
        state1: First state to compare
        state2: Second state to compare
        ignore_fields: List of field names to ignore in comparison

    Returns:
        True if states are equivalent (ignoring specified fields)
    """
    if ignore_fields is None:
        ignore_fields = ['state_id', 'timestamp']  # These are expected to differ

    # Compare all non-ignored fields
    for field_name in ['observation_id', 'screen_info', 'window_context', 'focus_context', 'metadata']:
        if field_name in ignore_fields:
            continue
        val1 = getattr(state1, field_name)
        val2 = getattr(state2, field_name)
        if val1 != val2:
            return False

    # Special handling for lists - compare content
    if 'resolved_targets' not in ignore_fields:
        if len(state1.resolved_targets) != len(state2.resolved_targets):
            return False
        # Simple comparison - in practice might need more sophisticated target comparison
        for t1, t2 in zip(state1.resolved_targets, state2.resolved_targets):
            if hasattr(t1, 'target_id') and hasattr(t2, 'target_id'):
                if t1.target_id != t2.target_id:
                    return False
            elif str(t1) != str(t2):
                return False

    if 'grounded_elements' not in ignore_fields:
        if len(state1.grounded_elements) != len(state2.grounded_elements):
            return False
        for e1, e2 in zip(state1.grounded_elements, state2.grounded_elements):
            if hasattr(e1, 'element_id') and hasattr(e2, 'element_id'):
                if e1.element_id != e2.element_id:
                    return False
            elif str(e1) != str(e2):
                return False

    return True