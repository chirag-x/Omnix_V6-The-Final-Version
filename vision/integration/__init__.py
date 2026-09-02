"""
Omnix V6 — Vision integration helpers (Phase 13).

The :class:`VisionTargetProvider` Protocol is the *typed* seam
between the :class:`core.orchestration.agent.Agent` and the
:class:`core.services.vision_service.VisionService`.

Why a separate module
---------------------
The Agent historically accepts a ``vision_service`` keyword
typed as ``Any``.  Phase 13 keeps that keyword for backwards
compatibility (Phase 7.2 already passes a service) but defines
a *narrow* Protocol that the Agent can also accept.  The default
implementation adapts a :class:`VisionService` (which returns
:class:`VisionResult` objects) to the protocol (which returns
:class:`TargetGroundingContract` objects), enforcing the
screenshot-freshness gate in the process.
"""
from .agent_provider import (
    VisionTargetProvider,
    DefaultVisionTargetProvider,
)

__all__ = [
    "VisionTargetProvider",
    "DefaultVisionTargetProvider",
]
