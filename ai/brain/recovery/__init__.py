"""
Omnix V6 — System 2 Brain recovery classification (Phase 17).

Public surface:

    * :class:`FailureKind`     — closed set of failure categories
    * :class:`RecoveryStrategy`— the Brain's per-category strategy
    * :class:`RecoveryClassifier` — the function that turns a
                                  :class:`Failure` into a
                                  :class:`RecoveryStrategy`

This module is pure data + a single classification function.  It
never imports a Windows service, a capability, or an LLM provider.
"""
from __future__ import annotations

from .classification import (
    FailureKind,
    RecoveryClassifier,
    RecoveryStrategy,
)

__all__ = ["FailureKind", "RecoveryClassifier", "RecoveryStrategy"]
