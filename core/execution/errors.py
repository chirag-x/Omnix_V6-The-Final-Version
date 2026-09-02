"""
Omnix V6 — Execution Errors for Stage 19.0.

Defines typed errors used for __init__ validation in the execution cycle.
These are not raised during normal execution - execution failures are
returned as ExecutionResult objects with appropriate status.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


class ExecutionError(Exception):
    """Base class for execution-related configuration errors."""

    def __init__(self, message: str, *, context: Optional[dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.context = context or {}


class ObservationFailedError(ExecutionError):
    """Raised when perception provider is missing or invalid."""

    def __init__(self, message: str = "Perception provider is required", *, context: Optional[dict[str, Any]] = None) -> None:
        super().__init__(message, context=context)


class GroundingFailedError(ExecutionError):
    """Raised when grounding provider is missing or invalid."""

    def __init__(self, message: str = "Grounding provider is required", *, context: Optional[dict[str, Any]] = None) -> None:
        super().__init__(message, context=context)


class ActionFailedError(ExecutionError):
    """Raised when action executor is missing or invalid."""

    def __init__(self, message: str = "Action executor is required", *, context: Optional[dict[str, Any]] = None) -> None:
        super().__init__(message, context=context)


class VerificationFailedError(ExecutionError):
    """Raised when verification provider is missing or invalid."""

    def __init__(self, message: str = "Verification provider is required", *, context: Optional[dict[str, Any]] = None) -> None:
        super().__init__(message, context=context)


@dataclass(frozen=True)
class InvalidConfigurationError(ExecutionError):
    """Raised when the execution cycle is configured with invalid parameters."""

    config_issue: str
    received_value: Any = None

    def __init__(
        self,
        config_issue: str,
        received_value: Any = None,
        *,
        context: Optional[dict[str, Any]] = None,
    ) -> None:
        message = f"Invalid configuration: {config_issue}"
        if received_value is not None:
            message += f" (got {received_value!r})"
        super().__init__(message, context=context)
        self.config_issue = config_issue
        self.received_value = received_value