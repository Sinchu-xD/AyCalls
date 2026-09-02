"""Opus encoder wrapper (48 kHz, stereo, 20 ms frames).

In the live path, encoding happens inside aiortc's ``RTCRtpSender`` (which owns the RTP
sequence numbers and timestamps). This wrapper exists for the standalone paths — the
offline pipeline check, ``scripts/live_check.py``'s dry run, and the unit tests — and to
give callers a way to verify that libopus is actually available before joining a call.
"""

from __future__ import annotations

from fractions import Fraction
from typing import TYPE_CHECKING, Any

from ..exceptions import OpusError
from ..logger import get_logger
from ..types import BYTES_PER_FRAME, CHANNELS, SAMPLE_RATE, SAMPLES_PER_FRAME

if TYPE_CHECKING:
    from av import AudioFrame

logger = get_logger("media.opus")

__all__ = ["OpusEncoder", "opus_available"]

_TIME_BASE = Fraction(1, SAMPLE_RATE)


def opus_available() -> bool:
    """True when PyAV exposes a libopus encoder."""
    try:
        import av

        av.codec.Codec("libopus", "w")
    except Exception:
        return False
    return True


class OpusEncoder:
    """Encode 20 ms PCM frames into Opus packets."""

    def __init__(
        self,
        *,
        bitrate: int = 96_000,
        application: str = "audio",
        sample_rate: int = SAMPLE_RATE,
        channels: int = CHANNELS,
    ) -> None:
        if not 6_000 <= bitrate <= 510_000:
            raise OpusError(f"Opus bitrate {bitrate} is outside the 6000..510000 range")
        self.bitrate = bitrate
        self.sample_rate = sample_rate
        self.channels = channels
        self._pts = 0
        try:
            from av.audio.codeccontext import AudioCodecContext
            from av.codec import CodecContext

            context = CodecContext.create("libopus", "w")
            assert isinstance(context, AudioCodecContext)
            context.sample_rate = sample_rate
            context.format = "s16"
            context.layout = "stereo" if channels == 2 else "mono"
            context.bit_rate = bitrate
            context.time_base = _TIME_BASE
            context.options = {"application": application, "frame_duration": "20"}
            self._context: Any = context
        except Exception as exc:
            raise OpusError(
                "libopus is not available through PyAV. Install the Opus library "
                "(apt install libopus0 / brew install opus) and reinstall `av`."
            ) from exc

    # -- encoding -----------------------------------------------------------------------

    def encode(self, pcm: bytes) -> list[bytes]:
        """Encode one 20 ms PCM frame (3840 bytes) into Opus packet payloads."""
        if len(pcm) != BYTES_PER_FRAME:
            raise OpusError(
                f"Expected exactly {BYTES_PER_FRAME} bytes (20 ms stereo s16), got {len(pcm)}"
            )
        frame = self._make_frame(pcm)
        try:
            packets = self._context.encode(frame)
        except Exception as exc:
            raise OpusError(f"Opus encoding failed: {exc}") from exc
        return [bytes(packet) for packet in packets]

    def flush(self) -> list[bytes]:
        """Drain the encoder's internal delay."""
        try:
            return [bytes(packet) for packet in self._context.encode(None)]
        except Exception as exc:
            raise OpusError(f"Opus flush failed: {exc}") from exc

    def _make_frame(self, pcm: bytes) -> AudioFrame:
        from av import AudioFrame

        frame = AudioFrame(
            format="s16",
            layout="stereo" if self.channels == 2 else "mono",
            samples=SAMPLES_PER_FRAME,
        )
        frame.planes[0].update(pcm)
        frame.sample_rate = self.sample_rate
        frame.time_base = _TIME_BASE
        frame.pts = self._pts
        self._pts += SAMPLES_PER_FRAME
        return frame

    def close(self) -> None:
        context, self._context = self._context, None
        if context is not None:
            try:
                context.close()
            except Exception:
                pass

    def __enter__(self) -> OpusEncoder:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
