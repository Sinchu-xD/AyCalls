"""Configuration objects, loaded from environment variables.

Credentials are **never** hardcoded and never logged. Everything comes from the
environment (or is passed explicitly by the caller).
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field, fields
from typing import Any

from .exceptions import FFmpegNotInstalled
from .types import FRAME_MS

__all__ = ["TelegramCredentials", "CallConfig", "env_flag", "AyConfig", "AyCreds"]


def env_flag(name: str, default: bool = False) -> bool:
    """Read a boolean-ish environment variable."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str) -> int | None:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"Environment variable {name}={raw!r} is not an integer") from exc


@dataclass
class TelegramCredentials:
    """Credentials for the *user* (assistant) session.

    Never hardcode these. Populate them from the environment with :meth:`from_env`.
    """

    api_id: int | None = None
    api_hash: str | None = None
    session_string: str | None = None
    #: Optional; only for the command interface, never for joining a call.
    bot_token: str | None = None

    @classmethod
    def from_env(cls, prefix: str = "") -> TelegramCredentials:
        """Read ``API_ID``, ``API_HASH``, ``STRING_SESSION`` (and ``BOT_TOKEN``)."""
        return cls(
            api_id=_env_int(f"{prefix}API_ID"),
            api_hash=os.environ.get(f"{prefix}API_HASH") or None,
            session_string=(
                os.environ.get(f"{prefix}STRING_SESSION")
                or os.environ.get(f"{prefix}SESSION_STRING")
                or None
            ),
            bot_token=os.environ.get(f"{prefix}BOT_TOKEN") or None,
        )

    @property
    def is_complete(self) -> bool:
        return bool(self.api_id and self.api_hash and self.session_string)

    def require(self) -> TelegramCredentials:
        missing = [
            name
            for name, value in (
                ("API_ID", self.api_id),
                ("API_HASH", self.api_hash),
                ("STRING_SESSION", self.session_string),
            )
            if not value
        ]
        if missing:
            raise ValueError(
                "Missing required environment variables: "
                + ", ".join(missing)
                + ". See README -> Generating a STRING_SESSION."
            )
        return self

    def __repr__(self) -> str:
        return (
            "TelegramCredentials(api_id="
            f"{'set' if self.api_id else 'unset'}, api_hash="
            f"{'set' if self.api_hash else 'unset'}, session_string="
            f"{'set' if self.session_string else 'unset'}, bot_token="
            f"{'set' if self.bot_token else 'unset'})"
        )


@dataclass
class CallConfig:
    """Tunables for a single :class:`~aytgcalls.call.group_call.GroupCall`."""

    # --- media ---------------------------------------------------------------
    ffmpeg_path: str = "ffmpeg"
    #: Opus target bitrate in bits/s. Telegram is happy in the 64–128 kbps range.
    opus_bitrate: int = 96_000
    #: Milliseconds of PCM to keep buffered ahead of the sender.
    buffer_ms: int = 400
    #: Milliseconds of PCM to accumulate before the first frame is released.
    prefetch_ms: int = 100
    #: Initial playback volume, in percent (100 == unity gain).
    volume: int = 100
    #: Fetch http(s) sources with aiohttp and pipe them into FFmpeg's stdin instead of
    #: letting FFmpeg open the URL. Required on FFmpeg builds without a working resolver
    #: (static builds), and gives resumable Range retries plus custom headers.
    fetch_urls_with_python: bool = False
    #: Extra HTTP headers for URL sources (e.g. Referer, Authorization, Cookie).
    http_headers: dict[str, str] | None = None

    # --- transport -----------------------------------------------------------
    #: STUN/TURN servers. Empty by default: Telegram's SFU has a public IP and
    #: behaves as an ICE-lite peer, so host candidates are sufficient.
    ice_servers: tuple[str, ...] = ()
    #: Seconds to wait for ICE + DTLS to come up.
    connect_timeout: float = 20.0

    # --- automation ----------------------------------------------------------
    #: ``play()`` joins the voice chat by itself when it is not joined yet.
    auto_join: bool = True
    #: Leave the voice chat automatically once the queue runs out.
    auto_leave: bool = True
    #: Grace period before auto-leaving, so a quick follow-up request keeps the call.
    auto_leave_delay: float = 3.0
    #: Where Telegram media is downloaded to. ``None`` uses a temporary directory.
    download_dir: str | None = None

    # --- signaling -----------------------------------------------------------
    #: Seconds between ``phone.CheckGroupCall`` keepalives.
    keepalive_interval: float = 10.0
    #: Join muted (listener mode). Publishers join unmuted.
    join_muted: bool = False

    # --- reconnect -----------------------------------------------------------
    auto_reconnect: bool = True
    reconnect_max_attempts: int = 8
    reconnect_initial_delay: float = 1.0
    reconnect_max_delay: float = 30.0
    reconnect_jitter: float = 0.3

    # --- misc ----------------------------------------------------------------
    debug_signaling: bool = field(default_factory=lambda: env_flag("AYTGCALLS_DEBUG"))

    @classmethod
    def from_env(cls, **overrides: Any) -> CallConfig:
        """Build a config from ``AYTGCALLS_*`` environment variables plus overrides."""
        env_map: dict[str, Any] = {}
        if (path := os.environ.get("AYTGCALLS_FFMPEG")) is not None:
            env_map["ffmpeg_path"] = path
        for env_name, attr, caster in (
            ("AYTGCALLS_OPUS_BITRATE", "opus_bitrate", int),
            ("AYTGCALLS_BUFFER_MS", "buffer_ms", int),
            ("AYTGCALLS_PREFETCH_MS", "prefetch_ms", int),
            ("AYTGCALLS_VOLUME", "volume", int),
            ("AYTGCALLS_CONNECT_TIMEOUT", "connect_timeout", float),
            ("AYTGCALLS_KEEPALIVE_INTERVAL", "keepalive_interval", float),
        ):
            raw = os.environ.get(env_name)
            if raw:
                env_map[attr] = caster(raw)
        if (servers := os.environ.get("AYTGCALLS_ICE_SERVERS")):
            env_map["ice_servers"] = tuple(s.strip() for s in servers.split(",") if s.strip())
        env_map.update(overrides)
        known = {f.name for f in fields(cls)}
        unknown = set(env_map) - known
        if unknown:
            raise TypeError(f"Unknown CallConfig fields: {sorted(unknown)}")
        return cls(**env_map)

    def __post_init__(self) -> None:
        if self.buffer_ms < FRAME_MS * 2:
            raise ValueError(f"buffer_ms must be at least {FRAME_MS * 2}")
        if self.prefetch_ms > self.buffer_ms:
            raise ValueError("prefetch_ms cannot exceed buffer_ms")
        if not 0 <= self.volume <= 200:
            raise ValueError("volume must be between 0 and 200")
        if not 6_000 <= self.opus_bitrate <= 510_000:
            raise ValueError("opus_bitrate must be between 6000 and 510000")

    def resolve_ffmpeg(self) -> str:
        """Return an absolute path to the ffmpeg binary or raise :class:`FFmpegNotInstalled`."""
        found = shutil.which(self.ffmpeg_path)
        if found is None:
            if os.path.isfile(self.ffmpeg_path) and os.access(self.ffmpeg_path, os.X_OK):
                return self.ffmpeg_path
            raise FFmpegNotInstalled(self.ffmpeg_path)
        return found


#: Branded aliases.
AyConfig = CallConfig
AyCreds = TelegramCredentials
