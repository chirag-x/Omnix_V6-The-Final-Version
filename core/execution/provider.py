"""
Omnix V6 — Execution Provider Protocols for Stage 19.0.

Defines the VerificationProvider, ActionExecutor, and GroundingProvider protocols
along with default implementations that adapt existing Omnix components.
"""

from __future__ import annotations

import asyncio
from typing import Any, Protocol, runtime_checkable, Optional, Mapping, Tuple
from dataclasses import dataclass, field

# Import existing types to avoid circular dependencies and ensure compatibility
from core.orchestration.cancellation import CancellationToken
from core.results import CapabilityResult
from core.capability_router import CapabilityRouter
from core.grounding.target_resolver import TargetResolver, TargetResolutionResult
from vision.perception_contract import (
    PerceptionProvider,
    PerceptionRequest,
    PerceptionResult,
)

# Import the execution-level VerificationResult (has verification_id/success/etc)
# - core.results.VerificationResult is the capability-level one (check_name/expected/etc)
from .result import VerificationResult, VerificationStatus


@runtime_checkable
class VerificationProvider(Protocol):
    """Canonical verification provider interface.

    Verifies post-action state against an expectation using a fresh observation.
    Does NOT decide actions, perform actions, or call LLMs for verification.
    """
    name: str

    async def verify(
        self,
        expectation: "VerificationExpectation",
        observation: PerceptionResult,  # always a fresh observation
        cancellation_token: Optional[CancellationToken] = None,
    ) -> VerificationResult:
        """Verify post-action state against expectation.

        Args:
            expectation: What to verify
            observation: Fresh perception observation (never reused from OBSERVE phase)
            cancellation_token: Optional token for cancelling the operation

        Returns:
            VerificationResult with status SUCCESS/FAILED/TIMEOUT/INCONCLUSIVE/CANCELLED

        The verification provider must:
        - Not call LLMs for verification
        - Not decide what actions to perform
        - Not perform any actions
        - Return structured verification result only
        """
        ...


@runtime_checkable
class ActionExecutor(Protocol):
    """Thin seam the cycle uses to invoke a capability without
    depending on core.capability_router. Lets tests inject fakes.
    """
    name: str

    async def execute(
        self,
        capability_name: str,
        parameters: Mapping[str, Any],
        target: Optional[Any] = None,  # ResolvedTarget or None
        timeout_s: float = 30.0,
        cancellation_token: Optional[CancellationToken] = None,
    ) -> CapabilityResult:
        """Execute a capability with given parameters and optional target.

        Args:
            capability_name: Name of capability to execute
            parameters: Parameters for the capability
            target: Optional resolved target (for mouse/keyboard capabilities)
            timeout_s: Timeout in seconds
            cancellation_token: Optional token for cancelling the operation

        Returns:
            CapabilityResult with status and phase information
        """
        ...


@runtime_checkable
class GroundingProvider(Protocol):
    """Thin seam for target resolution. Production wiring uses
    core.grounding.target_resolver.TargetResolver; tests inject a fake.
    """
    name: str

    def resolve(
        self,
        target_input: Any,
        *,
        screen_width: Optional[int] = None,
        screen_height: Optional[int] = None,
    ) -> TargetResolutionResult:
        """Resolve a target input to a ResolvedTarget.

        Args:
            target_input: Target query, coordinate, or hint to resolve
            screen_width: Optional screen width for bounds checking
            screen_height: Optional screen height for bounds checking

        Returns:
            TargetResolutionResult with status and resolved target data
        """
        ...


@dataclass
class DefaultActionExecutor:
    """Default action executor that adapts CapabilityRouter to ActionExecutor protocol."""
    _router: CapabilityRouter
    name: str = field(default="default_action_executor")

    async def execute(
        self,
        capability_name: str,
        parameters: Mapping[str, Any],
        target: Optional[Any] = None,
        timeout_s: float = 30.0,
        cancellation_token: Optional[CancellationToken] = None,
    ) -> CapabilityResult:
        # Prepare parameters - mouse/keyboard capabilities accept a 'target' param
        params = dict(parameters or {})
        if target is not None:
            params["target"] = target

        # Execute via router with cancellation token
        # Note: timeout_s is handled by the capability itself, not the router
        result = self._router.route(
            capability_name,
            params,
            cancellation_token=cancellation_token,
        )
        return result


