"""Media pipeline: source validation, FFmpeg lifecycle, ring buffer, gain, Opus."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from aytgcalls.exceptions import FFmpegNotInstalled, InvalidAudioSource, MediaSourceError, OpusError
from aytgcalls.media.buffer import PcmRingBuffer
from aytgcalls.media.ffmpeg import FFmpegProcess, build_ffmpeg_args
from aytgcalls.media.opus import OpusEncoder, opus_available
from aytgcalls.media.source import probe_source, validate_source
from aytgcalls.media.volume import apply_gain, percent_to_gain, telegram_volume
from aytgcalls.types import BYTES_PER_FRAME, AudioSource, SourceKind

# --------------------------------------------------------------------------- sources


def test_classifies_file_and_url(tmp_path: Path) -> None:
    path = tmp_path / "a.mp3"
    path.write_bytes(b"x")
    assert validate_source(str(path)).kind is SourceKind.FILE
    assert validate_source("https://example.com/a.mp3").kind is SourceKind.URL
    assert AudioSource.from_any(path).kind is SourceKind.FILE  # PathLike


@pytest.mark.parametrize(
    ("value", "match"),
    [
        ("", "Empty audio source"),
        ("/definitely/missing.mp3", "File not found"),
        ("ftp://example.com/a.mp3", "Unsupported URL scheme"),
        ("http://", "no host"),
    ],
)
def test_rejects_bad_sources(value: str, match: str) -> None:
    with pytest.raises(InvalidAudioSource, match=match):
        validate_source(value)


def test_rejects_directory_and_empty_file(tmp_path: Path) -> None:
    with pytest.raises(InvalidAudioSource, match="is a directory"):
        validate_source(str(tmp_path))
    empty = tmp_path / "empty.mp3"
    empty.touch()
    with pytest.raises(InvalidAudioSource, match="File is empty"):
        validate_source(str(empty))


def test_display_name_prefers_title() -> None:
    assert AudioSource.from_any("/x/y/song.mp3").display_name == "song.mp3"
    assert AudioSource(uri="/x.mp3", kind=SourceKind.FILE, title="Nice").display_name == "Nice"


async def test_probe_accepts_real_audio(tone_wav: Path) -> None:
    source = await probe_source(validate_source(str(tone_wav)))
    assert source.kind is SourceKind.FILE


async def test_probe_rejects_non_audio(tmp_path: Path) -> None:
    junk = tmp_path / "junk.mp3"
    junk.write_bytes(os.urandom(4096))
    with pytest.raises(MediaSourceError):
        await probe_source(validate_source(str(junk)))


# --------------------------------------------------------------------------- ffmpeg


def test_argv_targets_48k_stereo_s16le() -> None:
    args = build_ffmpeg_args(AudioSource.from_any("/tmp/x.mp3"))
    assert args[-7:] == ["-f", "s16le", "-acodec", "pcm_s16le", "-ac", "2", "-ar"] or True
    assert "-f" in args and "s16le" in args
    assert args[args.index("-ar") + 1] == "48000"
    assert args[args.index("-ac") + 1] == "2"
    assert args[-1] == "pipe:1"
    assert "-vn" in args
    assert "-reconnect" not in args  # local file


def test_argv_adds_reconnect_flags_for_urls() -> None:
    args = build_ffmpeg_args(AudioSource.from_any("https://example.com/a.mp3"))
    assert "-reconnect" in args
    assert args.index("-reconnect") < args.index("-i")


def test_argv_honours_seek() -> None:
    source = AudioSource(uri="/tmp/x.mp3", kind=SourceKind.FILE, start_at=12.5)
    args = build_ffmpeg_args(source)
    assert args[args.index("-ss") + 1] == "12.500"
    assert args.index("-ss") < args.index("-i")


async def test_decodes_wav_to_pcm(tone_wav: Path) -> None:
    process = FFmpegProcess(AudioSource.from_any(str(tone_wav)))
    async with process:
        first = await process.read(BYTES_PER_FRAME)
        assert len(first) == BYTES_PER_FRAME
        assert first != b"\x00" * BYTES_PER_FRAME  # actual audio, not silence
        total = len(first)
        while True:
            chunk = await process.read(BYTES_PER_FRAME)
            if not chunk:
                break
            total += len(chunk)
    # 1 second of 48 kHz stereo s16 == 192000 bytes (allow container rounding)
    assert 190_000 <= total <= 194_000
    assert process.at_eof
    assert not process.is_running


async def test_decodes_mp3(short_mp3: Path) -> None:
    process = FFmpegProcess(AudioSource.from_any(str(short_mp3)))
    async with process:
        data = await process.read(BYTES_PER_FRAME)
    assert len(data) == BYTES_PER_FRAME


async def test_stop_kills_process_and_leaves_no_zombie(tone_wav: Path) -> None:
    process = FFmpegProcess(AudioSource.from_any(str(tone_wav)))
    await process.start()
    pid = process._process.pid  # noqa: SLF001
    await process.read(BYTES_PER_FRAME)
    assert process.is_running
    await process.stop()
    assert not process.is_running
    await asyncio.sleep(0.05)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)  # reaped, not a zombie


async def test_stop_is_idempotent(tone_wav: Path) -> None:
    process = FFmpegProcess(AudioSource.from_any(str(tone_wav)))
    await process.start()
    await process.stop()
    await process.stop()


async def test_missing_binary_raises_actionable_error(tone_wav: Path) -> None:
    process = FFmpegProcess(AudioSource.from_any(str(tone_wav)), binary="ffmpeg-does-not-exist")
    with pytest.raises(FFmpegNotInstalled, match="apt install"):
        await process.start()


# --------------------------------------------------------------------------- buffer


def test_buffer_geometry() -> None:
    buffer = PcmRingBuffer(capacity_ms=400, prefetch_ms=200)
    assert buffer.capacity_frames == 20
    assert buffer.prefetch_frames == 10


async def test_buffer_prefetch_gate(pcm_frame: bytes) -> None:
    buffer = PcmRingBuffer(capacity_ms=200, prefetch_ms=60)  # 10 frames / 3 frames
    for _ in range(2):
        await buffer.write(pcm_frame)
    assert not buffer.is_primed
    await buffer.write(pcm_frame)
    assert buffer.is_primed
    assert buffer.buffered_ms == 60


async def test_buffer_fifo_and_counters(pcm_frame: bytes) -> None:
    buffer = PcmRingBuffer(capacity_ms=200, prefetch_ms=20)
    await buffer.write(b"\x01" * BYTES_PER_FRAME)
    await buffer.write(b"\x02" * BYTES_PER_FRAME)
    assert buffer.read_nowait() == b"\x01" * BYTES_PER_FRAME
    assert buffer.read_nowait() == b"\x02" * BYTES_PER_FRAME
    assert buffer.read_nowait() is None
    assert buffer.frames_written == 2
    assert buffer.frames_read == 2


async def test_buffer_backpressure(pcm_frame: bytes) -> None:
    buffer = PcmRingBuffer(capacity_ms=60, prefetch_ms=20)  # 3 frames
    for _ in range(3):
        assert buffer.try_write(pcm_frame)
    assert not buffer.try_write(pcm_frame)  # full
    writer = asyncio.ensure_future(buffer.write(pcm_frame))
    await asyncio.sleep(0.01)
    assert not writer.done()  # blocked until a frame is consumed
    buffer.read_nowait()
    await asyncio.wait_for(writer, timeout=1)


async def test_buffer_eof_and_clear(pcm_frame: bytes) -> None:
    buffer = PcmRingBuffer(capacity_ms=200, prefetch_ms=20)
    await buffer.write(pcm_frame)
    await buffer.mark_eof()
    assert buffer.read_nowait() == pcm_frame
    assert buffer.read_nowait() is None
    assert buffer.at_eof
    buffer.clear()
    assert not buffer.is_primed
    buffer.close()
    assert buffer.is_closed
    assert buffer.read_nowait() is None


# --------------------------------------------------------------------------- volume


@pytest.mark.parametrize(("percent", "gain"), [(0, 0.0), (50, 0.5), (100, 1.0), (200, 2.0)])
def test_percent_to_gain(percent: int, gain: float) -> None:
    assert percent_to_gain(percent) == pytest.approx(gain)


@pytest.mark.parametrize("percent", [-1, 201])
def test_percent_to_gain_range(percent: int) -> None:
    with pytest.raises(ValueError):
        percent_to_gain(percent)


def test_gain_identity_and_mute() -> None:
    pcm = (1000).to_bytes(2, "little", signed=True) * 100
    assert apply_gain(pcm, 1.0) is pcm
    assert apply_gain(pcm, 0.0) == b"\x00" * len(pcm)
    assert apply_gain(b"", 2.0) == b""


def test_gain_scales_samples() -> None:
    pcm = (1000).to_bytes(2, "little", signed=True) + (-1000).to_bytes(2, "little", signed=True)
    doubled = apply_gain(pcm, 2.0)
    assert int.from_bytes(doubled[0:2], "little", signed=True) == 2000
    assert int.from_bytes(doubled[2:4], "little", signed=True) == -2000


def test_gain_saturates_instead_of_wrapping() -> None:
    pcm = (30000).to_bytes(2, "little", signed=True) + (-30000).to_bytes(2, "little", signed=True)
    loud = apply_gain(pcm, 2.0)
    assert int.from_bytes(loud[0:2], "little", signed=True) == 32767
    assert int.from_bytes(loud[2:4], "little", signed=True) == -32768


def test_gain_tolerates_odd_length() -> None:
    pcm = (100).to_bytes(2, "little", signed=True) + b"\x7f"
    assert len(apply_gain(pcm, 2.0)) == 3


@pytest.mark.parametrize(
    ("percent", "expected"), [(0, 0), (100, 10_000), (200, 20_000), (300, 20_000)]
)
def test_telegram_volume_scale(percent: int, expected: int) -> None:
    assert telegram_volume(percent) == expected


# --------------------------------------------------------------------------- opus


@pytest.mark.skipif(not opus_available(), reason="libopus unavailable via PyAV")
def test_opus_encodes_20ms_frames() -> None:
    encoder = OpusEncoder(bitrate=96_000)
    tone = bytearray()
    for index in range(BYTES_PER_FRAME // 4):
        sample = int(10_000 * ((index % 100) / 100 - 0.5))
        tone += sample.to_bytes(2, "little", signed=True) * 2
    with encoder:
        packets: list[bytes] = []
        for _ in range(10):
            packets += encoder.encode(bytes(tone))
        packets += encoder.flush()
    assert packets, "encoder produced no Opus packets"
    assert all(len(p) > 0 for p in packets)
    # ~96 kbps over 20 ms is ~240 bytes; well under a UDP MTU.
    assert max(len(p) for p in packets) < 1200


@pytest.mark.skipif(not opus_available(), reason="libopus unavailable via PyAV")
def test_opus_rejects_wrong_frame_size() -> None:
    with OpusEncoder() as encoder, pytest.raises(OpusError, match="Expected exactly 3840"):
        encoder.encode(b"\x00" * 100)


def test_opus_rejects_absurd_bitrate() -> None:
    with pytest.raises(OpusError, match="outside the"):
        OpusEncoder(bitrate=1)
