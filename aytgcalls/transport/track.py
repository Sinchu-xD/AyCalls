"""The outgoing audio track.

This is where the 20 ms wall-clock pacing lives. aiortc's :class:`RTCRtpSender` pulls
frames as fast as ``recv()`` yields them, so the track *is* the pacer: it releases one
960-sample stereo frame every 20 ms, drift-corrected against a monotonic clock.

When the player is paused, between tracks, or the buffer underruns, the track emits
**silence** instead of stalling. Stopping the RTP flow makes Telegram's SFU treat the
source as dead, so the stream must never go quiet at the packet level.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from fractions import Fraction
from typing import TYPE_CHECKING

from aiortc.mediastreams import MediaStreamError, MediaStreamTrack

from ..logger import get_logger
from ..types import BYTES_PER_FRAME, CHANNELS, FRAME_MS, SAMPLE_RATE, SAMPLES_PER_FRAME, CallStats

if TYPE_CHECKING:
    from av import AudioFrame

logger = get_logger("transport.track")

__all__ = ["FrameProvider", "PcmStreamTrack", "SILENCE_FRAME"]

#: One frame of 48 kHz stereo s16 silence.
SILENCE_FRAME = b"\x00" * BYTES_PER_FRAME

#: ``async () -> bytes | None``. Must return exactly ``BYTES_PER_FRAME`` bytes,
#: a short final chunk, or ``None`` when nothing is available right now.
FrameProvider = Callable[[], Awaitable[bytes | None]]

_TIME_BASE = Fraction(1, SAMPLE_RATE)


class PcmStreamTrack(MediaStreamTrack):
    """A ``sendonly`` audio track fed by a pluggable PCM frame provider."""

    kind = "audio"

    def __init__(
        self,
        provider: FrameProvider | None = None,
        *,
        stats: CallStats | None = None,
        frame_ms: int = FRAME_MS,
    ) -> None:
        super().__init__()
        self._provider = provider
        self._stats = stats if stats is not None else CallStats()
        self._frame_ms = frame_ms
        self._frame_seconds = frame_ms / 1000.0
        self._samples = SAMPLE_RATE * frame_ms // 1000
        self._frame_bytes = self._samples * CHANNELS * 2
        self._silence = b"\x00" * self._frame_bytes
        self._pts = 0
        self._deadline: float | None = None
        self._closed = False
        #: Set when a real (non-silence) frame has been sent at least once.
        self.started = asyncio.Event()

    # -- wiring -------------------------------------------------------------------

    def set_provider(self, provider: FrameProvider | None) -> None:
        """Swap the frame source (used when the player starts/stops)."""
        self._provider = provider

    @property
    def stats(self) -> CallStats:
        return self._stats

    # -- pacing -------------------------------------------------------------------

    async def _pace(self) -> None:
        """Sleep until this frame's slot, correcting for accumulated drift."""
        now = time.monotonic()
        if self._deadline is None:
            self._deadline = now
        self._deadline += self._frame_seconds
        delay = self._deadline - now
        if delay > 0:
            await asyncio.sleep(delay)
        elif delay < -0.5:
            # Drifted more than 500 ms behind — likely a GC pause or a blocked loop.
            # Resync instead of flushing a burst of catch-up packets.
            logger.warning("Sender drifted %.0f ms; resyncing clock", -delay * 1000)
            self._deadline = time.monotonic()

    # -- MediaStreamTrack -----------------------------------------------------------

    async def recv(self) -> AudioFrame:
        if self._closed or self.readyState != "live":
            raise MediaStreamError("Track is not live")

        await self._pace()

        payload: bytes | None = None
        if self._provider is not None:
            try:
                payload = await self._provider()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Frame provider raised; emitting silence")
                payload = None

        if not payload:
            payload = self._silence
            self._stats.frames_silence += 1
            if self._provider is not None:
                self._stats.underruns += 1
        else:
            if len(payload) < self._frame_bytes:
                payload = payload + self._silence[len(payload) :]
            elif len(payload) > self._frame_bytes:
                payload = payload[: self._frame_bytes]
            self._stats.frames_encoded += 1
            if not self.started.is_set():
                self.started.set()

        return self._build_frame(payload)

    def _build_frame(self, payload: bytes) -> AudioFrame:
        from av import AudioFrame  # local import: av is heavy

        frame = AudioFrame(format="s16", layout="stereo", samples=self._samples)
        frame.planes[0].update(payload)
        frame.sample_rate = SAMPLE_RATE
        frame.time_base = _TIME_BASE
        frame.pts = self._pts
        self._pts += self._samples
        return frame

    def stop(self) -> None:
        self._closed = True
        self._provider = None
        super().stop()


def frames_from_pcm(data: bytes, *, frame_bytes: int = BYTES_PER_FRAME) -> list[bytes]:
    """Split a PCM buffer into whole frames, zero-padding the tail.

    Small helper used by tests and by the raw-PCM source path.
    """
    frames: list[bytes] = []
    for offset in range(0, len(data), frame_bytes):
        chunk = data[offset : offset + frame_bytes]
        if len(chunk) < frame_bytes:
            chunk = chunk + b"\x00" * (frame_bytes - len(chunk))
        frames.append(chunk)
    return frames


assert SAMPLES_PER_FRAME * CHANNELS * 2 == BYTES_PER_FRAME  # invariant guard
