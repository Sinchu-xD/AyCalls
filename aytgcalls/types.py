"""Shared dataclasses and enums."""

from __future__ import annotations

import enum
import os
import uuid
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

__all__ = [
    "SAMPLE_RATE",
    "CHANNELS",
    "SAMPLE_WIDTH",
    "FRAME_MS",
    "SAMPLES_PER_FRAME",
    "BYTES_PER_FRAME",
    "LoopMode",
    "PlaybackState",
    "SourceKind",
    "AudioSource",
    "TrackInfo",
    "StreamEndReason",
    "DisconnectReason",
    "CallStats",
]

#: Telegram group calls are always 48 kHz stereo Opus with 20 ms frames.
SAMPLE_RATE = 48_000
CHANNELS = 2
SAMPLE_WIDTH = 2  # s16le
FRAME_MS = 20
SAMPLES_PER_FRAME = SAMPLE_RATE * FRAME_MS // 1000  # 960 samples per channel
BYTES_PER_FRAME = SAMPLES_PER_FRAME * CHANNELS * SAMPLE_WIDTH  # 3840 bytes


class LoopMode(enum.Enum):
    """Queue looping behaviour."""

    OFF = "off"
    TRACK = "track"
    QUEUE = "queue"

    @classmethod
    def from_any(cls, value: Any) -> LoopMode:
        """Accept a :class:`LoopMode`, or a friendly string/bool from a chat command."""
        if isinstance(value, cls):
            return value
        if isinstance(value, bool):
            return cls.QUEUE if value else cls.OFF
        text = str(value).strip().lower()
        aliases = {
            "off": cls.OFF, "none": cls.OFF, "no": cls.OFF, "0": cls.OFF, "disable": cls.OFF,
            "track": cls.TRACK, "one": cls.TRACK, "single": cls.TRACK, "song": cls.TRACK,
            "current": cls.TRACK, "1": cls.TRACK,
            "queue": cls.QUEUE, "all": cls.QUEUE, "playlist": cls.QUEUE, "yes": cls.QUEUE,
            "on": cls.QUEUE,
        }
        if text not in aliases:
            raise ValueError(
                f"Unknown loop mode {value!r}. Use one of: off, track, queue."
            )
        return aliases[text]


class PlaybackState(enum.Enum):
    """Player state machine states."""

    IDLE = "idle"
    PLAYING = "playing"
    PAUSED = "paused"
    STOPPED = "stopped"


class SourceKind(enum.Enum):
    """Where the audio bytes come from."""

    FILE = "file"
    URL = "url"
    RAW_PCM = "raw_pcm"


class StreamEndReason(enum.Enum):
    """Why a track stopped."""

    COMPLETED = "completed"
    SKIPPED = "skipped"
    STOPPED = "stopped"
    ERROR = "error"


class DisconnectReason(enum.Enum):
    """Why the call ended."""

    REQUESTED = "requested"
    CALL_ENDED = "call_ended"
    KICKED = "kicked"
    TRANSPORT_FAILED = "transport_failed"
    SFU_TIMEOUT = "sfu_timeout"


@dataclass(frozen=True)
class AudioSource:
    """A validated, playable audio source.

    Build one with :meth:`from_any`; the constructor performs no validation so that
    tests can build synthetic sources.
    """

    uri: str
    kind: SourceKind
    title: str | None = None
    #: Extra arguments injected before ``-i`` on the ffmpeg command line.
    ffmpeg_input_args: tuple[str, ...] = ()
    #: Seek position in seconds applied via ``-ss``.
    start_at: float = 0.0
    #: Byte offset to start streaming from (URL sources seeked via HTTP ``Range``).
    byte_offset: int = 0
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12], compare=False)

    @property
    def display_name(self) -> str:
        if self.title:
            return self.title
        if self.kind is SourceKind.FILE:
            return os.path.basename(self.uri)
        return self.uri

    @classmethod
    def from_any(cls, value: Any, **kwargs: Any) -> AudioSource:
        """Coerce ``value`` (str / PathLike / AudioSource) into an :class:`AudioSource`.

        Validation of existence/decodability lives in :mod:`aytgcalls.media.source`;
        this only classifies the URI.
        """
        if isinstance(value, AudioSource):
            return value
        uri = os.fspath(value) if hasattr(value, "__fspath__") else str(value)
        scheme = urlparse(uri).scheme.lower()
        # Anything with a real URI scheme is a URL; a single-character "scheme" is a
        # Windows drive letter, and no scheme at all is a plain path. Validation of
        # *which* schemes are supported happens in aytgcalls.media.source.
        kind = SourceKind.URL if len(scheme) > 1 else SourceKind.FILE
        return cls(uri=uri, kind=kind, **kwargs)

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return self.display_name


@dataclass(frozen=True)
class TrackInfo:
    """A snapshot of what is playing, for ``/now`` style commands."""

    source: AudioSource | None
    state: PlaybackState
    position: float = 0.0
    duration: float | None = None
    volume: float = 100.0
    loop: LoopMode = LoopMode.OFF
    queued: int = 0
    is_live: bool = False

    @property
    def title(self) -> str:
        return self.source.display_name if self.source else "nothing"

    @property
    def progress(self) -> float | None:
        """Fraction played, 0..1, or ``None`` when the duration is unknown."""
        if not self.duration:
            return None
        return min(max(self.position / self.duration, 0.0), 1.0)

    @staticmethod
    def format_time(seconds: float | None) -> str:
        """``93.4`` -> ``'01:33'``; ``None`` -> ``'--:--'``."""
        if seconds is None:
            return "--:--"
        seconds = max(0, int(seconds))
        hours, rest = divmod(seconds, 3600)
        minutes, secs = divmod(rest, 60)
        return f"{hours:d}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"

    def progress_bar(self, width: int = 20) -> str:
        """A text progress bar, handy for a chat reply."""
        fraction = self.progress
        if fraction is None:
            return "🔴 LIVE"
        filled = int(fraction * width)
        return "▬" * filled + "🔘" + "▬" * max(0, width - filled - 1)

    def __str__(self) -> str:
        if self.source is None:
            return "nothing is playing"
        bar = self.progress_bar()
        return (
            f"{self.title} [{self.state.value}] "
            f"{self.format_time(self.position)} / {self.format_time(self.duration)} {bar}"
        )


@dataclass
class CallStats:
    """Snapshot of transport health, used by the live check and by callers."""

    packets_sent: int = 0
    bytes_sent: int = 0
    frames_encoded: int = 0
    frames_silence: int = 0
    underruns: int = 0
    ice_state: str = "new"
    dtls_state: str = "new"

    def as_dict(self) -> dict[str, Any]:
        return {
            "packets_sent": self.packets_sent,
            "bytes_sent": self.bytes_sent,
            "frames_encoded": self.frames_encoded,
            "frames_silence": self.frames_silence,
            "underruns": self.underruns,
            "ice_state": self.ice_state,
            "dtls_state": self.dtls_state,
        }
