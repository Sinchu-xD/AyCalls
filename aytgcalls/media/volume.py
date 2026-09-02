"""PCM gain.

Applied to s16le frames before they reach the Opus encoder. ``audioop`` was removed in
Python 3.13, so this uses numpy when available and falls back to the stdlib ``array``
module otherwise. Both paths saturate instead of wrapping around, which is the whole
point: a wrap produces a loud click, a clip merely distorts.
"""

from __future__ import annotations

import array
import sys

from ..logger import get_logger

logger = get_logger("media.volume")

__all__ = ["percent_to_gain", "apply_gain", "telegram_volume", "MAX_VOLUME_PERCENT"]

MAX_VOLUME_PERCENT = 200
_INT16_MIN = -32768
_INT16_MAX = 32767

try:
    import numpy as _np
except ImportError:
    _np = None


def percent_to_gain(percent: float) -> float:
    """Convert a 0..200 % volume into a linear multiplier (100 % == 1.0)."""
    if percent < 0:
        raise ValueError("Volume percent cannot be negative")
    if percent > MAX_VOLUME_PERCENT:
        raise ValueError(f"Volume percent cannot exceed {MAX_VOLUME_PERCENT}")
    return percent / 100.0


def apply_gain(pcm: bytes, gain: float) -> bytes:
    """Scale 16-bit little-endian samples by ``gain``, saturating at the int16 bounds."""
    if gain == 1.0 or not pcm:
        return pcm
    if gain == 0.0:
        return b"\x00" * len(pcm)
    if len(pcm) % 2:
        # Never split a sample: leave the stray byte untouched.
        return apply_gain(pcm[:-1], gain) + pcm[-1:]

    if _np is not None:
        samples = _np.frombuffer(pcm, dtype="<i2").astype(_np.float32) * gain
        _np.clip(samples, _INT16_MIN, _INT16_MAX, out=samples)
        return samples.astype("<i2").tobytes()

    samples = array.array("h")
    samples.frombytes(pcm)
    if sys.byteorder == "big":
        samples.byteswap()
    for index, value in enumerate(samples):
        scaled = int(value * gain)
        if scaled > _INT16_MAX:
            scaled = _INT16_MAX
        elif scaled < _INT16_MIN:
            scaled = _INT16_MIN
        samples[index] = scaled
    if sys.byteorder == "big":
        samples.byteswap()
    return samples.tobytes()


def telegram_volume(percent: float) -> int:
    """Map a 0..200 % volume onto Telegram's server-side scale (10000 == 100 %)."""
    return max(0, min(20_000, int(round(percent * 100))))
