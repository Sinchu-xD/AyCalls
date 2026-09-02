"""Async FFmpeg process management: any input -> raw PCM s16le 48 kHz stereo on stdout.

One process per *source*, never per frame, never a temp file, never the whole track in
RAM. stderr is drained continuously into a bounded ring so a stalled reader can never
deadlock the process on a full pipe.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import shlex
from typing import TYPE_CHECKING, Any

from ..exceptions import FFmpegError, MediaSourceError
from ..logger import get_logger
from ..types import BYTES_PER_FRAME, CHANNELS, SAMPLE_RATE, AudioSource, SourceKind

if TYPE_CHECKING:
    pass

logger = get_logger("media.ffmpeg")

__all__ = ["FFmpegProcess", "build_ffmpeg_args"]

#: Reconnect flags for network sources so a blip does not kill the stream.
_NETWORK_ARGS = (
    "-reconnect", "1",
    "-reconnect_streamed", "1",
    "-reconnect_delay_max", "5",
    "-rw_timeout", "15000000",
)


def build_ffmpeg_args(
    source: AudioSource, *, binary: str = "ffmpeg", from_stdin: bool = False
) -> list[str]:
    """Build the ffmpeg argv that decodes ``source`` to PCM on stdout.

    With ``from_stdin`` the media arrives on ``pipe:0`` (Python does the HTTP fetch), so
    FFmpeg's own network layer and its ``-reconnect`` flags are not used.
    """
    args = [binary, "-hide_banner"]
    if not from_stdin:
        args.append("-nostdin")
    args += ["-loglevel", "error"]
    if source.kind is SourceKind.URL and not from_stdin:
        args += list(_NETWORK_ARGS)
    args += list(source.ffmpeg_input_args)
    # A byte offset already positions the stream, so -ss would seek twice.
    if source.start_at > 0 and not source.byte_offset:
        args += ["-ss", f"{source.start_at:.3f}"]
    args += [
        "-i", "pipe:0" if from_stdin else source.uri,
        "-vn",
        "-f", "s16le",
        "-acodec", "pcm_s16le",
        "-ac", str(CHANNELS),
        "-ar", str(SAMPLE_RATE),
        "pipe:1",
    ]
    return args


class FFmpegProcess:
    """A running ffmpeg decode, exposing ``read()`` of fixed-size PCM chunks."""

    def __init__(
        self,
        source: AudioSource,
        *,
        binary: str = "ffmpeg",
        chunk_size: int = BYTES_PER_FRAME,
        stderr_lines: int = 40,
        http_fetch: bool = False,
        http_headers: dict[str, str] | None = None,
    ) -> None:
        self.source = source
        self.binary = binary
        self.chunk_size = chunk_size
        #: Fetch http(s) sources with aiohttp and pipe them into FFmpeg's stdin.
        self.http_fetch = http_fetch and source.kind is SourceKind.URL
        self.http_headers = http_headers
        self._process: asyncio.subprocess.Process | None = None
        self._stderr: collections.deque[str] = collections.deque(maxlen=stderr_lines)
        self._stderr_task: asyncio.Task[None] | None = None
        self._pump_task: asyncio.Task[None] | None = None
        self._reader: Any = None
        self._pump_error: BaseException | None = None
        self._eof = False

    # -- lifecycle --------------------------------------------------------------------

    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    @property
    def at_eof(self) -> bool:
        return self._eof

    @property
    def stderr_text(self) -> str:
        return "\n".join(self._stderr)

    async def start(self) -> None:
        """Spawn ffmpeg. Raises :class:`FFmpegError` if it cannot be spawned."""
        if self._process is not None:
            raise FFmpegError("FFmpegProcess.start() called twice")
        args = build_ffmpeg_args(self.source, binary=self.binary, from_stdin=self.http_fetch)
        logger.debug("spawn: %s", " ".join(shlex.quote(a) for a in args))
        try:
            self._process = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.PIPE if self.http_fetch else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            from ..exceptions import FFmpegNotInstalled

            raise FFmpegNotInstalled(self.binary) from exc
        except OSError as exc:
            raise FFmpegError(f"Could not start ffmpeg: {exc}") from exc
        self._stderr_task = asyncio.ensure_future(self._drain_stderr())
        if self.http_fetch:
            from .http import HttpStreamReader

            self._reader = HttpStreamReader(
                self.source.uri,
                headers=self.http_headers,
                start_offset=self.source.byte_offset,
            )
            self._pump_task = asyncio.ensure_future(self._pump_stdin())

    async def _drain_stderr(self) -> None:
        assert self._process is not None and self._process.stderr is not None
        try:
            while True:
                line = await self._process.stderr.readline()
                if not line:
                    break
                text = line.decode("utf-8", "replace").rstrip()
                if text:
                    self._stderr.append(text)
                    logger.debug("ffmpeg[%s]: %s", self.source.display_name, text)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("stderr drain ended: %s", exc)

    async def _pump_stdin(self) -> None:
        """Feed HTTP body bytes into FFmpeg's stdin until the source or FFmpeg ends."""
        assert self._process is not None and self._process.stdin is not None
        stdin = self._process.stdin
        try:
            async for chunk in self._reader.iter_chunks():
                stdin.write(chunk)
                await stdin.drain()
        except asyncio.CancelledError:
            raise
        except (BrokenPipeError, ConnectionResetError):
            logger.debug("ffmpeg closed stdin (normal on stop/seek)")
        except Exception as exc:
            self._pump_error = exc
            logger.error("HTTP pump failed for %s: %s", self.source.display_name, exc)
        finally:
            with contextlib.suppress(Exception):
                stdin.close()

    # -- reading ----------------------------------------------------------------------

    async def read(self, size: int | None = None) -> bytes:
        """Read exactly ``size`` bytes, or fewer at end of stream (``b""`` at EOF)."""
        if self._process is None or self._process.stdout is None:
            raise FFmpegError("FFmpegProcess.read() called before start()")
        want = size or self.chunk_size
        try:
            data = await self._process.stdout.readexactly(want)
        except asyncio.IncompleteReadError as exc:
            data = exc.partial
            self._eof = True
        except (BrokenPipeError, ConnectionResetError):
            self._eof = True
            data = b""
        if not data:
            self._eof = True
            if self._pump_error is not None:
                raise MediaSourceError(
                    f"Streaming {self.source.display_name!r} failed: {self._pump_error}"
                ) from self._pump_error
            await self._check_exit()
        return data

    async def _check_exit(self) -> None:
        """Raise if ffmpeg died with a non-zero status."""
        if self._process is None:
            return
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._process.wait(), timeout=2)
        code = self._process.returncode
        if code not in (0, None, -9, -15):
            raise FFmpegError(
                f"ffmpeg failed while decoding {self.source.display_name!r}",
                returncode=code,
                stderr=self.stderr_text,
            )

    # -- teardown ---------------------------------------------------------------------

    async def stop(self, *, timeout: float = 1.5) -> None:
        """Terminate ffmpeg and reap it. Never leaves a zombie behind.

        Order matters: stop feeding stdin, then close **our** end of the pipes so a
        blocked ``write()`` inside ffmpeg fails with EPIPE, and only then signal it.
        ffmpeg checks for SIGTERM between frames, so a process blocked writing into a
        full stdout pipe would otherwise ignore the signal until it is killed.
        """
        process, self._process = self._process, None
        if self._pump_task is not None:
            self._pump_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._pump_task
            self._pump_task = None
        if self._reader is not None:
            await self._reader.close()
            self._reader = None
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._stderr_task
            self._stderr_task = None
        if process is None:
            return

        # Close our ends first so ffmpeg cannot block on a full pipe.
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is None:
                continue
            transport = getattr(stream, "_transport", None) or getattr(stream, "transport", None)
            with contextlib.suppress(Exception):
                if transport is not None:
                    transport.close()
                elif hasattr(stream, "close"):
                    stream.close()

        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                logger.debug("ffmpeg ignored SIGTERM for %.1fs; sending SIGKILL", timeout)
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(process.wait(), timeout=timeout)
        logger.debug("ffmpeg for %s reaped (rc=%s)", self.source.display_name, process.returncode)

    async def __aenter__(self) -> FFmpegProcess:
        await self.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.stop()
