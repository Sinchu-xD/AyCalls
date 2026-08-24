"""FFmpeg subprocess that decodes any video source to raw Annex-B H.264 NAL units.

One process per video source.  stdout yields raw NAL units (start-code prefixed),
stderr is drained into a bounded ring.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import logging
import shlex
from typing import TYPE_CHECKING

from ..logger import get_logger
from ..types import AudioSource, SourceKind

if TYPE_CHECKING:  # pragma: no cover
    pass

logger = get_logger("media.video_ffmpeg")

__all__ = ["FFmpegVideoProcess", "build_video_ffmpeg_args"]

_NETWORK_ARGS = (
    "-reconnect", "1",
    "-reconnect_streamed", "1",
    "-reconnect_delay_max", "5",
    "-rw_timeout", "15000000",
)


def build_video_ffmpeg_args(
    source: AudioSource,
    *,
    binary: str = "ffmpeg",
    from_stdin: bool = False,
    codec: str = "copy",
    fps: int = 30,
    scale: str | None = "1280:720",
) -> list[str]:
    """Build ffmpeg argv that outputs raw H.264 Annex-B NAL units on stdout.

    :param source: The media source.
    :param codec: Video codec — ``copy`` (default) for already-H.264 sources,
        ``libx264`` for re-encoding.
    :param fps: Target frame rate (for re-encoded video).
    :param scale: Optional ``"width:height"`` scale filter. ``None`` for passthrough.
    """
    args = [binary, "-hide_banner", "-loglevel", "error"]
    if not from_stdin:
        args.append("-nostdin")
    if source.kind is SourceKind.URL and not from_stdin:
        args += list(_NETWORK_ARGS)
    args += list(source.ffmpeg_input_args)
    if source.start_at > 0 and not source.byte_offset:
        args += ["-ss", f"{source.start_at:.3f}"]
    args += ["-i", "pipe:0" if from_stdin else source.uri]
    args += ["-an"]  # strip audio — audio goes through the PCM path
    if codec == "copy":
        args += ["-c:v", "copy", "-bsf:v", "h264_mp4toannexb"]
    else:
        args += [
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            "-profile:v", "baseline",
            "-preset", "ultrafast",
            "-r", str(fps),
            "-g", str(fps * 2),
        ]
        if scale:
            args += ["-vf", f"scale={scale}"]
        args += ["-bsf:v", "h264_mp4toannexb"]
    args += ["-f", "h264", "pipe:1"]
    return args


class FFmpegVideoProcess:
    """A running ffmpeg video decode, yielding raw Annex-B H.264 NAL units."""

    def __init__(
        self,
        source: AudioSource,
        *,
        binary: str = "ffmpeg",
        codec: str = "copy",
        fps: int = 30,
        scale: str | None = "1280:720",
        http_fetch: bool = False,
        http_headers: dict[str, str] | None = None,
    ) -> None:
        self.source = source
        self.binary = binary
        self.codec = codec
        self.fps = fps
        self.scale = scale
        self.http_fetch = http_fetch and source.kind is SourceKind.URL
        self.http_headers = http_headers
        self._process: asyncio.subprocess.Process | None = None
        self._stderr: collections.deque[str] = collections.deque(maxlen=40)
        self._stderr_task: asyncio.Task[None] | None = None
        self._pump_task: asyncio.Task[None] | None = None
        self._reader: object | None = None
        self._pump_error: BaseException | None = None
        self._eof = False

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
        if self._process is not None:
            raise RuntimeError("FFmpegVideoProcess.start() called twice")
        args = build_video_ffmpeg_args(
            self.source,
            binary=self.binary,
            from_stdin=self.http_fetch,
            codec=self.codec,
            fps=self.fps,
            scale=self.scale,
        )
        logger.debug("spawn video: %s", " ".join(shlex.quote(a) for a in args))
        try:
            self._process = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.PIPE if self.http_fetch else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            from ..exceptions import FFmpegNotInstalled  # noqa: PLC0415
            raise FFmpegNotInstalled(self.binary) from exc
        except OSError as exc:
            raise RuntimeError(f"Could not start ffmpeg: {exc}") from exc
        self._stderr_task = asyncio.ensure_future(self._drain_stderr())
        if self.http_fetch:
            from .http import HttpStreamReader  # noqa: PLC0415
            self._reader = HttpStreamReader(self.source.uri, headers=self.http_headers)
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
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("stderr drain ended: %s", exc)

    async def _pump_stdin(self) -> None:
        assert self._process is not None and self._process.stdin is not None
        stdin = self._process.stdin
        try:
            async for chunk in self._reader.iter_chunks():  # type: ignore[union-attr]
                stdin.write(chunk)
                await stdin.drain()
        except asyncio.CancelledError:
            raise
        except (BrokenPipeError, ConnectionResetError):
            logger.debug("ffmpeg closed stdin (normal on stop)")
        except Exception as exc:
            self._pump_error = exc
            logger.error("HTTP pump failed for video: %s", exc)
        finally:
            with contextlib.suppress(Exception):
                stdin.close()

    async def read_nalu(self) -> bytes | None:
        """Read one NAL unit (from ``start_code + NAL header`` to next start code).

        Yields raw NAL units as they arrive from FFmpeg's stdout pipe.
        Returns ``None`` at end of stream.
        """
        if self._process is None or self._process.stdout is None:
            raise RuntimeError("FFmpegVideoProcess.read_nalu() called before start()")

        # NAL units in Annex-B start with 0x000001 or 0x00000001
        buf = bytearray()
        # First, consume any leftover from previous call.
        # We look for the start code pattern in the pipe.
        try:
            while True:
                data = await self._process.stdout.read(4096)
                if not data:
                    self._eof = True
                    if self._pump_error is not None:
                        raise RuntimeError(f"Streaming failed: {self._pump_error}")
                    await self._check_exit()
                    return None
                buf.extend(data)
                # Scan for NAL start codes
                nal_start = buf.find(b"\x00\x00\x00\x01")
                if nal_start < 0:
                    nal_start = buf.find(b"\x00\x00\x01")
                if nal_start >= 0:
                    # Find the next start code
                    search_from = nal_start + 3
                    next_start = buf.find(b"\x00\x00\x00\x01", search_from)
                    if next_start < 0:
                        next_start = buf.find(b"\x00\x00\x01", search_from)
                    if next_start >= 0:
                        nal = bytes(buf[nal_start:next_start])
                        del buf[:next_start]
                        return nal
        except asyncio.CancelledError:
            raise
        except RuntimeError:
            raise
        except (BrokenPipeError, ConnectionResetError):
            self._eof = True
            return None

    async def _check_exit(self) -> None:
        if self._process is None:
            return
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._process.wait(), timeout=2)
        code = self._process.returncode
        if code not in (0, None, -9, -15):
            raise RuntimeError(
                f"ffmpeg failed while decoding {self.source.display_name!r}",
                returncode=code,
                stderr=self.stderr_text,
            )

    async def stop(self, *, timeout: float = 1.5) -> None:
        process, self._process = self._process, None
        if self._pump_task is not None:
            self._pump_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._pump_task
            self._pump_task = None
        if self._reader is not None:
            await self._reader.close()  # type: ignore[attr-defined]
            self._reader = None
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._stderr_task
            self._stderr_task = None
        if process is None:
            return
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
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(process.wait(), timeout=timeout)

    async def __aenter__(self) -> "FFmpegVideoProcess":
        await self.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.stop()
