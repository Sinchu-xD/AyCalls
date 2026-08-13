"""The playback-control feature set: position, seek/forward/rewind/replay, previous,
loop aliases, now-playing snapshots and metadata probing.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from aytgcalls.config import CallConfig
from aytgcalls.exceptions import NotPlaying
from aytgcalls.media.metadata import MediaInfo, probe_media_info
from aytgcalls.player.player import Player
from aytgcalls.transport.track import PcmStreamTrack
from aytgcalls.types import (
    AudioSource,
    LoopMode,
    PlaybackState,
    SourceKind,
    TrackInfo,
)


def _config(**kwargs: object) -> CallConfig:
    base = {"buffer_ms": 200, "prefetch_ms": 40}
    base.update(kwargs)
    return CallConfig(**base)  # type: ignore[arg-type]


async def _pull(player: Player, frames: int, *, budget: float = 4.0) -> int:
    """Consume frames the way the RTP sender would, but without waiting on wall clock.

    Deadline based rather than iteration based: a seek respawns FFmpeg and has to refill
    the prefetch window, which legitimately takes a few hundred milliseconds.
    """
    got = 0
    deadline = time.monotonic() + budget
    while got < frames and time.monotonic() < deadline:
        if await player._provide_frame():  # noqa: SLF001
            got += 1
        else:
            await asyncio.sleep(0.005)
    return got


# --------------------------------------------------------------------------- loop aliases


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("off", LoopMode.OFF), ("none", LoopMode.OFF), ("0", LoopMode.OFF),
        ("track", LoopMode.TRACK), ("one", LoopMode.TRACK), ("song", LoopMode.TRACK),
        ("queue", LoopMode.QUEUE), ("all", LoopMode.QUEUE), ("playlist", LoopMode.QUEUE),
        ("TRACK", LoopMode.TRACK), ("  Queue  ", LoopMode.QUEUE),
        (True, LoopMode.QUEUE), (False, LoopMode.OFF),
        (LoopMode.TRACK, LoopMode.TRACK),
    ],
)
def test_loop_mode_accepts_friendly_values(value: object, expected: LoopMode) -> None:
    assert LoopMode.from_any(value) is expected


def test_loop_mode_rejects_nonsense() -> None:
    with pytest.raises(ValueError, match="Unknown loop mode"):
        LoopMode.from_any("sideways")


# --------------------------------------------------------------------------- TrackInfo


def test_track_info_formats_time() -> None:
    assert TrackInfo.format_time(None) == "--:--"
    assert TrackInfo.format_time(0) == "00:00"
    assert TrackInfo.format_time(93.4) == "01:33"
    assert TrackInfo.format_time(3725) == "1:02:05"


def test_track_info_progress_and_bar() -> None:
    source = AudioSource.from_any("song.mp3")
    info = TrackInfo(source=source, state=PlaybackState.PLAYING, position=50, duration=100)
    assert info.progress == pytest.approx(0.5)
    bar = info.progress_bar(width=10)
    assert "🔘" in bar and len(bar.replace("🔘", "")) == 9
    assert info.title == "song.mp3"
    assert "song.mp3" in str(info)


def test_track_info_live_has_no_progress() -> None:
    info = TrackInfo(
        source=AudioSource.from_any("https://x/live"), state=PlaybackState.PLAYING, is_live=True
    )
    assert info.progress is None
    assert info.progress_bar() == "🔴 LIVE"


# --------------------------------------------------------------------------- metadata


async def test_probe_local_file_duration(long_tone_wav: Path) -> None:
    info = await probe_media_info(AudioSource.from_any(str(long_tone_wav)))
    assert info.duration == pytest.approx(5.0, abs=0.2)
    assert info.size and info.size > 0
    assert not info.is_live


async def test_probe_url_duration_size_and_ranges(range_server: str) -> None:
    info = await probe_media_info(AudioSource.from_any(f"{range_server}/long.wav"))
    assert info.duration == pytest.approx(5.0, abs=0.3)
    assert info.accepts_ranges is True
    assert info.size and info.size > 400_000


async def test_probe_never_raises_on_garbage() -> None:
    info = await probe_media_info(AudioSource.from_any("/nope/missing.mp3"), timeout=2)
    assert info.duration is None and info.size is None


@pytest.mark.parametrize(
    ("position", "expected"),
    [(0, 0), (50, 500_000), (100, 1_000_000), (200, 1_000_000)],
)
def test_byte_offset_estimation(position: float, expected: int) -> None:
    info = MediaInfo(duration=100.0, size=1_000_000, accepts_ranges=True, format="mp3")
    assert info.byte_offset_for(position) == expected


def test_byte_offset_needs_ranges_size_duration_and_format() -> None:
    ok = {"duration": 10.0, "size": 100, "accepts_ranges": True, "format": "mp3"}
    assert MediaInfo(**ok).byte_offset_for(5) == 50
    assert MediaInfo(**{**ok, "accepts_ranges": False}).byte_offset_for(5) is None
    assert MediaInfo(**{**ok, "duration": None}).byte_offset_for(5) is None
    assert MediaInfo(**{**ok, "size": None}).byte_offset_for(5) is None
    assert MediaInfo(**{**ok, "format": "wav"}).byte_offset_for(5) is None
    assert MediaInfo(duration=None).is_live is True


@pytest.mark.parametrize(
    ("fmt", "resyncable"),
    [
        ("mp3", True), ("aac", True), ("adts", True), ("mpegts", True), ("ac3", True),
        ("wav", False), ("flac", False), ("ogg", False), ("matroska,webm", False),
        ("mov,mp4,m4a,3gp,3g2,mj2", False), (None, False), ("", False),
    ],
)
def test_only_self_framing_formats_are_resyncable(fmt: str | None, resyncable: bool) -> None:
    assert MediaInfo(format=fmt).is_resyncable is resyncable


# --------------------------------------------------------------------------- position


async def test_position_tracks_frames_sent(long_tone_wav: Path) -> None:
    player = Player(PcmStreamTrack(), config=_config())
    try:
        await player.play(str(long_tone_wav))
        assert player.position == 0.0
        assert await _pull(player, 50) == 50      # 50 frames == 1.0 s
        assert player.position == pytest.approx(1.0, abs=0.001)
        assert await _pull(player, 25) == 25
        assert player.position == pytest.approx(1.5, abs=0.001)
    finally:
        await player.close()


async def test_position_is_zero_before_playing() -> None:
    player = Player(PcmStreamTrack(), config=_config())
    assert player.position == 0.0
    assert player.duration is None
    assert player.now_playing.source is None
    await player.close()


async def test_duration_becomes_available(long_tone_wav: Path) -> None:
    player = Player(PcmStreamTrack(), config=_config())
    try:
        await player.play(str(long_tone_wav))
        for _ in range(50):
            if player.duration is not None:
                break
            await asyncio.sleep(0.05)
        assert player.duration == pytest.approx(5.0, abs=0.2)
        snapshot = player.now_playing
        assert snapshot.duration == pytest.approx(5.0, abs=0.2)
        assert snapshot.state is PlaybackState.PLAYING
        assert snapshot.progress is not None
    finally:
        await player.close()


# --------------------------------------------------------------------------- seeking


async def test_seek_local_file_moves_position(long_tone_wav: Path) -> None:
    player = Player(PcmStreamTrack(), config=_config())
    try:
        await player.play(str(long_tone_wav))
        await _pull(player, 10)
        landed = await player.seek(2.0)
        assert landed == pytest.approx(2.0)
        assert player.position == pytest.approx(2.0, abs=0.001)
        assert player.current is not None
        assert player.current.start_at == pytest.approx(2.0)
        # audio still flows after the seek
        assert await _pull(player, 10) == 10
        assert player.position == pytest.approx(2.2, abs=0.01)
    finally:
        await player.close()


async def test_seek_clamps_to_duration(long_tone_wav: Path) -> None:
    player = Player(PcmStreamTrack(), config=_config())
    try:
        await player.play(str(long_tone_wav))
        landed = await player.seek(9999)
        assert 4.0 <= landed <= 5.0, landed
        assert await player.seek(-50) == 0.0
    finally:
        await player.close()


async def test_forward_and_rewind_are_relative(long_tone_wav: Path) -> None:
    player = Player(PcmStreamTrack(), config=_config())
    try:
        await player.play(str(long_tone_wav))
        await player.seek(2.0)
        assert await player.forward(1.0) == pytest.approx(3.0)
        assert await player.rewind(2.0) == pytest.approx(1.0)
        assert await player.rewind(60) == 0.0          # clamped at the start
        assert await player.forward(-1.0) == 0.0       # negative forward == rewind
    finally:
        await player.close()


async def test_replay_restarts_the_track(long_tone_wav: Path) -> None:
    player = Player(PcmStreamTrack(), config=_config())
    try:
        await player.play(str(long_tone_wav))
        await player.seek(3.0)
        assert await player.replay() == 0.0
        assert player.position == 0.0
        assert await _pull(player, 5) == 5
    finally:
        await player.close()


async def test_seek_keeps_paused_state(long_tone_wav: Path) -> None:
    player = Player(PcmStreamTrack(), config=_config())
    try:
        await player.play(str(long_tone_wav))
        await _pull(player, 5)
        await player.pause()
        await player.seek(1.5)
        assert player.is_paused
        assert player.position == pytest.approx(1.5, abs=0.001)
        assert await player._provide_frame() is None  # noqa: SLF001  still silent
        await player.resume()
        assert player.is_playing
    finally:
        await player.close()


async def test_seek_does_not_pollute_history(long_tone_wav: Path) -> None:
    player = Player(PcmStreamTrack(), config=_config())
    try:
        await player.play(str(long_tone_wav))
        await player.seek(1.0)
        await player.seek(2.0)
        assert player.queue.history == ()   # seeking is not a track change
    finally:
        await player.close()


async def test_seek_without_playback_is_rejected() -> None:
    player = Player(PcmStreamTrack(), config=_config())
    with pytest.raises(NotPlaying, match="nothing to seek"):
        await player.seek(5)
    await player.close()


async def test_mp3_url_seek_uses_byte_offset(range_server: str) -> None:
    """MP3 is self-framing, so we seek with an HTTP Range instead of re-downloading."""
    player = Player(PcmStreamTrack(), config=_config(buffer_ms=400, prefetch_ms=80))
    try:
        await player.play(f"{range_server}/long.mp3")
        await _pull(player, 5)
        info = await player._ensure_info(player.current)  # noqa: SLF001
        assert info.can_byte_seek, f"mp3 should be byte-seekable, got {info}"

        landed = await player.seek(2.5)
        assert landed == pytest.approx(2.5, abs=0.05)
        current = player.current
        assert current is not None and current.kind is SourceKind.URL
        assert current.byte_offset > 0, "MP3 URL seek should use an HTTP Range offset"
        assert current.byte_offset == pytest.approx((info.size or 0) / 2, rel=0.25)
        assert await _pull(player, 10) == 10, "no audio after seeking an MP3 URL"
    finally:
        await player.close()


async def test_wav_url_seek_falls_back_to_container_seek(range_server: str) -> None:
    """WAV keeps its geometry in the RIFF header, so a byte slice would be undecodable.

    The player must fall back to ``-ss`` on the piped stream and still produce audio.
    """
    player = Player(PcmStreamTrack(), config=_config(buffer_ms=400, prefetch_ms=80))
    try:
        await player.play(f"{range_server}/long.wav")
        await _pull(player, 5)
        info = await player._ensure_info(player.current)  # noqa: SLF001
        assert info.accepts_ranges and info.duration
        assert not info.can_byte_seek, "WAV must never be byte-sliced"

        landed = await player.seek(2.0)
        assert landed == pytest.approx(2.0, abs=0.05)
        current = player.current
        assert current is not None
        assert current.byte_offset == 0, "WAV seek must not use a byte offset"
        assert current.start_at == pytest.approx(2.0)
        assert await _pull(player, 10) == 10, "no audio after seeking a WAV URL"
    finally:
        await player.close()


async def test_live_stream_cannot_be_seeked(long_tone_wav: Path) -> None:
    """A URL with no discoverable duration is treated as live and refuses to seek."""
    player = Player(PcmStreamTrack(), config=_config())
    try:
        await player.play(str(long_tone_wav))
        # Pretend the current source is a live URL with unknown duration.
        live = AudioSource(uri="https://radio.example/live", kind=SourceKind.URL)
        await player.queue.replace_current(live)
        player._decoding.clear()          # noqa: SLF001
        player._decoding.append(live)     # noqa: SLF001
        player._info[live.id] = MediaInfo(duration=None)  # noqa: SLF001
        with pytest.raises(NotPlaying, match="live stream"):
            await player.seek(10)
    finally:
        await player.close()


# --------------------------------------------------------------------------- previous


async def test_previous_returns_to_the_earlier_track(long_tone_wav: Path, tone_wav: Path) -> None:
    player = Player(PcmStreamTrack(), config=_config())
    try:
        await player.play(str(long_tone_wav))
        await player.queue.add(str(tone_wav))
        await _pull(player, 3)
        assert (await player.skip()).uri == str(tone_wav)
        earlier = await player.previous()
        assert earlier is not None and earlier.uri == str(long_tone_wav)
        assert player.position == 0.0
        assert await _pull(player, 5) == 5
    finally:
        await player.close()


async def test_previous_without_history_is_rejected(long_tone_wav: Path) -> None:
    player = Player(PcmStreamTrack(), config=_config())
    try:
        await player.play(str(long_tone_wav))
        with pytest.raises(NotPlaying, match="No previous track"):
            await player.previous()
    finally:
        await player.close()


# --------------------------------------------------------------------------- gapless + position


async def test_position_resets_when_playback_crosses_to_next_track(
    tone_wav: Path, long_tone_wav: Path
) -> None:
    """The reader runs ahead; position must follow what is *audible*, not what is decoded."""
    player = Player(PcmStreamTrack(), config=_config())
    try:
        await player.play(str(tone_wav))          # 1 s
        await player.queue.add(str(long_tone_wav))  # 5 s
        crossed = False
        for _ in range(600):
            await player._provide_frame()  # noqa: SLF001
            current = player.current
            if current is not None and current.uri == str(long_tone_wav):
                crossed = True
                break
            await asyncio.sleep(0.002)
        assert crossed, "playback never reached the second track"
        # Position restarted for the new track rather than continuing to climb.
        assert player.position < 0.5, player.position
    finally:
        await player.close()
