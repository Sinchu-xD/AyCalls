"""HTTP(S) streaming through Python instead of FFmpeg's own network layer.

Why this exists:

* Some FFmpeg builds cannot do DNS at all (statically linked binaries lose glibc's NSS
  resolver and crash on any network URL), so ``-i https://…`` is not always available.
* Fetching in Python gives us custom headers, cookies, redirect control, the system trust
  store, and **resumable** retries via HTTP ``Range`` — FFmpeg's ``-reconnect`` restarts
  the whole request instead.

Bytes are streamed straight into FFmpeg's stdin; nothing is buffered to disk and the whole
track is never held in memory.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from ..exceptions import MediaSourceError
from ..logger import get_logger

logger = get_logger("media.http")

__all__ = ["HttpStreamReader", "DEFAULT_USER_AGENT"]

DEFAULT_USER_AGENT = "aytgcalls/0.2 (+https://github.com/aytgcalls/aytgcalls)"


class HttpStreamReader:
    """Streams an http(s) URL as chunks, resuming with ``Range`` after a drop."""

    def __init__(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        chunk_size: int = 64 * 1024,
        connect_timeout: float = 15.0,
        read_timeout: float = 30.0,
        max_retries: int = 3,
        retry_delay: float = 1.0,
        start_offset: int = 0,
    ) -> None:
        self.url = url
        #: Byte offset to start from, used for seeking (HTTP ``Range``).
        self.start_offset = max(0, start_offset)
        self.headers = {"User-Agent": DEFAULT_USER_AGENT, **(headers or {})}
        self.chunk_size = chunk_size
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.bytes_read = self.start_offset
        self.retries_used = 0
        self._session: object | None = None
        self._closed = False

    # -- lifecycle ---------------------------------------------------------------------

    async def _ensure_session(self) -> object:
        import aiohttp

        if self._session is None:
            timeout = aiohttp.ClientTimeout(
                connect=self.connect_timeout, sock_read=self.read_timeout, total=None
            )
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def close(self) -> None:
        self._closed = True
        session = self._session
        self._session = None
        if session is not None:
            try:
                await session.close()
            except Exception as exc:
                logger.debug("closing HTTP session failed: %s", exc)

    # -- streaming ---------------------------------------------------------------------

    async def iter_chunks(self) -> AsyncIterator[bytes]:
        """Yield response body chunks, transparently resuming after a network drop.

        Live radio streams (no ``Content-Length``, endless body) simply never finish,
        which is exactly what we want.
        """
        import aiohttp

        attempt = 0
        while not self._closed:
            headers = dict(self.headers)
            resuming = self.bytes_read > 0
            if resuming:
                headers["Range"] = f"bytes={self.bytes_read}-"
            session = await self._ensure_session()
            try:
                async with session.get(
                    self.url, headers=headers, allow_redirects=True
                ) as response:
                    if response.status >= 400:
                        raise MediaSourceError(
                            f"HTTP {response.status} {response.reason} for {self.url}"
                        )
                    if resuming and response.status == 200:
                        # RFC 7233: honouring Range means answering 206. A 200 here means
                        # the server replayed from byte 0, so skip what we already sent or
                        # FFmpeg would hear duplicated audio.
                        logger.debug("server ignored Range; discarding %d bytes", self.bytes_read)
                        await self._discard(response, self.bytes_read)
                    logger.debug(
                        "streaming %s (HTTP %s, type=%s)",
                        self.url,
                        response.status,
                        response.headers.get("Content-Type", "?"),
                    )
                    async for chunk in response.content.iter_chunked(self.chunk_size):
                        if self._closed:
                            return
                        self.bytes_read += len(chunk)
                        yield chunk
                    return  # clean end of body
            except asyncio.CancelledError:
                raise
            except MediaSourceError:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                attempt += 1
                self.retries_used = attempt
                if attempt > self.max_retries or self._closed:
                    raise MediaSourceError(
                        f"Streaming {self.url} failed after {attempt} attempt(s): "
                        f"{type(exc).__name__}: {exc}"
                    ) from exc
                logger.warning(
                    "stream dropped after %d bytes (%s); retry %d/%d",
                    self.bytes_read,
                    type(exc).__name__,
                    attempt,
                    self.max_retries,
                )
                await asyncio.sleep(self.retry_delay * attempt)

    @staticmethod
    async def _discard(response: object, count: int) -> None:
        remaining = count
        content = response.content
        while remaining > 0:
            chunk = await content.read(min(remaining, 64 * 1024))
            if not chunk:
                return
            remaining -= len(chunk)

    async def __aenter__(self) -> HttpStreamReader:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()
