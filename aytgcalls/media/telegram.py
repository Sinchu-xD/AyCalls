"""Turn Telegram media into something FFmpeg can play.

``play()`` accepts more than paths and URLs: hand it a Kurigram ``Message`` (a voice note,
an audio track, a document, a video) or any media object carrying a ``file_id`` and it is
downloaded through the same MTProto session that is already in the call, then played like
any other file.

Downloads land in a temporary directory and are deleted as soon as the track finishes, so
nothing accumulates on disk.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from typing import TYPE_CHECKING, Any

from ..exceptions import InvalidAudioSource, MediaSourceError
from ..logger import get_logger
from ..types import AudioSource, SourceKind

if TYPE_CHECKING:
    from pyrogram import Client

logger = get_logger("media.telegram")

__all__ = [
    "is_telegram_media",
    "extract_media",
    "extract_media_with_kind",
    "TelegramDownloader",
]

#: Message attributes that can carry playable audio, in priority order.
_MEDIA_ATTRS = ("voice", "audio", "document", "video", "video_note", "animation")


def extract_media_with_kind(source: Any) -> tuple[Any | None, str | None]:
    """Return ``(media, kind)`` for ``source``.

    ``kind`` is the *message attribute* the media came from (``"voice"``, ``"audio"``…),
    which is a far more reliable label than the media object's class name.
    """
    if source is None or isinstance(source, (str, bytes, os.PathLike, AudioSource)):
        return None, None
    for attribute in _MEDIA_ATTRS:
        media = getattr(source, attribute, None)
        if media is not None and getattr(media, "file_id", None):
            return media, attribute
    if getattr(source, "file_id", None):
        return source, None
    return None, None


def extract_media(source: Any) -> Any | None:
    """Return the downloadable media object inside ``source``, or ``None``.

    Accepts a ``Message`` (picks its voice/audio/document/video) or a media object that
    already has a ``file_id``.
    """
    return extract_media_with_kind(source)[0]


def is_telegram_media(source: Any) -> bool:
    """Whether ``source`` is a Telegram message/media that needs downloading."""
    return extract_media(source) is not None


def _size_or_none(path: str) -> int | None:
    """Blocking stat, meant to be run through :func:`asyncio.to_thread`."""
    try:
        return os.path.getsize(path)
    except OSError:
        return None


def _title_for(media: Any, kind: str | None = None) -> str:
    """A human label: real metadata first, then the message attribute it came from."""
    for attribute in ("title", "file_name"):
        value = getattr(media, attribute, None)
        if value:
            return str(value)
    performer = getattr(media, "performer", None)
    if performer:
        return str(performer)
    label = (kind or type(media).__name__).lower()
    return "voice message" if label in {"voice", "video_note"} else f"telegram {label}"


class TelegramDownloader:
    """Downloads Telegram media to a temp dir and cleans up after itself."""

    def __init__(self, client: Client, *, directory: str | None = None) -> None:
        self._client = client
        self._root = directory
        self._owns_root = directory is None
        self._files: dict[str, str] = {}

    @property
    def directory(self) -> str:
        if self._root is None:
            self._root = tempfile.mkdtemp(prefix="aytgcalls_")
        os.makedirs(self._root, exist_ok=True)
        return self._root

    async def resolve(self, source: Any) -> AudioSource:
        """Download ``source`` and return a playable :class:`AudioSource`.

        :raises InvalidAudioSource: ``source`` carries no downloadable media.
        :raises MediaSourceError: the download failed or produced nothing.
        """
        media, kind = extract_media_with_kind(source)
        if media is None:
            raise InvalidAudioSource(f"{type(source).__name__} carries no Telegram media")

        mime = str(getattr(media, "mime_type", "") or "")
        if mime and not mime.startswith(("audio/", "video/")):
            raise InvalidAudioSource(
                f"Telegram media has mime type {mime!r}, which is not audio or video."
            )

        title = _title_for(media, kind)
        logger.info("Downloading %s from Telegram…", title)
        try:
            # in_memory=False is explicit so the overload resolves to a file path result.
            result = await self._client.download_media(
                media, file_name=self.directory + os.sep, in_memory=False
            )
        except Exception as exc:
            raise MediaSourceError(f"Could not download {title!r} from Telegram: {exc}") from exc
        # download_media returns str | list[str] (and None if the download failed).
        downloaded: str | None
        if isinstance(result, list):
            downloaded = str(result[0]) if result else None
        else:
            downloaded = str(result) if result else None
        if not downloaded:
            raise MediaSourceError(f"Telegram download for {title!r} produced no file")

        path = downloaded
        size = await asyncio.to_thread(_size_or_none, path)
        if size is None:
            raise MediaSourceError(
                f"Telegram download for {title!r} produced no file at {path!r}"
            )
        logger.info("Downloaded %s (%.1f KiB)", title, size / 1024)
        track = AudioSource(uri=path, kind=SourceKind.FILE, title=title)
        self._files[track.id] = path
        return track

    def release(self, source: AudioSource) -> None:
        """Delete the temp file backing ``source``, if we downloaded it."""
        path = self._files.pop(source.id, None)
        if path and os.path.exists(path):
            try:
                os.remove(path)
                logger.debug("Removed temp download %s", path)
            except OSError as exc:
                logger.debug("Could not remove %s: %s", path, exc)

    def owns(self, source: AudioSource) -> bool:
        return source.id in self._files

    def cleanup(self) -> None:
        """Delete every remaining download (and the temp dir, if we made it)."""
        for path in list(self._files.values()):
            if os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass
        self._files.clear()
        if self._owns_root and self._root and os.path.isdir(self._root):
            shutil.rmtree(self._root, ignore_errors=True)
            self._root = None
