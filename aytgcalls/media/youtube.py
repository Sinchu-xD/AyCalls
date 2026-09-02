"""YouTube (and yt-dlp supported sites) URL resolution via YouTubeMusic.

When ``YouTubeMusic`` is installed, URLs are resolved to a direct audio stream URL
that FFmpeg can decode.  The ``YouTubeMusic`` package wraps yt-dlp with the correct
format selectors so it always returns a direct playable URL (never an HLS/DASH manifest).

Without YouTubeMusic, plain http/https URLs pass through unchanged.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
from urllib.parse import urlparse

from ..exceptions import MediaSourceError
from ..types import AudioSource, SourceKind

logger = logging.getLogger("media.youtube")

__all__ = ["resolve_url", "is_youtube_url", "is_ytdlp_url", "YOUTUBE_MUSIC_AVAILABLE"]

#: Well-known domains that YouTubeMusic / yt-dlp can handle.
_YT_DOMAINS: frozenset[str] = frozenset(
    {
        "youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be",
        "youtube-nocookie.com", "www.youtube-nocookie.com",
        "vimeo.com", "www.vimeo.com", "player.vimeo.com",
        "soundcloud.com", "www.soundcloud.com", "on.soundcloud.com",
        "twitch.tv", "www.twitch.tv", "m.twitch.tv",
        "dailymotion.com", "www.dailymotion.com", "dai.ly",
        "streamable.com", "www.streamable.com",
        "bilibili.com", "www.bilibili.com",
        "tiktok.com", "www.tiktok.com",
        "instagram.com", "www.instagram.com",
        "twitter.com", "x.com", "www.twitter.com", "www.x.com",
        "facebook.com", "www.facebook.com", "fb.watch",
        "reddit.com", "www.reddit.com", "v.redd.it",
        "streamtape.com", "streamsb.net", "dood.to", "dood.watch",
        "mixdrop.co", "upstream.to",
    }
)


def _cookie_path() -> str | None:
    """Return a cookie file path if one exists at the standard temp location."""
    path = os.path.join(tempfile.gettempdir(), "aytgcalls_cookies.txt")
    if os.path.isfile(path) and os.path.getsize(path) > 0:
        return path
    return None


def YOUTUBE_MUSIC_AVAILABLE() -> bool:
    """Return True if the ``YouTubeMusic`` package is installed."""
    return shutil.which("yt-dlp") is not None


def is_youtube_url(url: str) -> bool:
    """Return True if the URL looks like a site YouTubeMusic can handle."""
    try:
        host = urlparse(url).netloc.lower()
        host = host.split(":")[0].removeprefix("www.")
        return host in _YT_DOMAINS
    except Exception:
        return False


#: Backward-compatible alias for the player module.
is_ytdlp_url = is_youtube_url


async def resolve_url(url: str) -> AudioSource | None:
    """Resolve a YouTube / streaming URL to a direct playable source.

    Uses the ``YouTubeMusic`` package (which wraps yt-dlp) to get a direct
    audio stream URL.  Returns ``None`` if ``yt-dlp`` is not available
    (caller should fall back to the original URL).
    """
    if not YOUTUBE_MUSIC_AVAILABLE():
        logger.debug("yt-dlp not available; cannot resolve %r", url)
        return None

    cookies = _cookie_path()
    try:
        from YouTubeMusic import get_stream
        direct_url = await get_stream(url, cookies)
    except ImportError:
        logger.debug("YouTubeMusic package not installed; cannot resolve %r", url)
        return None
    except Exception as exc:
        raise MediaSourceError(
            f"YouTubeMusic failed to resolve {url!r}: {exc}"
        ) from exc

    if not direct_url:
        logger.error("YouTubeMusic returned no stream URL for %r — cannot fall back to original URL (may be an HLS manifest)", url)
        raise MediaSourceError(
            f"YouTubeMusic returned no stream URL for {url!r}. "
            "The track may be geo-restricted, age-restricted, or unavailable."
        )

    # Reject HLS/DASH manifests as a safety net
    parsed = urlparse(direct_url)
    if parsed.path.endswith((".m3u8", ".mpd")):
        logger.error("YouTubeMusic returned an HLS/DASH manifest for %r: %s — rejecting", url, parsed.path)
        raise MediaSourceError(
            f"Resolved URL is an HLS/DASH manifest, not a direct audio stream: {direct_url}"
        )

    logger.info("Resolved %r -> direct videoplayback URL (%s)", url, parsed.hostname)

    return AudioSource(
        uri=direct_url,
        kind=SourceKind.URL,
        title=None,          # title is not available from get_stream
        start_at=0.0,
        byte_offset=0,
    )