@dataclass
class DefaultGroundingProvider:
    """Default grounding provider that adapts TargetResolver to GroundingProvider protocol."""
    _resolver: TargetResolver
    name: str = field(default="default_grounding_provider")

    def resolve(
        self,
        target_input: Any,
        *,
        screen_width: Optional[int] = None,
        screen_height: Optional[int] = None,
    ) -> TargetResolutionResult:
        # Delegate to the target resolver
        return self._resolver.resolve(target_input, screen_width=screen_width, screen_height=screen_height)


@dataclass
class DefaultVerificationProvider:
    """Default verification provider that supports basic expectations using perception."""
    _perception_provider: PerceptionProvider
    name: str = field(default="default_verification_provider")

    async def verify(
        self,
        expectation: "VerificationExpectation",
        observation: PerceptionResult,
        cancellation_token: Optional[CancellationToken] = None,
    ) -> VerificationResult:
        """Verify expectation using the given observation.

        This implementation supports a honest set of expectations that can be
        verified deterministically using existing perception data.
        """
        from .expectation import ExpectationKind
        from .result import VerificationStatus
        import time

        start_time = time.time()

        # Check cancellation
        if cancellation_token and hasattr(cancellation_token, 'is_cancelled') and cancellation_token.is_cancelled:
            return VerificationResult(
                verification_id="",  # Will be set by __post_init__
                status=VerificationStatus.CANCELLED,
                success=False,
                observation_id=observation.observation_id,
                elapsed_ms=(time.time() - start_time) * 1000,
                reason="Verification cancelled",
                attempt=1,
            )

        # Handle NONE expectation - always succeed
        if expectation.kind == ExpectationKind.NONE:
            return VerificationResult(
                verification_id="",  # Will be set by __post_init__
                status=VerificationStatus.SUCCESS,
                success=True,
                confidence=1.0,
                evidence=observation,
                observation_id=observation.observation_id,
                elapsed_ms=(time.time() - start_time) * 1000,
                reason="No verification requested",
                attempt=1,
            )

        # For other expectations, delegate to specific verifiers
        verifier = getattr(self, f"_verify_{expectation.kind.value}", None)
        if verifier is None:
            # Unsupported expectation - return INCONCLUSIVE (honest failure)
            return VerificationResult(
                verification_id="",  # Will be set by __post_init__
                status=VerificationStatus.INCONCLUSIVE,
                success=False,
                evidence=observation,
                observation_id=observation.observation_id,
                elapsed_ms=(time.time() - start_time) * 1000,
                reason=f"Unsupported expectation kind: {expectation.kind.value}",
                attempt=1,
            )

        try:
            # Call the specific verifier
            result = await verifier(expectation, observation, cancellation_token)
            # Ensure timing is set (use replace since VerificationResult is frozen)
            from dataclasses import replace
            if result.elapsed_ms == 0.0:
                result = replace(result, elapsed_ms=(time.time() - start_time) * 1000)
            return result
        except Exception as e:
            # Verification error - return FAILED
            return VerificationResult(
                verification_id="",  # Will be set by __post_init__
                status=VerificationStatus.FAILED,
                success=False,
                evidence=observation,
                observation_id=observation.observation_id,
                elapsed_ms=(time.time() - start_time) * 1000,
                reason=f"Verification error: {str(e)}",
                attempt=1,
            )

    async def _verify_target_visible(
        self,
        expectation: "VerificationExpectation",
        observation: PerceptionResult,
        cancellation_token: Optional[CancellationToken] = None,
    ) -> VerificationResult:
        """Verify that a target matching the query is visible."""
        from .result import VerificationStatus

        # Check if any candidate matches the target query
        query = expectation.target_query.lower()
        for candidate in observation.candidates:
            # Check text match
            if hasattr(candidate, 'text') and candidate.text and query in candidate.text.lower():
                return VerificationResult(
                    verification_id="",  # Will be set by __post_init__
                    status=VerificationStatus.SUCCESS,
                    success=True,
                    confidence=getattr(candidate, 'confidence', 1.0),
                    evidence=observation,
                    observation_id=observation.observation_id,
                    elapsed_ms=0.0,  # Will be set by caller
                    reason=f"Target '{expectation.target_query}' found",
                    attempt=1,
                )
            # Check properties/label match
            if hasattr(candidate, 'properties'):
                props = candidate.properties
                if isinstance(props, dict):
                    label = props.get('name') or props.get('label') or props.get('automation_id')
                    if label and query in label.lower():
                        return VerificationResult(
                            verification_id="",  # Will be set by __post_init__
                            status=VerificationStatus.SUCCESS,
                            success=True,
                            confidence=getattr(candidate, 'confidence', 1.0),
                            evidence=observation,
                            observation_id=observation.observation_id,
                            elapsed_ms=0.0,
                            reason=f"Target '{expectation.target_query}' found",
                            attempt=1,
                        )

        return VerificationResult(
            verification_id="",  # Will be set by __post_init__
            status=VerificationStatus.FAILED,
            success=False,
            evidence=observation,
            observation_id=observation.observation_id,
            elapsed_ms=0.0,
            reason=f"Target '{expectation.target_query}' not found",
            attempt=1,
        )

    async def _verify_target_present(
        self,
        expectation: "VerificationExpectation",
        observation: PerceptionResult,
        cancellation_token: Optional[CancellationToken] = None,
    ) -> VerificationResult:
        """Verify that a target matching the query is present (same as visible for now)."""
        return await self._verify_target_visible(expectation, observation, cancellation_token)

    async def _verify_target_absent(
        self,
        expectation: "VerificationExpectation",
        observation: PerceptionResult,
        cancellation_token: Optional[CancellationToken] = None,
    ) -> VerificationResult:
        """Verify that no target matching the query is present."""
        # Check that NO candidate matches the target query
        query = expectation.target_query.lower()
        for candidate in observation.candidates:
            # Check text match
            if hasattr(candidate, 'text') and candidate.text and query in candidate.text.lower():
                return VerificationResult(
                    verification_id="",  # Will be set by __post_init__
                    status=VerificationStatus.FAILED,
                    success=False,
                    confidence=getattr(candidate, 'confidence', 1.0),
                    evidence=observation,
                    observation_id=observation.observation_id,
                    elapsed_ms=0.0,
                    reason=f"Target '{expectation.target_query}' found (should be absent)",
                    attempt=1,
                )
            # Check properties/label match
            if hasattr(candidate, 'properties'):
                props = candidate.properties
                if isinstance(props, dict):
                    label = props.get('name') or props.get('label') or props.get('automation_id')
                    if label and query in label.lower():
                        return VerificationResult(
                            verification_id="",  # Will be set by __post_init__
                            status=VerificationStatus.FAILED,
                            success=False,
                            confidence=getattr(candidate, 'confidence', 1.0),
                            evidence=observation,
                            observation_id=observation.observation_id,
                            elapsed_ms=0.0,
                            reason=f"Target '{expectation.target_query}' found (should be absent)",
                            attempt=1,
                        )

        return VerificationResult(
            verification_id="",  # Will be set by __post_init__
            status=VerificationStatus.SUCCESS,
            success=True,
            confidence=1.0,
            evidence=observation,
            observation_id=observation.observation_id,
            elapsed_ms=0.0,
            reason=f"Target '{expectation.target_query}' correctly absent",
            attempt=1,
        )

    async def _verify_window_exists(
        self,
        expectation: "VerificationExpectation",
        observation: PerceptionResult,
        cancellation_token: Optional[CancellationToken] = None,
    ) -> VerificationResult:
        """Verify that a window with the expected title/application exists."""
        from .result import VerificationStatus

        window_ctx = observation.window_context
        if not window_ctx:
            return VerificationResult(
                verification_id="",  # Will be set by __post_init__
                status=VerificationStatus.FAILED,
                success=False,
                evidence=observation,
                observation_id=observation.observation_id,
                elapsed_ms=0.0,
                reason="No window context in observation",
                attempt=1,
            )

        # Check title match
        title_match = True
        if expectation.expected_window_title:
            if not window_ctx.title:
                title_match = False
            else:
                title_match = expectation.expected_window_title.lower() in window_ctx.title.lower()

        # Check application match
        app_match = True
        if expectation.expected_application:
            if not window_ctx.application:
                app_match = False
            else:
                app_match = expectation.expected_application.lower() in window_ctx.application.lower()

        if title_match and app_match:
            return VerificationResult(
                verification_id="",  # Will be set by __post_init__
                status=VerificationStatus.SUCCESS,
                success=True,
                confidence=1.0,
                evidence=observation,
                observation_id=observation.observation_id,
                elapsed_ms=0.0,
                reason=f"Window matches expectations",
                attempt=1,
            )

        return VerificationResult(
            verification_id="",  # Will be set by __post_init__
            status=VerificationStatus.FAILED,
            success=False,
            evidence=observation,
            observation_id=observation.observation_id,
            elapsed_ms=0.0,
            reason=f"Window does not match expectations",
            attempt=1,
        )

    async def _verify_window_focused(
        self,
        expectation: "VerificationExpectation",
        observation: PerceptionResult,
        cancellation_token: Optional[CancellationToken] = None,
    ) -> VerificationResult:
        """Verify that a window with the expected title/application is focused."""
        # First check if window exists and matches
        exist_result = await self._verify_window_exists(expectation, observation, cancellation_token)
        if not exist_result.success:
            return exist_result

        # Then check if it's focused
        window_ctx = observation.window_context
        if not window_ctx or not window_ctx.is_foreground:
            return VerificationResult(
                verification_id="",  # Will be set by __post_init__
                status=VerificationStatus.FAILED,
                success=False,
                evidence=observation,
                observation_id=observation.observation_id,
                elapsed_ms=0.0,
                reason=f"Window exists but is not focused",
                attempt=1,
            )

        return VerificationResult(
            verification_id="",  # Will be set by __post_init__
            status=VerificationStatus.SUCCESS,
            success=True,
            confidence=1.0,
            evidence=observation,
            observation_id=observation.observation_id,
            elapsed_ms=0.0,
            reason=f"Window is focused and matches expectations",
            attempt=1,
        )

    async def _verify_text_present(
        self,
        expectation: "VerificationExpectation",
        observation: PerceptionResult,
        cancellation_token: Optional[CancellationToken] = None,
    ) -> VerificationResult:
        """Verify that the expected text is present on screen."""
        from .result import VerificationStatus

        # This would require OCR data from perception
        # For now, return INCONCLUSIVE if OCR data not available
        # In a full implementation, we'd check perceptionResult.ocr_text or similar
        return VerificationResult(
            verification_id="",  # Will be set by __post_init__
            status=VerificationStatus.INCONCLUSIVE,
            success=False,
            evidence=observation,
            observation_id=observation.observation_id,
            elapsed_ms=0.0,
            reason=f"Text verification not implemented (requires OCR data)",
            attempt=1,
        )

    async def _verify_text_changed(
        self,
        expectation: "VerificationExpectation",
        observation: PerceptionResult,
        cancellation_token: Optional[CancellationToken] = None,
    ) -> VerificationResult:
        """Verify that text has changed since the reference observation."""
        # This would require storing and comparing OCR text
        # For now, return INCONCLUSIVE
        return VerificationResult(
            verification_id="",  # Will be set by __post_init__
            status=VerificationStatus.INCONCLUSIVE,
            success=False,
            evidence=observation,
            observation_id=observation.observation_id,
            elapsed_ms=0.0,
            reason=f"Text changed verification not implemented",
            attempt=1,
        )

    async def _verify_screen_changed(
        self,
        expectation: "VerificationExpectation",
        observation: PerceptionResult,
        cancellation_token: Optional[CancellationToken] = None,
    ) -> VerificationResult:
        """Verify that the screen has changed since the reference observation."""
        from .result import VerificationStatus

        # For screen changed, we need to compare with the reference observation
        # Since we don't have access to the reference observation here,
        # we'd need to pass it in or store it elsewhere
        # For now, return INCONCLUSIVE as we can't determine change without reference
        return VerificationResult(
            verification_id="",  # Will be set by __post_init__
            status=VerificationStatus.INCONCLUSIVE,
            success=False,
            evidence=observation,
            observation_id=observation.observation_id,
            elapsed_ms=0.0,
            reason=f"Screen changed verification requires reference observation",
            attempt=1,
        )

    async def _verify_focus_changed(
        self,
        expectation: "VerificationExpectation",
        observation: PerceptionResult,
        cancellation_token: Optional[CancellationToken] = None,
    ) -> VerificationResult:
        """Verify that window focus has changed since the reference observation."""
        # Similar to screen changed - need reference observation
        return VerificationResult(
            verification_id="",  # Will be set by __post_init__
            status=VerificationStatus.INCONCLUSIVE,
            success=False,
            evidence=observation,
            observation_id=observation.observation_id,
            elapsed_ms=0.0,
            reason=f"Focus changed verification requires reference observation",
            attempt=1,
        )


# Factory functions for easy creation
def create_default_action_router(
    router: CapabilityRouter,
) -> DefaultActionExecutor:
    """Create a default action executor."""
    return DefaultActionExecutor(router=router)


def create_default_grounding_provider(
    resolver: TargetResolver,
) -> DefaultGroundingProvider:
    """Create a default grounding provider."""
    return DefaultGroundingProvider(resolver=resolver)


def create_default_verification_provider(
    perception_provider: PerceptionProvider,
) -> DefaultVerificationProvider:
    """Create a default verification provider."""
    return DefaultVerificationProvider(perception_provider=perception_provider)