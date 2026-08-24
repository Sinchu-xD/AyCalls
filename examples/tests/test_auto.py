"""The automatic behaviours: auto-join, auto-queue, auto-leave, loop counts, Telegram media.

The integration tests here run against the same simulated SFU as ``test_integration``, so
auto-join and auto-leave are exercised over a real ICE + DTLS-SRTP + RTP session.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

pyrogram = pytest.importorskip("pyrogram", reason="kurigram is not installed")
raw = pyrogram.raw

from aytgcalls import AyCall, AyClient, AyConfig, AyFac, AyLoop  # noqa: E402
from aytgcalls.exceptions import InvalidAudioSource, NotJoined  # noqa: E402
from aytgcalls.media.telegram import (  # noqa: E402
    TelegramDownloader,
    extract_media,
    is_telegram_media,
)
from aytgcalls.player.queue import AyQueue  # noqa: E402
from aytgcalls.types import DisconnectReason, PlaybackState  # noqa: E402

from .test_integration import CHAT_ID, FakeTelegram  # noqa: E402
from .test_loopback import FakeSfu  # noqa: E402


def _exists(path: str) -> bool:
    """Sync helper so the blocking stat is not in an async function body."""
    return os.path.exists(path)


def _isdir(path: str) -> bool:
    return os.path.isdir(path)


def _config(**kwargs: object) -> AyConfig:
    base = {
        "buffer_ms": 200,
        "prefetch_ms": 40,
        "keepalive_interval": 5.0,
        "auto_reconnect": False,
    }
    base.update(kwargs)
    return AyConfig(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- branding


def test_ay_names_are_the_public_api() -> None:
    import aytgcalls
    from aytgcalls.call.factory import GroupCallFactory
    from aytgcalls.call.group_call import GroupCall

    assert aytgcalls.AyCall is GroupCall
    assert aytgcalls.AyFac is GroupCallFactory
    assert aytgcalls.AyLoop is aytgcalls.LoopMode
    assert aytgcalls.AyConfig is aytgcalls.CallConfig
    for name in ("AyCall", "AyFac", "AyPlayer", "AyQueue", "AyConfig", "AyCreds",
                 "AyLoop", "AyState", "AySource", "AyTrack", "AyStats"):
        assert name in aytgcalls.__all__, name
        assert getattr(aytgcalls, name) is not None


# --------------------------------------------------------------------------- loop counts


async def test_loop_times_repeats_then_moves_on() -> None:
    queue = AyQueue()
    await queue.extend(["a.mp3", "b.mp3"])
    assert (await queue.next()).uri == "a.mp3"

    queue.loop = AyLoop.TIMES
    queue.loop_times = 2
    assert (await queue.next()).uri == "a.mp3"      # repeat 1
    assert queue.loop_times == 1
    assert (await queue.next()).uri == "a.mp3"      # repeat 2
    assert queue.loop_times == 0
    assert queue.loop is AyLoop.OFF                 # spent, back to normal
    assert (await queue.next()).uri == "b.mp3"


async def test_loop_queue_with_auto_shuffle_keeps_membership() -> None:
    queue = AyQueue(loop=AyLoop.QUEUE)
    queue.auto_shuffle = True
    names = [f"{i}.mp3" for i in range(8)]
    await queue.extend(names)
    for _ in range(len(names) * 3):
        track = await queue.next()
        assert track is not None
        # Invariant: the current track plus whatever is pending is always exactly the
        # original set. Shuffling reorders, it never loses or duplicates a track.
        alive = {item.uri for item in queue.items} | {track.uri}
        assert alive == set(names), f"queue lost or duplicated tracks: {alive}"
    assert len(queue) == len(names) - 1


def test_loop_accepts_counts_and_words() -> None:
    assert AyLoop.from_any(3) is AyLoop.TIMES
    assert AyLoop.from_any("3") is AyLoop.TIMES
    assert AyLoop.from_any(0) is AyLoop.OFF
    assert AyLoop.from_any(-1) is AyLoop.OFF
    assert AyLoop.from_any("repeat") is AyLoop.TIMES
    with pytest.raises(ValueError):
        AyLoop.from_any("sideways")


async def test_queue_reset_clears_the_repeat_counter() -> None:
    queue = AyQueue(loop=AyLoop.TIMES, loop_times=5)
    await queue.add("a.mp3")
    await queue.reset()
    assert queue.loop_times == 0


# --------------------------------------------------------------------------- telegram media


class FakeVoice:
    def __init__(self, mime: str = "audio/ogg") -> None:
        self.file_id = "BAADdummyFileId"
        self.mime_type = mime
        self.duration = 3


class FakeAudio(FakeVoice):
    def __init__(self) -> None:
        super().__init__("audio/mpeg")
        self.title = "My Song"
        self.performer = "Someone"


class FakeDownloadClient:
    """Stands in for a Kurigram client that can download media."""

    def __init__(self, payload: Path) -> None:
        self.payload = payload
        self.calls = 0

    async def download_media(
        self, media: object, file_name: str = "", **kwargs: object
    ) -> str:
        # the real Client accepts in_memory=, block=, progress= …
        assert kwargs.get("in_memory") is False, "expected a file download, not in-memory"
        self.calls += 1
        target = os.path.join(file_name, f"dl_{self.calls}{self.payload.suffix}")
        shutil.copyfile(self.payload, target)
        return target


def test_media_detection() -> None:
    assert is_telegram_media(SimpleNamespace(voice=FakeVoice(), audio=None))
    assert is_telegram_media(FakeVoice())                       # bare media object
    assert not is_telegram_media("song.mp3")
    assert not is_telegram_media("https://example.com/a.mp3")
    assert not is_telegram_media(Path("song.mp3"))
    assert extract_media(SimpleNamespace(voice=None, audio=None)) is None


def test_media_priority_prefers_voice_then_audio() -> None:
    voice, audio = FakeVoice(), FakeAudio()
    message = SimpleNamespace(voice=voice, audio=audio)
    assert extract_media(message) is voice
    assert extract_media(SimpleNamespace(voice=None, audio=audio)) is audio


async def test_downloads_voice_and_cleans_up(tone_wav: Path) -> None:
    client = FakeDownloadClient(tone_wav)
    downloader = TelegramDownloader(client)  # type: ignore[arg-type]
    try:
        track = await downloader.resolve(SimpleNamespace(voice=FakeVoice()))
        assert _exists(track.uri)
        assert track.title == "voice message"
        assert downloader.owns(track)
        downloader.release(track)
        assert not _exists(track.uri), "temp download was not deleted"
    finally:
        downloader.cleanup()


async def test_download_uses_audio_title(tone_wav: Path) -> None:
    downloader = TelegramDownloader(FakeDownloadClient(tone_wav))  # type: ignore[arg-type]
    try:
        track = await downloader.resolve(SimpleNamespace(voice=None, audio=FakeAudio()))
        assert track.title == "My Song"
    finally:
        downloader.cleanup()


async def test_cleanup_removes_everything(tone_wav: Path) -> None:
    downloader = TelegramDownloader(FakeDownloadClient(tone_wav))  # type: ignore[arg-type]
    tracks = [await downloader.resolve(SimpleNamespace(voice=FakeVoice())) for _ in range(3)]
    directory = downloader.directory
    downloader.cleanup()
    assert all(not _exists(t.uri) for t in tracks)
    assert not _isdir(directory)


async def test_non_audio_media_is_rejected(tone_wav: Path) -> None:
    downloader = TelegramDownloader(FakeDownloadClient(tone_wav))  # type: ignore[arg-type]
    try:
        with pytest.raises(InvalidAudioSource, match="not audio or video"):
            await downloader.resolve(SimpleNamespace(document=FakeVoice("application/pdf")))
        with pytest.raises(InvalidAudioSource, match="no Telegram media"):
            await downloader.resolve(SimpleNamespace(text="hello"))
    finally:
        downloader.cleanup()


# --------------------------------------------------------------------------- auto join/queue/leave


async def test_play_auto_joins_without_calling_join(long_tone_wav: Path) -> None:
    """The whole point: no manual join(), just play()."""
    sfu = FakeSfu()
    await sfu.gather()
    client = FakeTelegram(sfu)
    call = AyCall(client, CHAT_ID, config=_config(auto_leave=False))
    client.group_call = call
    try:
        assert not call.is_connected
        track, started = await call.play(str(long_tone_wav))
        assert started is True
        assert call.is_connected, "play() should have joined the voice chat"
        assert call.ssrc is not None
        assert any(
            isinstance(r, raw.functions.phone.JoinGroupCall) for r in client.requests
        ), "phone.joinGroupCall was never sent"
        await asyncio.sleep(0.4)
        assert (await call.get_stats()).packets_sent > 0
    finally:
        await call.leave()
        await sfu.close()
        await client.cleanup()


async def test_play_auto_queues_when_busy(long_tone_wav: Path, short_mp3: Path) -> None:
    sfu = FakeSfu()
    await sfu.gather()
    client = FakeTelegram(sfu)
    call = AyCall(client, CHAT_ID, config=_config(auto_leave=False))
    client.group_call = call
    try:
        _, started_first = await call.play(str(long_tone_wav))
        _, started_second = await call.play(str(short_mp3))
        _, started_third = await call.play(str(short_mp3))
        assert started_first is True
        assert (started_second, started_third) == (False, False)
        assert len(call.queue) == 2
        # force=True jumps the queue instead
        track, started = await call.play(str(short_mp3), force=True)
        assert started is True
        assert call.player.current is not None
        assert call.player.current.uri == str(short_mp3)
    finally:
        await call.leave()
        await sfu.close()
        await client.cleanup()


async def test_play_without_chat_id_anywhere_is_rejected() -> None:
    call = AyCall(FakeTelegram(None))
    with pytest.raises(NotJoined, match="No chat id known"):
        await call.play("song.mp3")


async def test_auto_leave_when_the_queue_runs_out(short_mp3: Path) -> None:
    """Song khatam -> call khatam, without anyone calling leave()."""
    sfu = FakeSfu()
    await sfu.gather()
    client = FakeTelegram(sfu)
    call = AyCall(client, CHAT_ID, config=_config(auto_leave=True, auto_leave_delay=0.3))
    client.group_call = call
    reasons: list[DisconnectReason] = []

    @call.on_disconnect
    async def _(_call: AyCall, reason: DisconnectReason) -> None:
        reasons.append(reason)

    try:
        await call.play(str(short_mp3))          # 0.5 s of audio
        for _ in range(80):                      # wait for playout + grace period
            if not call.is_connected:
                break
            await asyncio.sleep(0.1)
        assert not call.is_connected, "auto-leave never fired"
        assert reasons == [DisconnectReason.QUEUE_FINISHED]
        assert client.left_source is not None, "phone.leaveGroupCall was not sent"
    finally:
        await call.leave()
        await sfu.close()
        await client.cleanup()


async def test_auto_leave_is_cancelled_by_a_new_request(short_mp3: Path,
                                                        long_tone_wav: Path) -> None:
    """A request during the grace period must keep us in the call."""
    sfu = FakeSfu()
    await sfu.gather()
    client = FakeTelegram(sfu)
    call = AyCall(client, CHAT_ID, config=_config(auto_leave=True, auto_leave_delay=1.5))
    client.group_call = call
    try:
        await call.play(str(short_mp3))
        for _ in range(60):                       # wait until the queue is exhausted
            if call.playback_state is PlaybackState.IDLE:
                break
            await asyncio.sleep(0.05)
        assert call.is_connected
        await call.play(str(long_tone_wav))        # arrives during the grace period
        await asyncio.sleep(1.8)
        assert call.is_connected, "auto-leave should have been cancelled"
        assert call.is_playing
    finally:
        await call.leave()
        await sfu.close()
        await client.cleanup()


async def test_auto_leave_can_be_disabled(short_mp3: Path) -> None:
    sfu = FakeSfu()
    await sfu.gather()
    client = FakeTelegram(sfu)
    call = AyCall(client, CHAT_ID, config=_config(auto_leave=False, auto_leave_delay=0.2))
    client.group_call = call
    try:
        await call.play(str(short_mp3))
        for _ in range(60):
            if call.playback_state is PlaybackState.IDLE:
                break
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.6)
        assert call.is_connected, "auto_leave=False must keep us in the call"
    finally:
        await call.leave()
        await sfu.close()
        await client.cleanup()


async def test_stop_leaves_the_call(long_tone_wav: Path) -> None:
    sfu = FakeSfu()
    await sfu.gather()
    client = FakeTelegram(sfu)
    call = AyCall(client, CHAT_ID, config=_config(auto_leave=False))
    client.group_call = call
    try:
        await call.play(str(long_tone_wav))
        await asyncio.sleep(0.3)
        await call.stop()
        assert not call.is_connected, "stop() should leave the voice chat"
        assert call.playback_state is PlaybackState.STOPPED
    finally:
        await sfu.close()
        await client.cleanup()


async def test_telegram_voice_plays_end_to_end(tone_wav: Path) -> None:
    """A voice message goes straight into play(): downloaded, played, then deleted."""
    sfu = FakeSfu()
    await sfu.gather()
    client = FakeTelegram(sfu)
    call = AyCall(client, CHAT_ID, config=_config(auto_leave=False))
    client.group_call = call
    # give the fake client a download_media implementation
    downloader = FakeDownloadClient(tone_wav)
    client.download_media = downloader.download_media  # type: ignore[attr-defined]
    try:
        track, started = await call.play(SimpleNamespace(voice=FakeVoice()))
        assert started is True
        assert downloader.calls == 1, "media was not downloaded"
        assert track.title == "voice message"
        assert _exists(track.uri)
        temp_path = track.uri

        await asyncio.sleep(0.5)
        assert (await call.get_stats()).packets_sent > 0
        await call.leave()
        assert not _exists(temp_path), "temp download was not cleaned up on leave"
    finally:
        await sfu.close()
        await client.cleanup()


# --------------------------------------------------------------------------- AyFac


async def test_ayfac_play_is_a_one_liner(long_tone_wav: Path) -> None:
    sfu = FakeSfu()
    await sfu.gather()
    client = FakeTelegram(sfu)
    fac = AyFac(client, config=_config(auto_leave=False))
    # the factory creates the call; wire it up so the fake SFU can answer the join
    call = fac.create(CHAT_ID)
    client.group_call = call
    try:
        track, started = await fac.play(CHAT_ID, str(long_tone_wav))
        assert started is True and track.uri == str(long_tone_wav)
        assert CHAT_ID in fac and fac[CHAT_ID].is_connected

        await asyncio.sleep(0.3)
        assert await fac.loop(CHAT_ID, 2) is AyLoop.TIMES
        await fac.pause(CHAT_ID)
        await fac.resume(CHAT_ID)
        await fac.set_volume(CHAT_ID, 70)
        assert fac.now_playing(CHAT_ID).title.endswith("long.wav")

        assert await fac.stop(CHAT_ID) is True
        assert CHAT_ID not in fac
        assert await fac.stop(CHAT_ID) is False
    finally:
        await fac.leave_all()
        await sfu.close()
        await client.cleanup()


async def test_ayfac_controls_reject_unknown_chats() -> None:
    fac = AyFac(FakeTelegram(None))
    with pytest.raises(NotJoined):
        await fac.pause(-100999)
    with pytest.raises(KeyError):
        _ = fac[-100999]
    assert fac.now_playing(-100999) is None


# --------------------------------------------------------------------------- AyClient


async def test_ayclient_play_auto_joins_and_queues(long_tone_wav: Path, short_mp3: Path) -> None:
    """AyClient.play() handles join, queue, and playback in one call."""
    from aytgcalls import AyClient  # noqa: E402

    sfu = FakeSfu()
    await sfu.gather()
    tg = FakeTelegram(sfu)
    fac = AyFac(tg, config=_config(auto_leave=False))
    client = AyClient(tg, config=_config(auto_leave=False))

    # wire up the internal factory so the fake SFU can answer the join
    client._factory = fac
    call = fac.create(CHAT_ID)
    tg.group_call = call

    try:
        # --- auto-join via play() ---
        track, started = await client.play(CHAT_ID, str(long_tone_wav))
        assert started is True
        assert call.is_connected
        assert call.player.current is not None

        # --- auto-queue on second call ---
        track2, started2 = await client.play(CHAT_ID, str(short_mp3))
        assert started2 is False
        assert len(call.queue) == 1

        # --- controls work through AyClient ---
        await client.pause(CHAT_ID)
        await client.resume(CHAT_ID)
        await client.seek(CHAT_ID, 5)
        await client.skip(CHAT_ID)
        assert call.player.current.uri == str(short_mp3)

        # --- introspection ---
        assert client.position(CHAT_ID) >= 0
        assert client.playback_state(CHAT_ID) is not None
        assert client.now_playing(CHAT_ID) is not None
        assert client.is_connected(CHAT_ID) is True

        # --- end tears down ---
        await client.end(CHAT_ID)
        assert not call.is_connected
    finally:
        await fac.leave_all()
        await sfu.close()
        await tg.cleanup()


# --------------------------------------------------------------------------- SFU drop


async def test_keepalive_notices_the_sfu_dropped_us(long_tone_wav: Path) -> None:
    """phone.checkGroupCall stops listing our source -> we treat the call as gone.

    With auto_reconnect off this must end the call cleanly rather than sit there sending
    RTP into a session Telegram has forgotten.
    """
    sfu = FakeSfu()
    await sfu.gather()
    client = FakeTelegram(sfu)
    call = AyCall(
        client,
        CHAT_ID,
        config=_config(auto_leave=False, keepalive_interval=0.15, auto_reconnect=False),
    )
    client.group_call = call
    reasons: list[DisconnectReason] = []

    @call.on_disconnect
    async def _(_call: AyCall, reason: DisconnectReason) -> None:
        reasons.append(reason)

    try:
        await call.play(str(long_tone_wav))
        assert call.is_connected
        # From now on the "SFU" claims it has never heard of our source.
        client.forget_sources = True
        for _ in range(60):
            if not call.is_connected:
                break
            await asyncio.sleep(0.1)
        assert not call.is_connected, "keepalive never noticed the drop"
        assert DisconnectReason.SFU_TIMEOUT in reasons
    finally:
        await call.leave()
        await sfu.close()
        await client.cleanup()
