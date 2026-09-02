"""Media metadata probing: duration, size, and whether byte-range seeking is possible.

Used by :meth:`~aytgcalls.player.player.Player.seek` to decide *how* to seek:

* local file  -> FFmpeg ``-ss`` (fast and accurate, the container index is available)
* URL in a **self-framing** format (MP3, ADTS AAC, MPEG-TS) that advertises
  ``Accept-Ranges`` + a length -> HTTP ``Range`` byte offset, so we do not download and
  discard everything before the seek point
* URL in a header-dependent format (WAV, FLAC, OGG/Opus, M4A/MP4, WebM) -> ``-ss`` on the
  piped stream. A raw byte offset would cut away the header the decoder needs, so this
  path deliberately trades speed for correctness.
* live radio (no discoverable duration) -> not seekable at all

Duration comes from PyAV rather than a subprocess: PyAV links its own FFmpeg libraries, so
it works even where a statically linked ``ffmpeg`` binary cannot resolve DNS.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass

from ..logger import get_logger
from ..types import AudioSource, SourceKind

logger = get_logger("media.metadata")

__all__ = ["MediaInfo", "probe_media_info", "RESYNCABLE_FORMATS"]

#: Container/demuxer names whose bitstream can be decoded starting at an arbitrary byte
#: offset. These formats repeat a sync word in every frame, so the decoder resyncs on its
#: own. Everything else keeps essential setup data in a leading header (RIFF chunk, OGG
#: identification packets, MP4 ``moov`` atom, FLAC STREAMINFO) and cannot be byte-sliced.
RESYNCABLE_FORMATS = frozenset(
    {"mp3", "mp2", "mp1", "mpeg", "mpegts", "aac", "adts", "ac3", "eac3", "dts", "mpegaudio"}
)


@dataclass(frozen=True)
class MediaInfo:
    """What we could learn about a source without downloading it."""

    duration: float | None = None
    size: int | None = None
    accepts_ranges: bool = False
    #: FFmpeg demuxer name, e.g. ``"mp3"``, ``"wav"``, ``"ogg"``.
    format: str | None = None

    @property
    def is_live(self) -> bool:
        """No duration means an endless stream (internet radio) as far as we can tell."""
        return self.duration is None

    @property
    def is_resyncable(self) -> bool:
        """Whether the bitstream can be decoded from an arbitrary byte offset."""
        if not self.format:
            return False
        names = {part.strip().lower() for part in self.format.split(",")}
        return bool(names & RESYNCABLE_FORMATS)

    @property
    def can_byte_seek(self) -> bool:
        """Whether an HTTP ``Range`` seek would produce decodable audio."""
        return bool(
            self.accepts_ranges and self.size and self.duration and self.is_resyncable
        )

    def byte_offset_for(self, position: float) -> int | None:
        """Estimate the byte offset of ``position`` seconds.

        Exact for constant-bitrate media and close enough for VBR that the decoder resyncs
        within a frame or two. Returns ``None`` when a byte seek would be wrong — either
        the server does not support ranges, the length/duration is unknown, or the format
        needs its leading header (see :data:`RESYNCABLE_FORMATS`).
        """
        if not self.can_byte_seek:
            return None
        if position <= 0:
            return 0
        assert self.size is not None and self.duration is not None
        ratio = min(max(position / self.duration, 0.0), 1.0)
        return int(self.size * ratio)


def _probe_file_blocking(path: str, timeout: float) -> tuple[float | None, str | None, int | None]:
    """Duration, demuxer name and size for a local file — all blocking work in one thread."""
    duration, fmt = _probe_container(path, timeout)
    try:
        size: int | None = os.path.getsize(path)
    except OSError:
        size = None
    return duration, fmt, size


def _probe_container(target: str, timeout: float) -> tuple[float | None, str | None]:
    """Return ``(duration_seconds, demuxer_name)`` using PyAV.

    PyAV links its own FFmpeg libraries, so this works even where a statically linked
    ``ffmpeg`` binary cannot resolve DNS.
    """
    try:
        import av

        with av.open(target, timeout=timeout) as container:
            name = getattr(container.format, "name", None)
            duration = container.duration
            if duration:
                return round(duration / 1_000_000, 3), name
            for stream in container.streams:
                if stream.duration and stream.time_base:
                    return round(float(stream.duration * stream.time_base), 3), name
            return None, name
    except Exception as exc:
        logger.debug("container probe failed for %s: %s", target, exc)
    return None, None


async def _probe_http(url: str, timeout: float) -> tuple[int | None, bool]:
    """Return ``(content_length, accepts_ranges)`` using a HEAD, falling back to a 1-byte GET."""
    try:
        import aiohttp

        client_timeout = aiohttp.ClientTimeout(total=timeout)
        async with aiohttp.ClientSession(timeout=client_timeout) as session:
            try:
                async with session.head(url, allow_redirects=True) as response:
                    length = response.content_length
                    ranges = response.headers.get("Accept-Ranges", "").lower()
                    if length:
                        return length, ranges == "bytes"
            except aiohttp.ClientError as exc:
                logger.debug("HEAD failed for %s: %s", url, exc)
            # Some servers reject HEAD; a Range GET tells us both things at once.
            async with session.get(
                url, headers={"Range": "bytes=0-0"}, allow_redirects=True
            ) as response:
                if response.status == 206:
                    content_range = response.headers.get("Content-Range", "")
                    total = content_range.rsplit("/", 1)[-1]
                    return (int(total) if total.isdigit() else None), True
                return response.content_length, False
    except Exception as exc:
        logger.debug("range probe failed for %s: %s", url, exc)
    return None, False


async def probe_media_info(source: AudioSource, *, timeout: float = 8.0) -> MediaInfo:
    """Best-effort metadata. Never raises — unknown values come back as ``None``/``False``."""
    if source.kind is SourceKind.FILE:
        duration, fmt, size = await asyncio.to_thread(_probe_file_blocking, source.uri, timeout)
        # Local files are always seekable, but we use FFmpeg's -ss for them anyway.
        return MediaInfo(duration=duration, size=size, accepts_ranges=False, format=fmt)

    if source.kind is SourceKind.URL:
        container_task = asyncio.create_task(
            asyncio.to_thread(_probe_container, source.uri, timeout)
        )
        http_task = asyncio.create_task(_probe_http(source.uri, timeout))
        (duration, fmt), (size, ranges) = await asyncio.gather(container_task, http_task)
        info = MediaInfo(duration=duration, size=size, accepts_ranges=ranges, format=fmt)
        logger.debug(
            "probed %s: duration=%s size=%s format=%s ranges=%s byte_seek=%s",
            source.display_name, info.duration, info.size, info.format,
            info.accepts_ranges, info.can_byte_seek,
        )
        return info

    return MediaInfo()
