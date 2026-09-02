"""
Text normalisation helpers (Phase 8).

Used by the deterministic test suite to compare expected vs
actual text.  Pure functions, no LLM.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Optional


_WHITESPACE = re.compile(r"\s+")


class TextNormalizer:
    """Canonical text comparison for the browser subsystem.

    The Brain / Verifier use this to compare a *requested* text
    ("Search") against what was actually extracted ("  search  ")
    without false negatives.
    """

    @staticmethod
    def normalize(text: Optional[str]) -> str:
        if not isinstance(text, str):
            return ""
        # Unicode-normalise first (so e.g. fullwidth spaces collapse).
        text = unicodedata.normalize("NFKC", text)
        # Collapse all runs of whitespace into a single space.
        text = _WHITESPACE.sub(" ", text).strip()
        return text

    @classmethod
    def equals(cls, a: Optional[str], b: Optional[str]) -> bool:
        return cls.normalize(a).lower() == cls.normalize(b).lower()

    @classmethod
    def contains(cls, haystack: Optional[str], needle: Optional[str]) -> bool:
        return cls.normalize(needle).lower() in cls.normalize(haystack).lower()
