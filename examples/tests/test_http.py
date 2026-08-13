"""URL streaming through aiohttp -> FFmpeg stdin.

A local aiohttp server stands in for the internet, so these tests are deterministic and
need no outbound connectivity.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

pytest.importorskip("aiohttp")
from aiohttp import web  # noqa: E402

from aytgcalls.config import CallConfig  # noqa: E402
from aytgcalls.exceptions import MediaSourceError  # noqa: E402
from aytgcalls.media.ffmpeg import FFmpegProcess, build_ffmpeg_args  # noqa: E402
from aytgcalls.media.http import HttpStreamReader  # noqa: E402
from aytgcalls.media.source import probe_source, validate_source  # noqa: E402
from aytgcalls.player.player import Player  # noqa: E402
from aytgcalls.transport.track import PcmStreamTrack  # noqa: E402
from aytgcalls.types import BYTES_PER_FRAME, AudioSource, SourceKind  # noqa: E402


@pytest.fixture
async def audio_server(tone_bytes: bytes) -> AsyncIterator[str]:
    """Serve the test tone over HTTP, plus a few failure modes."""
    payload = tone_bytes
    state = {"drop_calls": 0, "ignore_calls": 0}

    async def whole(_request: web.Request) -> web.Response:
        return web.Response(body=payload, content_type="audio/wav")

    async def ranged(request: web.Request) -> web.StreamResponse:
        start = 0
        if (header := request.headers.get("Range")):
            start = int(header.split("=")[1].split("-")[0])
        response = web.StreamResponse(
            status=206 if start else 200, headers={"Content-Type": "audio/wav"}
        )
        await response.prepare(request)
        await response.write(payload[start:])
        await response.write_eof()
        return response

    async def drop(request: web.Request) -> web.StreamResponse:
        """First call dies halfway; the retry (with Range) completes the file."""
        state["drop_calls"] += 1
        start = 0
        if (header := request.headers.get("Range")):
            start = int(header.split("=")[1].split("-")[0])
        headers = {"Content-Type": "audio/wav"}
        if start:
            headers["Content-Range"] = f"bytes {start}-{len(payload) - 1}/{len(payload)}"
        # RFC 7233: a server honouring Range MUST answer 206, which is how the reader
        # knows it does not have to discard anything.
        response = web.StreamResponse(status=206 if start else 200, headers=headers)
        await response.prepare(request)
        if state["drop_calls"] == 1:
            await response.write(payload[: len(payload) // 3])
            raise ConnectionResetError("simulated mid-stream drop")
        await response.write(payload[start:])
        await response.write_eof()
        return response

    async def drop_ignoring_range(request: web.Request) -> web.StreamResponse:
        """Dies halfway, then ignores Range and replays from byte 0 with a 200."""
        state["ignore_calls"] += 1
        response = web.StreamResponse(status=200, headers={"Content-Type": "audio/wav"})
        await response.prepare(request)
        if state["ignore_calls"] == 1:
            await response.write(payload[: len(payload) // 3])
            raise ConnectionResetError("simulated mid-stream drop")
        await response.write(payload)  # full body regardless of Range
        await response.write_eof()
        return response

    async def not_found(_request: web.Request) -> web.Response:
        return web.Response(status=404, text="nope")

    async def echo_headers(request: web.Request) -> web.Response:
        return web.json_response(dict(request.headers))

    app = web.Application()
    app.router.add_get("/audio.wav", whole)
    app.router.add_get("/ranged.wav", ranged)
    app.router.add_get("/drop.wav", drop)
    app.router.add_get("/drop-ignore-range.wav", drop_ignoring_range)
    app.router.add_get("/missing.wav", not_found)
    app.router.add_get("/headers", echo_headers)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        await runner.cleanup()


# --------------------------------------------------------------------------- argv


def test_stdin_argv_drops_ffmpeg_network_flags() -> None:
    source = AudioSource.from_any("https://example.com/a.mp3")
    args = build_ffmpeg_args(source, from_stdin=True)
    assert args[args.index("-i") + 1] == "pipe:0"
    assert "-reconnect" not in args      # Python owns retries now
    assert "-nostdin" not in args        # we *are* using stdin
    assert args[-1] == "pipe:1"


def test_url_argv_keeps_reconnect_when_ffmpeg_fetches() -> None:
    source = AudioSource.from_any("https://example.com/a.mp3")
    args = build_ffmpeg_args(source, from_stdin=False)
    assert args[args.index("-i") + 1] == source.uri
    assert "-reconnect" in args


def test_http_fetch_flag_ignored_for_local_files(tone_wav: Path) -> None:
    process = FFmpegProcess(AudioSource.from_any(str(tone_wav)), http_fetch=True)
    assert process.http_fetch is False  # only meaningful for URLs


# --------------------------------------------------------------------------- reader


async def test_reader_streams_whole_body(audio_server: str, tone_bytes: bytes) -> None:
    reader = HttpStreamReader(f"{audio_server}/audio.wav")
    chunks = b""
    try:
        async for chunk in reader.iter_chunks():
            chunks += chunk
    finally:
        await reader.close()
    assert chunks == tone_bytes
    assert reader.bytes_read == len(chunks)


async def test_reader_resumes_with_range_after_a_drop(
    audio_server: str, tone_bytes: bytes
) -> None:
    reader = HttpStreamReader(f"{audio_server}/drop.wav", retry_delay=0.05)
    chunks = b""
    try:
        async for chunk in reader.iter_chunks():
            chunks += chunk
    finally:
        await reader.close()
    assert reader.retries_used == 1
    assert chunks == tone_bytes, "resume produced a corrupted body"


async def test_reader_handles_servers_that_ignore_range(
    audio_server: str, tone_bytes: bytes
) -> None:
    """A non-compliant server replays from byte 0; the reader must not duplicate audio."""
    reader = HttpStreamReader(f"{audio_server}/drop-ignore-range.wav", retry_delay=0.05)
    chunks = b""
    try:
        async for chunk in reader.iter_chunks():
            chunks += chunk
    finally:
        await reader.close()
    assert reader.retries_used == 1
    assert chunks == tone_bytes, "duplicated or lost audio after restart"


async def test_reader_reports_http_errors(audio_server: str) -> None:
    reader = HttpStreamReader(f"{audio_server}/missing.wav")
    with pytest.raises(MediaSourceError, match="HTTP 404"):
        async for _ in reader.iter_chunks():
            pass
    await reader.close()


async def test_reader_sends_custom_headers(audio_server: str) -> None:
    reader = HttpStreamReader(
        f"{audio_server}/headers", headers={"Referer": "https://example.com/page"}
    )
    body = b""
    try:
        async for chunk in reader.iter_chunks():
            body += chunk
    finally:
        await reader.close()
    headers = json.loads(body)
    assert headers["Referer"] == "https://example.com/page"
    assert "aytgcalls" in headers["User-Agent"]


async def test_reader_gives_up_after_max_retries() -> None:
    reader = HttpStreamReader(
        "http://127.0.0.1:1/never", max_retries=2, retry_delay=0.01, connect_timeout=0.5
    )
    with pytest.raises(MediaSourceError, match="after 3 attempt"):
        async for _ in reader.iter_chunks():
            pass
    await reader.close()


# --------------------------------------------------------------------------- pipeline


async def test_ffmpeg_decodes_a_url_via_python(audio_server: str) -> None:
    source = validate_source(f"{audio_server}/audio.wav")
    assert source.kind is SourceKind.URL
    process = FFmpegProcess(source, http_fetch=True)
    async with process:
        first = await process.read(BYTES_PER_FRAME)
        assert len(first) == BYTES_PER_FRAME
        assert first.strip(b"\x00"), "decoded silence from a real tone"
        total = len(first)
        while True:
            chunk = await process.read(BYTES_PER_FRAME)
            if not chunk:
                break
            total += len(chunk)
    assert 190_000 <= total <= 194_000  # ~1s of 48k stereo s16


async def test_probe_source_accepts_a_url(audio_server: str) -> None:
    await probe_source(validate_source(f"{audio_server}/audio.wav"), http_fetch=True)


async def test_probe_source_rejects_a_404(audio_server: str) -> None:
    with pytest.raises(MediaSourceError):
        await probe_source(validate_source(f"{audio_server}/missing.wav"), http_fetch=True)


async def test_player_streams_a_url_end_to_end(audio_server: str) -> None:
    config = CallConfig(buffer_ms=200, prefetch_ms=40, fetch_urls_with_python=True)
    player = Player(PcmStreamTrack(), config=config)
    try:
        await player.play(f"{audio_server}/ranged.wav")
        received = 0
        for _ in range(200):
            frame = await player._provide_frame()  # noqa: SLF001
            if frame:
                received += 1
                if received >= 10:
                    break
            await asyncio.sleep(0.01)
        assert received >= 10, "no PCM frames produced from the URL"
    finally:
        await player.close()


async def test_url_stop_kills_pump_and_process(audio_server: str) -> None:
    process = FFmpegProcess(validate_source(f"{audio_server}/ranged.wav"), http_fetch=True)
    await process.start()
    await process.read(BYTES_PER_FRAME)
    assert process._pump_task is not None  # noqa: SLF001
    await process.stop()
    assert process._pump_task is None  # noqa: SLF001
    assert process._reader is None  # noqa: SLF001
    assert not process.is_running
