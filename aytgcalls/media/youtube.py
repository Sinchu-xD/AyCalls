"""YouTube (and yt-dlp supported sites) URL resolution.

When ``yt_dlp`` is installed (optional dependency), URLs from YouTube, Vimeo,
SoundCloud, etc. are transparently resolved to a direct audio stream URL that FFmpeg
can decode.  Without yt_dlp, plain http/https URLs pass through unchanged.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
from urllib.parse import urlparse

from ..exceptions import MediaSourceError
from ..types import AudioSource, SourceKind

logger = logging.getLogger("media.youtube")

__all__ = ["resolve_url", "is_ytdlp_url", "YTDLP_AVAILABLE"]

#: Well-known domains that yt-dlp can handle (not exhaustive — yt-dlp supports 1000+).
_YTDLP_DOMAINS: frozenset[str] = frozenset(
    {
        "youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be",
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

#: Minimal audio formats yt-dlp should prefer (opus, mp3, aac, m4a).
#: Formats ending in + are disallowed as they return HLS/DASH manifests instead of direct URLs.
_AUDIO_FORMATS = [
    "251",  # webm opus 160k (YouTube)
    "250",  # webm opus 70k
    "249",  # webm opus 50k
    "140",  # m4a aac 128k
    "139",  # m4a aac 48k
    "171",  # webm opus vorbis
    "160",  # mp4a 128k
    "18",   # mp4a 128k (legacy)
    "bestaudio[ext=webm]/bestaudio[ext=m4a]/bestaudio",
]

def _ytdlp_cookie_path() -> str | None:
    """Return a cookie file path if one exists at the standard temp location, else None."""
    path = os.path.join(tempfile.gettempdir(), "aytgcalls_cookies.txt")
    if os.path.isfile(path) and os.path.getsize(path) > 0:
        return path
    return None


def YTDLP_AVAILABLE() -> bool:
    """Return True if ``yt-dlp`` is installed and callable."""
    return shutil.which("yt-dlp") is not None


def is_ytdlp_url(url: str) -> bool:
    """Return True if the URL looks like a site yt-dlp can handle."""
    try:
        host = urlparse(url).netloc.lower()
        # Strip port and "www."
        host = host.split(":")[0].removeprefix("www.")
        return host in _YTDLP_DOMAINS
    except Exception:
        return False


def _extract_direct_url(raw_url: str, cookies_from: str | None) -> tuple[str, str, float | None]:
    """Run yt-dlp to get a direct audio URL.  Returns (url, title, duration)."""
    args = [
        "yt-dlp",
        "--dump-json",
        "--no-playlist",
        "--no-warnings",
        "--no-check-certificates",
        "--force-ipv4",
        "-f", "/".join(_AUDIO_FORMATS),
        "--format-sort", "ext",
        raw_url,
    ]
    if cookies_from and os.path.exists(cookies_from) and os.path.getsize(cookies_from) > 0:
        args.extend(["--cookies", cookies_from])
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except FileNotFoundError:
        raise MediaSourceError(
            "yt-dlp is not installed. Install it with: pip install yt-dlp"
        )
    except subprocess.TimeoutExpired:
        raise MediaSourceError(
            f"yt-dlp timed out resolving {raw_url!r}. "
            "The URL may be geo-restricted or the site is unreachable."
        )

    if proc.returncode != 0:
        stderr = proc.stderr.strip()
        if proc.stdout.strip():
            stderr = proc.stdout.strip()
        raise MediaSourceError(
            f"yt-dlp failed to resolve {raw_url!r}: {stderr}"
        )

    lines = [l.strip() for l in proc.stdout.strip().splitlines() if l.strip()]
    if not lines:
        raise MediaSourceError(
            f"yt-dlp returned no data for {raw_url!r}."
        )

    best = None
    for line in lines:
        try:
            info = json.loads(line)
            if best is None or info.get("abr") or info.get("vbr"):
                best = info
        except json.JSONDecodeError:
            continue

    if best is None:
        raise MediaSourceError(
            f"yt-dlp could not parse metadata for {raw_url!r}."
        )

    direct_url = (
        best.get("url")
        or best.get("webpage_url")
        or raw_url
    )

    # Reject HLS/DASH manifest URLs — they can't be fed directly to FFmpeg
    parsed = urlparse(direct_url)
    if parsed.path.endswith((".m3u8", ".mpd")):
        # Try again with stricter format selection that avoids HLS/DASH
        return _extract_direct_url_strict(raw_url, cookies_from)

    title = best.get("title") or best.get("description", "")[:60] or None
    duration = best.get("duration")
    return direct_url, title, duration


def _extract_direct_url_strict(
    raw_url: str, cookies_from: str | None
) -> tuple[str, str, float | None]:
    """Fallback: force a direct audio URL, never HLS/DASH."""
    args = [
        "yt-dlp",
        "--dump-json",
        "--no-playlist",
        "--no-warnings",
        "--no-check-certificates",
        "--force-ipv4",
        "--extract-audio",
        "--audio-format", "best",
        "--audio-quality", "0",
        raw_url,
    ]
    if cookies_from and os.path.exists(cookies_from) and os.path.getsize(cookies_from) > 0:
        args.extend(["--cookies", cookies_from])
    proc = subprocess.run(args, capture_output=True, text=True, timeout=30, check=False)
    if proc.returncode != 0:
        raise MediaSourceError(
            f"yt-dlp strict fallback failed for {raw_url!r}: {proc.stderr.strip()}"
        )
    lines = [l.strip() for l in proc.stdout.strip().splitlines() if l.strip()]
    best_strict = None
    for line in lines:
        try:
            info = json.loads(line)
            if best_strict is None or info.get("abr") or info.get("vbr"):
                best_strict = info
        except json.JSONDecodeError:
            continue
    if best_strict is None:
        raise MediaSourceError(f"yt-dlp strict fallback returned no data for {raw_url!r}.")
    url = best_strict.get("url") or best_strict.get("webpage_url") or raw_url
    title = best_strict.get("title") or best_strict.get("description", "")[:60] or None
    duration = best_strict.get("duration")
    return url, title, duration


def resolve_url(url: str) -> AudioSource | None:
    """Resolve a yt-dlp URL to a direct playable source.

    Returns an :class:`AudioSource` with the direct URL, or ``None`` if yt-dlp
    is not installed (caller should fall back to the original URL).
    """
    if not YTDLP_AVAILABLE():
        logger.debug("yt-dlp not installed; cannot resolve %r", url)
        return None

    try:
        loop = asyncio.get_event_loop()
        direct_url, title, duration = await loop.run_in_executor(
            None, _extract_direct_url, url, None
        )
    except MediaSourceError:
        raise
    except Exception as exc:
        raise MediaSourceError(
            f"Unexpected error resolving {url!r}: {exc}"
        ) from exc

    logger.debug(
        "Resolved %r -> %r (title=%r, duration=%s)",
        url, direct_url, title, duration,
    )

    source = AudioSource(
        uri=direct_url,
        kind=SourceKind.URL,
        title=title,
        start_at=0.0,
        byte_offset=0,
    )
    return source
