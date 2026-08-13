"""Audio source validation.

Classification (file vs URL) lives in :class:`~aytgcalls.types.AudioSource`; this module
answers the harder question — *can FFmpeg actually decode it?* — with a cheap probe that
never downloads the whole thing.
"""

from __future__ import annotations

import asyncio
import os
from urllib.parse import urlparse

from ..exceptions import FFmpegError, InvalidAudioSource, MediaSourceError
from ..logger import get_logger
from ..types import AudioSource, SourceKind

logger = get_logger("media.source")

__all__ = ["validate_source", "probe_source", "SUPPORTED_HINT_EXTENSIONS"]

#: Purely informational — FFmpeg decides what is really playable.
SUPPORTED_HINT_EXTENSIONS = frozenset(
    {
        ".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".oga", ".opus",
        ".webm", ".mka", ".mp4", ".mkv", ".wma", ".aiff", ".alac", ".ts",
    }
)


def validate_source(value: object) -> AudioSource:
    """Coerce and statically validate a source.

    :raises InvalidAudioSource: local file missing/unreadable, or an unusable URL scheme.
    """
    source = AudioSource.from_any(value)
    if not source.uri:
        raise InvalidAudioSource("Empty audio source")

    if source.kind is SourceKind.URL:
        parsed = urlparse(source.uri)
        if parsed.scheme not in {"http", "https"}:
            raise InvalidAudioSource(
                f"Unsupported URL scheme {parsed.scheme!r}; only http and https are accepted."
            )
        if not parsed.netloc:
            raise InvalidAudioSource(f"URL has no host: {source.uri!r}")
        return source

    if source.kind is SourceKind.RAW_PCM:
        return source

    path = source.uri
    if not os.path.exists(path):
        raise InvalidAudioSource(f"File not found: {path!r}")
    if os.path.isdir(path):
        raise InvalidAudioSource(f"{path!r} is a directory, not an audio file")
    if not os.access(path, os.R_OK):
        raise InvalidAudioSource(f"File is not readable: {path!r}")
    if os.path.getsize(path) == 0:
        raise InvalidAudioSource(f"File is empty: {path!r}")
    return source


async def probe_source(
    source: AudioSource,
    *,
    binary: str = "ffmpeg",
    timeout: float = 15.0,
    probe_bytes: int = 32_000,
    http_fetch: bool = True,
    http_headers: dict[str, str] | None = None,
) -> AudioSource:
    """Confirm FFmpeg can decode ``source`` by decoding a fraction of a second of it.

    :raises MediaSourceError: FFmpeg produced no audio.
    """
    from .ffmpeg import FFmpegProcess  # noqa: PLC0415  (circular at import time)

    process = FFmpegProcess(
        source, binary=binary, http_fetch=http_fetch, http_headers=http_headers
    )
    try:
        await process.start()
        data = await asyncio.wait_for(process.read(probe_bytes), timeout=timeout)
    except asyncio.TimeoutError as exc:
        raise MediaSourceError(
            f"Timed out after {timeout:g}s probing {source.display_name!r}."
        ) from exc
    except FFmpegError as exc:
        raise MediaSourceError(f"FFmpeg cannot decode {source.display_name!r}: {exc}") from exc
    finally:
        await process.stop()

    if not data:
        raise MediaSourceError(
            f"FFmpeg produced no audio for {source.display_name!r}. "
            f"stderr:\n{process.stderr_text}"
        )
    logger.debug("Probed %s: %d PCM bytes", source.display_name, len(data))
    return source
