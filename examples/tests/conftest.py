"""Shared pytest fixtures. No network access is required by any unit test."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from aytgcalls.types import BYTES_PER_FRAME

FFMPEG = shutil.which("ffmpeg")
requires_ffmpeg = pytest.mark.skipif(FFMPEG is None, reason="ffmpeg is not installed")


@pytest.fixture(scope="session")
def ffmpeg_bin() -> str:
    if FFMPEG is None:  # pragma: no cover
        pytest.skip("ffmpeg is not installed")
    return FFMPEG


@pytest.fixture(scope="session")
def tone_wav(tmp_path_factory: pytest.TempPathFactory, ffmpeg_bin: str) -> Path:
    """A 1 second 440 Hz stereo WAV, generated with ffmpeg."""
    path = tmp_path_factory.mktemp("audio") / "tone.wav"
    subprocess.run(
        [
            ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=1:sample_rate=48000",
            "-ac", "2", str(path),
        ],
        check=True,
    )
    return path


@pytest.fixture(scope="session")
def short_mp3(tmp_path_factory: pytest.TempPathFactory, ffmpeg_bin: str) -> Path:
    """A 0.5 second MP3, to exercise a compressed container."""
    path = tmp_path_factory.mktemp("audio") / "beep.mp3"
    subprocess.run(
        [
            ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "sine=frequency=880:duration=0.5:sample_rate=48000",
            "-ac", "2", "-b:a", "128k", str(path),
        ],
        check=True,
    )
    return path


@pytest.fixture(scope="session")
def long_tone_wav(tmp_path_factory: pytest.TempPathFactory, ffmpeg_bin: str) -> Path:
    """A 5 second stereo WAV — long enough that seeking is meaningful."""
    path = tmp_path_factory.mktemp("audio_long") / "long.wav"
    subprocess.run(
        [
            ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=5:sample_rate=48000",
            "-ac", "2", str(path),
        ],
        check=True,
    )
    return path


@pytest.fixture(scope="session")
def long_tone_bytes(long_tone_wav: Path) -> bytes:
    return long_tone_wav.read_bytes()


@pytest.fixture(scope="session")
def long_tone_mp3_bytes(long_tone_mp3: Path) -> bytes:
    """The 5s MP3 as bytes, read synchronously so async fixtures never touch the disk."""
    return long_tone_mp3.read_bytes()


@pytest.fixture(scope="session")
def long_tone_mp3(tmp_path_factory: pytest.TempPathFactory, ffmpeg_bin: str) -> Path:
    """A 5 second CBR MP3 — self-framing, so byte-offset seeking is valid."""
    path = tmp_path_factory.mktemp("audio_mp3") / "long.mp3"
    subprocess.run(
        [
            ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=5:sample_rate=48000",
            "-ac", "2", "-b:a", "128k", str(path),
        ],
        check=True,
    )
    return path


@pytest.fixture
async def range_server(long_tone_bytes: bytes, long_tone_mp3_bytes: bytes):
    """Serve the 5s tone as WAV and MP3 with full HTTP Range support (like a real CDN)."""
    pytest.importorskip("aiohttp")
    from aiohttp import web

    bodies = {"/long.wav": (long_tone_bytes, "audio/wav"),
              "/long.mp3": (long_tone_mp3_bytes, "audio/mpeg")}

    async def audio(request: web.Request) -> web.StreamResponse:
        payload, content_type = bodies[request.path]
        has_range = request.headers.get("Range")
        start = int(has_range.split("=")[1].split("-")[0]) if has_range else 0

        response = web.Response(
            body=payload[start:],
            status=206 if has_range else 200,
            headers={
                "Accept-Ranges": "bytes",
                "Content-Type": content_type,
            },
        )
        if has_range:
            response.headers["Content-Range"] = (
                f"bytes {start}-{len(payload) - 1}/{len(payload)}"
            )
        return response

    app = web.Application()
    app.router.add_get("/long.wav", audio)  # aiohttp registers HEAD for us
    app.router.add_get("/long.mp3", audio)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    try:
        yield f"http://127.0.0.1:{runner.addresses[0][1]}"
    finally:
        await runner.cleanup()


@pytest.fixture
def pcm_frame() -> bytes:
    """One frame of non-silent PCM."""
    return bytes([0x11, 0x22]) * (BYTES_PER_FRAME // 2)


@pytest.fixture(scope="session")
def tone_bytes(tone_wav: Path) -> bytes:
    """The tone WAV as bytes, read synchronously so async tests never touch the disk."""
    return tone_wav.read_bytes()


@pytest.fixture
def telegram_join_response() -> dict:
    """A realistic ``UpdateGroupCallConnection.params`` payload (PROTOCOL.md §3)."""
    return {
        "transport": {
            "ufrag": "9aBc",
            "pwd": "kK6TmvVsFbQmXsAmzMPHrCsL",
            "fingerprints": [
                {
                    "hash": "sha-256",
                    "fingerprint": (
                        "11:22:33:44:55:66:77:88:99:AA:BB:CC:DD:EE:FF:00:"
                        "11:22:33:44:55:66:77:88:99:AA:BB:CC:DD:EE:FF:00"
                    ),
                    "setup": "passive",
                }
            ],
            "candidates": [
                {
                    "generation": "0",
                    "component": "1",
                    "protocol": "udp",
                    "port": "44445",
                    "ip": "91.108.9.1",
                    "foundation": "1",
                    "id": "6da76b9dd4",
                    "priority": "2130706431",
                    "type": "host",
                    "network": "0",
                },
                {
                    "generation": "0",
                    "component": "1",
                    "protocol": "udp",
                    "port": "44446",
                    "ip": "2001:67c:4e8:f004::a",
                    "foundation": "2",
                    "id": "6da76b9dd5",
                    "priority": "2130706430",
                    "type": "host",
                    "network": "0",
                },
            ],
            "rtcp-mux": True,
            "xmlns": "urn:xmpp:jingle:transports:ice-udp:1",
        },
        "audio": {
            "ssrc": 987654321,
            "payload-types": [
                {
                    "id": 111,
                    "name": "opus",
                    "clockrate": 48000,
                    "channels": 2,
                    "rtcp-fbs": [{"type": "transport-cc"}],
                    "parameters": {"minptime": "10", "useinbandfec": "1"},
                }
            ],
            "rtp-hdrexts": [
                {"id": 1, "uri": "urn:ietf:params:rtp-hdrext:ssrc-audio-level"},
                {
                    "id": 3,
                    "uri": (
                        "http://www.ietf.org/id/draft-holmer-rmcat-"
                        "transport-wide-cc-extensions-01"
                    ),
                },
            ],
        },
    }


@pytest.fixture
def telegram_join_response_json(telegram_join_response: dict) -> str:
    return json.dumps(telegram_join_response)
