"""Browser targeting strategies (Phase 8).

DOM-first resolution lives in :mod:`browser.session.session` (it has
to touch the live page).  This module contains *advisory* strategies
the service consults *before* resolution:

* :class:`TextNormalizer` — canonicalises whitespace / case so the
  Brain can compare ``"Sign  in"`` to ``"Sign in"``.
* :class:`RelativeTargetResolver` — given a :class:`BrowserTarget`
  with ``label=...`` and ``nth=...``, returns a *refined* locator
  hint the session can use to disambiguate.
* :class:`VisionFallbackAdapter` — duck-typed adapter the service
  consults *only after* DOM resolution has failed.  The service
  never assumes the adapter is present; in tests and dev mode it
  is ``None`` and the fallback is a no-op.
"""

from browser.strategies.normalize import TextNormalizer
from browser.strategies.relative import RelativeTargetResolver
from browser.strategies.vision_fallback import (
    NullVisionFallback,
    VisionFallback,
    VisionFallbackAdapter,
)

__all__ = [
    "TextNormalizer",
    "RelativeTargetResolver",
    "NullVisionFallback",
    "VisionFallback",
    "VisionFallbackAdapter",
]
