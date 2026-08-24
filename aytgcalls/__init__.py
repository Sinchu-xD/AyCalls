"""aytgcalls — play audio into Telegram group voice chats.

Kurigram (MTProto) for signaling, aiortc (ICE/DTLS-SRTP/RTP) for media, FFmpeg for
decoding. No py-tgcalls, no tgcalls, no Telethon.

    from aytgcalls import AyClient, AyCreds

    call = AyClient(AyCreds.from_env())
    await call.start()

    # all controls take chat_id first
    await call.play(-1001234567890, "song.mp3")
    await call.pause(-1001234567890)
    await call.resume(-1001234567890)
    await call.seek(-1001234567890, 45)
    await call.skip(-1001234567890)
    await call.end(-1001234567890)
    await call.stop()
"""

from __future__ import annotations

from .config import AyConfig, AyCreds, CallConfig, TelegramCredentials
from .exceptions import (
    AlreadyJoined,
    AlreadyPlaying,
    AytgcallsError,
    BotClientNotAllowed,
    DTLSHandshakeFailed,
    FFmpegError,
    FFmpegNotInstalled,
    GroupCallNotFound,
    ICEFailed,
    InvalidAudioSource,
    MediaSourceError,
    NotInGroup,
    NotJoined,
    NotPlaying,
    OpusError,
    TelegramCallError,
    TransportError,
)
from .logger import enable_debug, get_logger
from .types import (
    BYTES_PER_FRAME,
    CHANNELS,
    FRAME_MS,
    SAMPLE_RATE,
    SAMPLES_PER_FRAME,
    AudioSource,
    AyDisconnectReason,
    AyEndReason,
    AyKind,
    AyLoop,
    AySource,
    AyState,
    AyStats,
    AyTrack,
    CallStats,
    DisconnectReason,
    LoopMode,
    PlaybackState,
    SourceKind,
    StreamEndReason,
    TrackInfo,
)

__version__ = "0.2.0"


def __getattr__(name: str) -> object:
    """Lazily expose the call classes.

    ``GroupCall`` pulls in aiortc and (indirectly) kurigram. Importing them lazily keeps
    ``import aytgcalls`` cheap and lets the SDP/media layers be used on a machine without
    an MTProto stack installed.
    """
    if name in ("AyCall", "GroupCall"):
        from .call.group_call import GroupCall  # noqa: PLC0415

        return GroupCall
    if name in ("AyFac", "GroupCallFactory"):
        from .call.factory import GroupCallFactory  # noqa: PLC0415

        return GroupCallFactory
    if name == "AyClient":
        from .client import AyClient  # noqa: PLC0415

        return AyClient
    if name in ("AyPlayer", "Player"):
        from .player.player import Player  # noqa: PLC0415

        return Player
    if name in ("AyQueue", "TrackQueue"):
        from .player.queue import TrackQueue  # noqa: PLC0415

        return TrackQueue
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "__version__",
    # --- the branded API: this is what you import ---
    "AyCall",
    "AyFac",
    "AyPlayer",
    "AyQueue",
    "AyConfig",
    "AyCreds",
    "AyLoop",
    "AyState",
    "AySource",
    "AyTrack",
    "AyStats",
    "AyKind",
    "AyEndReason",
    "AyDisconnectReason",
    # --- descriptive aliases, all still valid ---
    "GroupCall",
    "GroupCallFactory",
    "AyClient",
    "Player",
    "TrackQueue",
    # config
    "CallConfig",
    "TelegramCredentials",
    # types
    "AudioSource",
    "CallStats",
    "DisconnectReason",
    "LoopMode",
    "PlaybackState",
    "SourceKind",
    "StreamEndReason",
    "TrackInfo",
    "SAMPLE_RATE",
    "CHANNELS",
    "FRAME_MS",
    "SAMPLES_PER_FRAME",
    "BYTES_PER_FRAME",
    # logging
    "enable_debug",
    "get_logger",
    # exceptions
    "AytgcallsError",
    "BotClientNotAllowed",
    "GroupCallNotFound",
    "NotInGroup",
    "AlreadyJoined",
    "NotJoined",
    "AlreadyPlaying",
    "NotPlaying",
    "InvalidAudioSource",
    "MediaSourceError",
    "FFmpegError",
    "FFmpegNotInstalled",
    "OpusError",
    "TransportError",
    "ICEFailed",
    "DTLSHandshakeFailed",
    "TelegramCallError",
]
