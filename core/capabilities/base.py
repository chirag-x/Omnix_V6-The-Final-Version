"""
Omnix V6 - Base capability.

This module provides the base structure for concrete capabilities.
All concrete capabilities should inherit from BaseCapability and
implement the execute method conforming to the Capability protocol
defined in core.capability.
"""

from typing import Any, Mapping

from core.capability import Capability, CapabilitySpec
from core.results import CapabilityResult, ActionStatus, CapabilityStatus

class BaseCapability(Capability):
    """
    Base class for all capabilities.
    
    Subclasses must define a spec property and an execute method.
    """
    
    @property
    def spec(self) -> CapabilitySpec:
        """Return the immutable specification of the capability."""
        raise NotImplementedError("Subclasses must implement spec property.")

    async def execute(self, params: Mapping[str, Any]) -> CapabilityResult:
        """
        Execute the capability with the given matched parameters.

        This method should be overridden by actual capability implementations.
        """
        raise NotImplementedError("Subclasses must implement execute method.")

    def is_available(self) -> bool:
        """Return ``True`` by default; concrete capabilities may override.

        The :class:`Capability` Protocol in :mod:`core.capability` declares
        this method with a default that yields ``None``.  A ``None`` return
        is treated as unavailable by :class:`CapabilityRegistry`, so any
        concrete capability that does not override this method would be
        silently routed to ``SKIPPED``.  We default to available here so
        that capabilities that do not have any preconditions to check
        Just Work, while leaving the door open for capabilities that need
        a live probe (e.g. "is the target app running?") to override.
        """
        return True
