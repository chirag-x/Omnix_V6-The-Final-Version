"""
Omnix V6 — System 3 (Vision) screen-stability detector.

Detects whether the screen is currently changing.  Used by
:func:`vision.api.wait_for` to honour the spec's "wait until the
screen stops moving" semantics.

The detector is intentionally cheap and approximate.  It does
NOT try to be a perceptual-hash library; the goal is to give
the new public API a way to know "should I keep polling, or is
this the same screen I saw a moment ago?".  High-resolution
decisions (which element moved, by how much) are the job of
:mod:`vision.recovery`.

Two layers:

  * :func:`compute_stability` — a pure function over two
    images.  Returns a score in ``[0.0, 1.0]``: 1.0 means
    identical, 0.0 means completely different.

  * :func:`is_stable` and :class:`StabilityWindow` — a rolling
    window over recent stability scores.  ``is_stable`` returns
    ``True`` when the last N scores are all at or above a
    threshold.  Used by ``wait_for(stable_for_s=...)``.

When an image library (PIL/Pillow) is unavailable the detector
falls back to a file-size and mtime comparison.  This is much
weaker but it is enough to drive the polling loop.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any, List, Optional, Sequence, Tuple

DEFAULT_THRESHOLD = 0.98


def _load_pixels(path: str) -> Optional[Any]:
    """Best-effort pixel load.  Returns ``None`` when the file
    is missing, not an image, or the host has no image lib.
    """
    if not path or not os.path.isfile(path):
        return None
    try:
        from PIL import Image  # type: ignore
        with Image.open(path) as img:
            # Downsample to a tiny grayscale for the hash.  This
            # keeps the cost O(thousands) of pixels, not millions.
            img = img.convert("L").resize((32, 32))
            return list(img.getdata())
    except Exception:  # noqa: BLE001
        return None


def _pixel_hash(pixels: Sequence[int]) -> Tuple[int, ...]:
    """Return a small perceptual hash from a 32x32 grayscale."""
    if not pixels:
        return tuple()
    avg = sum(pixels) / len(pixels)
    return tuple(1 if p > avg else 0 for p in pixels)


def _hamming(a: Sequence[int], b: Sequence[int]) -> int:
    if not a or not b:
        return max(len(a), len(b))
    n = min(len(a), len(b))
    return sum(1 for i in range(n) if a[i] != b[i]) + abs(len(a) - len(b))


def compute_stability(
    image_a: str,
    image_b: str,
    *,
    hamming_threshold: int = 4,
) -> float:
    """Compute a stability score in ``[0.0, 1.0]`` between two
    screenshot files.

    Algorithm:
      1. Load each image as a 32x32 grayscale (PIL).
      2. Compute a 1024-bit perceptual hash from the average
         pixel value.
      3. Return ``1.0`` when the Hamming distance is at or below
         ``hamming_threshold``, ``0.0`` when the distance is at
         the maximum, and linearly interpolated in between.

    When either image cannot be loaded, falls back to a
    file-size and mtime comparison: identical size and mtime
    returns ``1.0``, different size returns ``0.0``, identical
    size but different mtime returns ``0.5``.

    The function is pure: no side effects, no LLM, no
    capability calls.
    """
    if not image_a or not image_b:
        return 0.0
    if image_a == image_b:
        return 1.0
    pix_a = _load_pixels(image_a)
    pix_b = _load_pixels(image_b)
    if pix_a is None or pix_b is None:
        try:
            sa = os.path.getsize(image_a)
            sb = os.path.getsize(image_b)
            if sa != sb:
                return 0.0
            ma = os.path.getmtime(image_a)
            mb = os.path.getmtime(image_b)
            if abs(ma - mb) < 0.5:
                return 1.0
            return 0.5
        except OSError:
            return 0.0
    hash_a = _pixel_hash(pix_a)
    hash_b = _pixel_hash(pix_b)
    dist = _hamming(hash_a, hash_b)
    if not hash_a:
        return 0.0
    if dist <= hamming_threshold:
        return 1.0
    return max(0.0, 1.0 - (dist / len(hash_a)))


@dataclass
class StabilityWindow:
    """Rolling window of recent stability scores.

    :class:`StabilityWindow` is a small mutable helper.  The
    detector pushes a score on every screenshot poll; ``is_stable``
    returns ``True`` when the last ``window`` scores are all at
    or above ``threshold``.  When fewer than ``window`` scores
    have been observed, ``is_stable`` returns ``False`` (we
    cannot yet confirm stability).
    """

    threshold: float = DEFAULT_THRESHOLD
    window: int = 3
    scores: List[float] = field(default_factory=list)

    def push(self, score: float) -> None:
        s = max(0.0, min(1.0, float(score)))
        self.scores.append(s)
        if len(self.scores) > self.window:
            self.scores = self.scores[-self.window:]

    @property
    def last(self) -> Optional[float]:
        return self.scores[-1] if self.scores else None

    def is_stable(self) -> bool:
        if len(self.scores) < self.window:
            return False
        return all(s >= self.threshold for s in self.scores[-self.window:])


def is_stable(
    recent_images: Sequence[str],
    *,
    threshold: float = DEFAULT_THRESHOLD,
    window: int = 3,
) -> bool:
    """Return ``True`` when the last ``window`` images in
    ``recent_images`` all have a stability score ``>= threshold``.

    The helper is a thin wrapper over :class:`StabilityWindow`
    for one-shot callers.  Long-running callers should construct
    a :class:`StabilityWindow` once and push scores as they
    arrive.
    """
    sw = StabilityWindow(threshold=threshold, window=window)
    for path in recent_images:
        # Compare against the previous image, not the first —
        # that way the rolling score reflects the latest delta.
        if len(sw.scores) == 0:
            sw.push(1.0)
            continue
        prev = recent_images[len(sw.scores) - 1]
        sw.push(compute_stability(prev, path))
    return sw.is_stable()


__all__ = [
    "compute_stability",
    "is_stable",
    "StabilityWindow",
    "DEFAULT_THRESHOLD",
]
