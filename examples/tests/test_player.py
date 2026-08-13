"""Queue semantics, player state machine, pacing and task cleanup."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from aytgcalls.config import CallConfig
from aytgcalls.exceptions import NotPlaying
from aytgcalls.player.player import Player
from aytgcalls.player.queue import TrackQueue
from aytgcalls.player.state import StateMachine
from aytgcalls.transport.track import PcmStreamTrack, frames_from_pcm
from aytgcalls.types import (
    BYTES_PER_FRAME,
    FRAME_MS,
    AudioSource,
    LoopMode,
    PlaybackState,
    StreamEndReason,
)

# --------------------------------------------------------------------------- queue


async def test_queue_is_fifo() -> None:
    queue = TrackQueue()
    await queue.extend(["a.mp3", "b.mp3", "c.mp3"])
    assert len(queue) == 3
    assert (await queue.next()).uri == "a.mp3"
    assert (await queue.next()).uri == "b.mp3"
    assert (await queue.next()).uri == "c.mp3"
    assert await queue.next() is None


async def test_queue_loop_track_repeats() -> None:
    queue = TrackQueue(loop=LoopMode.TRACK)
    await queue.extend(["a.mp3", "b.mp3"])
    assert (await queue.next()).uri == "a.mp3"
    for _ in range(3):
        assert (await queue.next()).uri == "a.mp3"
    queue.loop = LoopMode.OFF
    assert (await queue.next()).uri == "b.mp3"


async def test_queue_loop_queue_recycles() -> None:
    queue = TrackQueue(loop=LoopMode.QUEUE)
    await queue.extend(["a.mp3", "b.mp3"])
    order = [(await queue.next()).uri for _ in range(5)]
    assert order == ["a.mp3", "b.mp3", "a.mp3", "b.mp3", "a.mp3"]


async def test_queue_remove_clear_and_bounds() -> None:
    queue = TrackQueue()
    await queue.extend(["a", "b", "c"])
    assert (await queue.remove(1)).uri == "b"
    with pytest.raises(IndexError):
        await queue.remove(99)
    assert await queue.clear() == 2
    assert len(queue) == 0


async def test_queue_shuffle_preserves_membership() -> None:
    queue = TrackQueue()
    names = [f"{i}.mp3" for i in range(50)]
    await queue.extend(names)
    await queue.shuffle()
    assert sorted(item.uri for item in queue) == sorted(names)


async def test_queue_previous_uses_history() -> None:
    queue = TrackQueue()
    await queue.extend(["a", "b"])
    await queue.next()  # a
    await queue.next()  # b
    assert (await queue.previous()).uri == "a"
    assert queue.current.uri == "a"
    assert queue.items[0].uri == "b"  # b was pushed back


async def test_queue_move_and_insert() -> None:
    queue = TrackQueue()
    await queue.extend(["a", "b", "c"])
    await queue.move(2, 0)
    assert [i.uri for i in queue] == ["c", "a", "b"]
    await queue.add("z", position=0)
    assert queue.items[0].uri == "z"


async def test_queue_reset_clears_everything() -> None:
    queue = TrackQueue()
    await queue.extend(["a", "b"])
    await queue.next()
    await queue.reset()
    assert queue.current is None and not queue.history and len(queue) == 0


# --------------------------------------------------------------------------- state machine


def test_state_machine_legal_paths() -> None:
    machine = StateMachine()
    assert machine.state is PlaybackState.IDLE
    machine.transition(PlaybackState.PLAYING)
    machine.transition(PlaybackState.PAUSED)
    machine.transition(PlaybackState.PLAYING)
    machine.transition(PlaybackState.STOPPED)
    assert not machine.is_active


def test_state_machine_rejects_illegal_pause() -> None:
    machine = StateMachine()
    with pytest.raises(NotPlaying, match="Cannot pause while idle"):
        machine.transition(PlaybackState.PAUSED)


def test_state_machine_notifies_listeners() -> None:
    seen: list[tuple[PlaybackState, PlaybackState]] = []
    machine = StateMachine()
    machine.on_change(lambda old, new: seen.append((old, new)))
    machine.transition(PlaybackState.PLAYING)
    assert seen == [(PlaybackState.IDLE, PlaybackState.PLAYING)]


# --------------------------------------------------------------------------- track


def test_frames_from_pcm_pads_tail() -> None:
    frames = frames_from_pcm(b"\x01" * (BYTES_PER_FRAME + 10))
    assert len(frames) == 2
    assert all(len(f) == BYTES_PER_FRAME for f in frames)
    assert frames[1].endswith(b"\x00")


async def test_track_emits_silence_without_provider() -> None:
    track = PcmStreamTrack()
    frame = await track.recv()
    assert frame.samples == 960
    assert frame.sample_rate == 48_000
    assert frame.pts == 0
    assert bytes(frame.planes[0])[:16] == b"\x00" * 16
    second = await track.recv()
    assert second.pts == 960  # +960 samples per 20 ms frame
    track.stop()


async def test_track_paces_at_20ms(pcm_frame: bytes) -> None:
    async def provider() -> bytes:
        return pcm_frame

    track = PcmStreamTrack(provider)
    await track.recv()  # first frame primes the clock
    start = time.monotonic()
    for _ in range(10):
        await track.recv()
    elapsed = time.monotonic() - start
    assert 0.16 <= elapsed <= 0.28, f"10 frames took {elapsed:.3f}s, expected ~0.20s"
    assert track.stats.frames_encoded == 11
    track.stop()


async def test_track_counts_underruns_and_recovers(pcm_frame: bytes) -> None:
    supply = [pcm_frame, None, pcm_frame]

    async def provider() -> bytes | None:
        return supply.pop(0) if supply else None

    track = PcmStreamTrack(provider)
    for _ in range(3):
        await track.recv()
    assert track.stats.frames_encoded == 2
    assert track.stats.frames_silence == 1
    assert track.stats.underruns == 1
    track.stop()


async def test_track_survives_provider_exception() -> None:
    async def provider() -> bytes:
        raise RuntimeError("boom")

    track = PcmStreamTrack(provider)
    frame = await track.recv()  # silence, not a crash
    assert bytes(frame.planes[0]) == b"\x00" * BYTES_PER_FRAME
    track.stop()


async def test_track_truncates_and_pads_bad_frames() -> None:
    async def provider() -> bytes:
        return b"\x01" * (BYTES_PER_FRAME * 2)

    track = PcmStreamTrack(provider)
    frame = await track.recv()
    assert len(bytes(frame.planes[0])) == BYTES_PER_FRAME
    track.stop()


# --------------------------------------------------------------------------- player


def _config() -> CallConfig:
    return CallConfig(buffer_ms=200, prefetch_ms=40)


async def _drain(player: Player, frames: int = 40) -> int:
    """Pull frames the way the track would, without waiting on wall clock."""
    received = 0
    for _ in range(frames):
        frame = await player._provide_frame()  # noqa: SLF001
        if frame:
            received += 1
        await asyncio.sleep(0.005)
    return received


async def test_player_plays_real_audio(tone_wav: Path) -> None:
    track = PcmStreamTrack()
    player = Player(track, config=_config())
    await player.play(str(tone_wav))
    assert player.playback_state is PlaybackState.PLAYING
    await asyncio.sleep(0.3)
    assert await _drain(player) > 0
    await player.close()


async def test_player_pause_resume_emits_silence(tone_wav: Path) -> None:
    track = PcmStreamTrack()
    player = Player(track, config=_config())
    await player.play(str(tone_wav))
    await asyncio.sleep(0.2)
    await player.pause()
    assert player.playback_state is PlaybackState.PAUSED
    assert await player._provide_frame() is None  # noqa: SLF001
    await player.resume()
    await asyncio.sleep(0.1)
    assert await player._provide_frame() is not None  # noqa: SLF001
    await player.close()


async def test_player_pause_on_idle_raises() -> None:
    player = Player(PcmStreamTrack(), config=_config())
    with pytest.raises(NotPlaying):
        await player.pause()
    await player.close()


async def test_player_play_with_empty_queue_raises() -> None:
    player = Player(PcmStreamTrack(), config=_config())
    with pytest.raises(NotPlaying, match="queue is empty"):
        await player.play()
    await player.close()


async def test_player_stop_clears_queue_and_kills_ffmpeg(tone_wav: Path) -> None:
    player = Player(PcmStreamTrack(), config=_config())
    await player.play(str(tone_wav))
    await player.queue.add(str(tone_wav))
    await asyncio.sleep(0.15)
    await player.stop()
    assert player.playback_state is PlaybackState.STOPPED
    assert player.queue.current is None and len(player.queue) == 0
    assert player._process is None  # noqa: SLF001
    assert player._reader_task is None  # noqa: SLF001
    await player.close()


async def test_player_skip_advances(tone_wav: Path, short_mp3: Path) -> None:
    events: list[tuple[str, StreamEndReason]] = []
    player = Player(
        PcmStreamTrack(),
        config=_config(),
        on_stream_end=lambda source, reason: events.append((source.display_name, reason)),
    )
    await player.play(str(tone_wav))
    await player.queue.add(str(short_mp3))
    await asyncio.sleep(0.15)
    following = await player.skip()
    assert following is not None and following.uri == str(short_mp3)
    assert events and events[0][1] is StreamEndReason.SKIPPED
    await player.close()


async def test_player_skip_on_last_track_goes_idle(tone_wav: Path) -> None:
    player = Player(PcmStreamTrack(), config=_config())
    await player.play(str(tone_wav))
    await asyncio.sleep(0.1)
    assert await player.skip() is None
    assert player.playback_state is PlaybackState.IDLE
    await player.close()


async def test_player_reports_stream_end_on_completion(short_mp3: Path) -> None:
    ended = asyncio.Event()
    seen: list[StreamEndReason] = []

    def on_end(source: AudioSource, reason: StreamEndReason) -> None:
        seen.append(reason)
        ended.set()

    player = Player(PcmStreamTrack(), config=_config(), on_stream_end=on_end)
    await player.play(str(short_mp3))
    # Consume frames like the sender would until the source is exhausted.
    for _ in range(400):
        await player._provide_frame()  # noqa: SLF001
        if ended.is_set():
            break
        await asyncio.sleep(0.002)
    await asyncio.wait_for(ended.wait(), timeout=5)
    assert seen == [StreamEndReason.COMPLETED]
    await player.close()


async def test_player_advances_queue_gaplessly(short_mp3: Path, tone_wav: Path) -> None:
    player = Player(PcmStreamTrack(), config=_config())
    await player.play(str(short_mp3))
    await player.queue.add(str(tone_wav))
    for _ in range(600):
        await player._provide_frame()  # noqa: SLF001
        if player.queue.current and player.queue.current.uri == str(tone_wav):
            break
        await asyncio.sleep(0.002)
    assert player.queue.current is not None
    assert player.queue.current.uri == str(tone_wav)
    await player.close()


async def test_player_volume_changes_output(tone_wav: Path) -> None:
    player = Player(PcmStreamTrack(), config=_config())
    await player.play(str(tone_wav))
    await asyncio.sleep(0.25)
    await player.set_volume(0)
    assert player.volume == 0
    frame = await player._provide_frame()  # noqa: SLF001
    assert frame == b"\x00" * BYTES_PER_FRAME
    await player.close()


async def test_player_close_leaks_no_tasks(tone_wav: Path) -> None:
    before = len(asyncio.all_tasks())
    player = Player(PcmStreamTrack(), config=_config())
    await player.play(str(tone_wav))
    await asyncio.sleep(0.2)
    await player.close()
    await asyncio.sleep(0.1)
    assert len(asyncio.all_tasks()) <= before
    assert player._reader_task is None  # noqa: SLF001


async def test_player_bad_source_raises_before_playing() -> None:
    player = Player(PcmStreamTrack(), config=_config())
    from aytgcalls.exceptions import InvalidAudioSource

    with pytest.raises(InvalidAudioSource):
        await player.play("/nope/missing.mp3")
    assert player.playback_state is PlaybackState.IDLE
    await player.close()


def test_frame_geometry_matches_telegram_expectations() -> None:
    assert FRAME_MS == 20
    assert BYTES_PER_FRAME == 960 * 2 * 2
