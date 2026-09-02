"""
Omnix V6 — desktop.wait capability (Stage 18.6).

Provides a deterministic wait primitive that sleeps for a specified duration
while supporting cooperative cancellation. No AI, no perception, no busy loop.
"""

from __future__ import annotations

import asyncio
from typing import Any, Mapping, Optional

from core.capability import CapabilitySpec, CapabilityParameter, ParamType
from core.results import CapabilityResult, CapabilityStatus
from core.errors import OmnixError
from core.utils.timers import CancellationToken, OperationCancelled
from .base import BaseCapability


class WaitCapability(BaseCapability):
    """Deterministic wait capability with cancellation support."""

    @property
    def spec(self) -> CapabilitySpec:
        return CapabilitySpec(
            name="desktop.wait",
            version="1.0.0",
            description=(
                "Waits for the specified duration in seconds. "
                "Supports cooperative cancellation via CancellationToken. "
                "No AI involvement, no perception, no busy loop."
            ),
            parameters={
                "duration_s": CapabilityParameter(
                    name="duration_s",
                    type=ParamType.FLOAT,
                    description="Wait duration in seconds (must be > 0 and <= 300).",
                    required=True,
                ),
            },
            tags={"desktop", "wait"},
        )

    async def execute(self, params: Mapping[str, Any]) -> CapabilityResult:
        """Execute wait with cancellation support."""
        duration_s = params.get("duration_s")
        if duration_s is None:
            return CapabilityResult(
                capability_name=self.spec.name,
                status=CapabilityStatus.FAILED,
                failed=True,
                error=OmnixError("duration_s parameter is required"),
            )

        try:
            duration_s = float(duration_s)
        except (ValueError, TypeError):
            return CapabilityResult(
                capability_name=self.spec.name,
                status=CapabilityStatus.FAILED,
                failed=True,
                error=OmnixError("duration_s must be a number"),
            )

        # Validate bounds
        if duration_s <= 0:
            return CapabilityResult(
                capability_name=self.spec.name,
                status=CapabilityStatus.FAILED,
                failed=True,
                error=OmnixError("duration_s must be greater than 0"),
            )

        if duration_s > 300:  # 5 minute max to prevent runaway waits
            return CapabilityResult(
                capability_name=self.spec.name,
                status=CapabilityStatus.FAILED,
                failed=True,
                error=OmnixError("duration_s must not exceed 300 seconds (5 minutes)"),
            )

        # Get cancellation token if provided
        cancellation_token: Optional[CancellationToken] = params.get("cancellation_token")

        try:
            # Wait in small chunks to check for cancellation
            slept = 0.0
            chunk_size = 0.05  # 50ms chunks for responsive cancellation

            while slept < duration_s:
                # Check for cancellation
                if cancellation_token is not None:
                    try:
                        cancellation_token.check()
                    except OperationCancelled:
                        return CapabilityResult(
                            capability_name=self.spec.name,
                            status=CapabilityStatus.CANCELLED,
                            attempted=True,
                        )

                # Sleep for a chunk or remaining time, whichever is smaller
                remaining = duration_s - slept
                sleep_time = min(chunk_size, remaining)
                await asyncio.sleep(sleep_time)
                slept += sleep_time

            # Completed successfully
            return CapabilityResult(
                capability_name=self.spec.name,
                status=CapabilityStatus.EXECUTED,
                executed=True,
            )

        except Exception as exc:  # noqa: BLE001
            return CapabilityResult(
                capability_name=self.spec.name,
                status=CapabilityStatus.FAILED,
                failed=True,
                error=OmnixError(f"Wait capability failed: {exc!r}"),
            )


__all__ = ["WaitCapability"]