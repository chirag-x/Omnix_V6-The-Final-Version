"""System: process service package."""
from .process_service import (
    WindowsProcessService,
    NON_OVERRIDABLE_PROTECTED,
    DEFAULT_PROTECTED,
)

__all__ = [
    "WindowsProcessService",
    "NON_OVERRIDABLE_PROTECTED",
    "DEFAULT_PROTECTED",
]
