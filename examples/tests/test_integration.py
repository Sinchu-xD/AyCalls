"""Full public-API integration test: GroupCall.join -> play -> leave.

Telegram is replaced by a fake Kurigram client that returns **real TL objects**, and its
SFU is replaced by the local aiortc peer from ``test_loopback``. Everything in between is
the production code path: discovery, ``phone.joinGroupCall``, JSON parsing, ICE, DTLS,
SRTP, FFmpeg decode, pacing, Opus encode and RTP.

This is the closest thing to an end-to-end test that can run without a Telegram account.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pyrogram = pytest.importorskip("pyrogram", reason="kurigram is not installed")
raw = pyrogram.raw

from aiortc.rtcdtlstransport import RTCDtlsFingerprint  # noqa: E402

from aytgcalls import CallConfig, GroupCall  # noqa: E402
from aytgcalls.exceptions import AlreadyJoined, GroupCallNotFound  # noqa: E402
from aytgcalls.types import DisconnectReason, LoopMode, PlaybackState  # noqa: E402

from .test_loopback import FakeSfu  # noqa: E402

CHAT_ID = -1001234567890
INPUT_CALL = raw.types.InputGroupCall(id=555, access_hash=777)


class FakeTelegram:
    """A Kurigram client whose SFU is a local aiortc peer."""

    def __init__(self, sfu: FakeSfu | None, *, has_call: bool = True) -> None:
        self.sfu = sfu
        self.has_call = has_call
        self.me = SimpleNamespace(is_bot=False, id=42, username="assistant")
        self.requests: list[Any] = []
        self.handlers: list[Any] = []
        self.group_call: GroupCall | None = None
        self.left_source: int | None = None
        self._accept_task: asyncio.Task[None] | None = None

    # -- Kurigram surface ---------------------------------------------------------

    async def get_me(self) -> Any:
        return self.me

    async def resolve_peer(self, chat_id: int | str) -> Any:
        return raw.types.InputPeerChannel(channel_id=abs(int(chat_id)) % 10**9, access_hash=1)

    def add_handler(self, handler: Any, group: int = 0) -> None:
        self.handlers.append(handler)

    def remove_handler(self, handler: Any, group: int = 0) -> None:
        if handler in self.handlers:
            self.handlers.remove(handler)

    async def invoke(self, request: Any) -> Any:
        self.requests.append(request)
        if isinstance(request, raw.functions.channels.GetFullChannel):
            return SimpleNamespace(
                full_chat=SimpleNamespace(call=INPUT_CALL if self.has_call else None)
            )
        if isinstance(request, raw.functions.phone.GetGroupCall):
            return SimpleNamespace(
                call=SimpleNamespace(
                    participants_count=3, schedule_date=None, rtmp_stream=False, title="Test VC"
                )
            )
        if isinstance(request, raw.functions.phone.JoinGroupCall):
            return await self._join(request)
        if isinstance(request, raw.functions.phone.CheckGroupCall):
            return list(request.sources)
        if isinstance(request, raw.functions.phone.LeaveGroupCall):
            self.left_source = request.source
            return None
        if isinstance(request, raw.functions.phone.EditGroupCallParticipant):
            return None
        raise AssertionError(f"unexpected request {type(request).__name__}")

    # -- the "SFU" ------------------------------------------------------------------

    async def _join(self, request: Any) -> Any:
        assert self.sfu is not None and self.group_call is not None
        payload = json.loads(request.params.data)
        # Exactly the audio-only payload documented in PROTOCOL.md §2.
        assert set(payload) == {"ssrc", "ufrag", "pwd", "fingerprints"}
        assert 0 < payload["ssrc"] < 2**31
        assert payload["fingerprints"][0]["setup"] == "active"

        transport = self.group_call._transport  # noqa: SLF001
        assert transport is not None
        for candidate in transport.local_candidates:
            if candidate.protocol == "udp" and ":" not in candidate.ip:
                await self.sfu.ice.addRemoteCandidate(candidate)
        await self.sfu.ice.addRemoteCandidate(None)

        fingerprint = RTCDtlsFingerprint(
            algorithm=payload["fingerprints"][0]["hash"],
            value=payload["fingerprints"][0]["fingerprint"],
        )
        # The real SFU answers while we are still connecting, so do the same.
        self._accept_task = asyncio.ensure_future(
            self.sfu.accept(payload["ufrag"], payload["pwd"], fingerprint)
        )
        return raw.types.Updates(
            updates=[
                raw.types.UpdateGroupCallConnection(
                    params=raw.types.DataJSON(data=json.dumps(self.sfu.join_response_json())),
                    presentation=False,
                )
            ],
            users=[], chats=[], date=0, seq=0,
        )

    async def cleanup(self) -> None:
        if self._accept_task is not None:
            self._accept_task.cancel()
            try:
                await self._accept_task
            except (asyncio.CancelledError, Exception):
                pass


def _config() -> CallConfig:
    return CallConfig(buffer_ms=200, prefetch_ms=40, keepalive_interval=0.5, auto_reconnect=False)


async def test_join_play_and_leave_end_to_end(tone_wav: Path) -> None:
    sfu = FakeSfu()
    await sfu.gather()
    client = FakeTelegram(sfu)
    call = GroupCall(client, config=_config())
    client.group_call = call

    disconnects: list[DisconnectReason] = []

    @call.on_disconnect
    async def _(_call: GroupCall, reason: DisconnectReason) -> None:
        disconnects.append(reason)

    try:
        await call.join(CHAT_ID)
        assert call.is_connected
        assert call.ssrc is not None and 0 < call.ssrc < 2**31

        stats = await call.get_stats()
        assert stats.ice_state in {"completed", "connected"}
        assert stats.dtls_state == "connected"

        await sfu.start_receiver(call.ssrc)
        await call.play(str(tone_wav))
        assert call.playback_state is PlaybackState.PLAYING

        # The far end must decode audio that came from the mp3/wav we asked for.
        frame = await asyncio.wait_for(sfu.receiver.track.recv(), timeout=20)
        assert frame.sample_rate == 48_000

        await asyncio.sleep(1.0)
        stats = await call.get_stats()
        assert stats.packets_sent >= 20, stats.as_dict()
        assert stats.frames_encoded > 0, "only silence reached the encoder"

        # keepalive must have run at least once
        assert any(
            isinstance(request, raw.functions.phone.CheckGroupCall)
            for request in client.requests
        )
    finally:
        source_before_leave = call.ssrc
        await call.leave()
        await sfu.close()
        await client.cleanup()

    assert not call.is_connected
    assert client.left_source == source_before_leave  # phone.leaveGroupCall got our SSRC
    assert disconnects == [DisconnectReason.REQUESTED]
    assert client.handlers == []  # update handler removed


async def test_pause_resume_skip_over_a_live_transport(tone_wav: Path, short_mp3: Path) -> None:
    sfu = FakeSfu()
    await sfu.gather()
    client = FakeTelegram(sfu)
    call = GroupCall(client, config=_config())
    client.group_call = call
    try:
        await call.join(CHAT_ID)
        await call.play(str(tone_wav))
        await call.queue.add(str(short_mp3))
        await asyncio.sleep(0.3)

        await call.pause()
        assert call.playback_state is PlaybackState.PAUSED
        await call.resume()
        assert call.playback_state is PlaybackState.PLAYING

        following = await call.skip()
        assert following is not None and following.uri == str(short_mp3)

        await call.set_volume(50)
        assert call.player.volume == 50
        await call.set_volume(50, server_side=True)
        edits = [
            r for r in client.requests
            if isinstance(r, raw.functions.phone.EditGroupCallParticipant)
        ]
        assert edits and edits[-1].volume == 5000  # 50% -> Telegram's 0..20000 scale

        await call.stop()
        assert call.playback_state is PlaybackState.STOPPED
    finally:
        await call.leave()
        await sfu.close()
        await client.cleanup()


async def test_playback_controls_over_a_live_transport(
    long_tone_wav: Path, short_mp3: Path
) -> None:
    """seek / forward / rewind / replay / previous / add / loop / now_playing, live."""
    sfu = FakeSfu()
    await sfu.gather()
    client = FakeTelegram(sfu)
    call = GroupCall(client, config=_config())
    client.group_call = call
    try:
        await call.join(CHAT_ID)
        await sfu.start_receiver(call.ssrc)

        # add() on an idle call starts playback; on a busy call it queues.
        track, started = await call.add(str(long_tone_wav))
        assert started is True and track.uri == str(long_tone_wav)
        queued, started = await call.add(str(short_mp3))
        assert started is False and len(call.queue) == 1

        # real audio reaches the far end
        frame = await asyncio.wait_for(sfu.receiver.track.recv(), timeout=20)
        assert frame.sample_rate == 48_000
        await asyncio.sleep(0.6)
        assert call.position > 0
        assert call.is_playing and not call.is_paused

        # duration is discovered in the background
        for _ in range(40):
            if call.duration is not None:
                break
            await asyncio.sleep(0.05)
        assert call.duration == pytest.approx(5.0, abs=0.3)

        # seeking family
        assert await call.seek(2.0) == pytest.approx(2.0)
        assert call.position == pytest.approx(2.0, abs=0.05)
        assert await call.forward(1.0) == pytest.approx(3.0, abs=0.05)
        assert await call.rewind(2.0) == pytest.approx(1.0, abs=0.05)
        assert await call.replay() == 0.0

        # loop modes from plain strings, as a chat command would send them
        assert call.set_loop("one") is LoopMode.TRACK
        assert call.set_loop("all") is LoopMode.QUEUE
        call.loop = "off"
        assert call.loop is LoopMode.OFF

        # now_playing snapshot
        snapshot = call.now_playing
        assert snapshot.title.endswith("long.wav")
        assert snapshot.state is PlaybackState.PLAYING
        assert snapshot.queued == 1
        assert "long.wav" in str(snapshot)
        assert snapshot.progress is not None

        # pause/resume/skip/previous still work on the live transport
        await call.pause()
        assert call.is_paused
        await call.resume()
        following = await call.skip()
        assert following is not None and following.uri == str(short_mp3)
        earlier = await call.previous()
        assert earlier is not None and earlier.uri == str(long_tone_wav)

        await call.shuffle()
        assert await call.clear_queue() >= 0

        # RTP never stopped through any of that: sample the counter over time rather
        # than asserting an arbitrary total, since the sender is paced at 50 packets/s.
        before = (await call.get_stats()).packets_sent
        await asyncio.sleep(0.6)
        after = (await call.get_stats()).packets_sent
        assert before > 0, "no RTP at all"
        assert after - before >= 15, f"RTP stalled: {before} -> {after}"
    finally:
        await call.leave()
        await sfu.close()
        await client.cleanup()


async def test_end_stops_and_leaves(long_tone_wav: Path) -> None:
    """end() is the single-call teardown: stop playback + leave."""
    sfu = FakeSfu()
    await sfu.gather()
    client = FakeTelegram(sfu)
    call = GroupCall(client, config=_config())
    client.group_call = call
    try:
        await call.join(CHAT_ID)
        await call.play(str(long_tone_wav))
        await asyncio.sleep(0.3)
        await call.end()
        assert not call.is_connected
        assert call.playback_state is PlaybackState.STOPPED
        assert client.left_source is not None
        await call.end()  # idempotent
    finally:
        await sfu.close()
        await client.cleanup()


async def test_mute_and_unmute_hit_the_api(long_tone_wav: Path) -> None:
    sfu = FakeSfu()
    await sfu.gather()
    client = FakeTelegram(sfu)
    call = GroupCall(client, config=_config())
    client.group_call = call
    try:
        await call.join(CHAT_ID)
        await call.mute()
        await call.unmute()
        edits = [
            r for r in client.requests
            if isinstance(r, raw.functions.phone.EditGroupCallParticipant)
        ]
        assert [e.muted for e in edits] == [True, False]
    finally:
        await call.leave()
        await sfu.close()
        await client.cleanup()


async def test_double_join_is_rejected(tone_wav: Path) -> None:
    sfu = FakeSfu()
    await sfu.gather()
    client = FakeTelegram(sfu)
    call = GroupCall(client, config=_config())
    client.group_call = call
    try:
        await call.join(CHAT_ID)
        with pytest.raises(AlreadyJoined):
            await call.join(CHAT_ID)
    finally:
        await call.leave()
        await sfu.close()
        await client.cleanup()


async def test_missing_voice_chat_is_reported() -> None:
    client = FakeTelegram(None, has_call=False)
    call = GroupCall(client, config=_config())
    client.group_call = call
    with pytest.raises(GroupCallNotFound, match="No active voice chat"):
        await call.join(CHAT_ID)
    assert not call.is_connected
    await call.leave()


async def test_leaving_twice_is_safe(tone_wav: Path) -> None:
    sfu = FakeSfu()
    await sfu.gather()
    client = FakeTelegram(sfu)
    call = GroupCall(client, config=_config())
    client.group_call = call
    await call.join(CHAT_ID)
    await call.leave()
    await call.leave()
    await sfu.close()
    await client.cleanup()


async def test_no_tasks_or_processes_leak_after_leave(tone_wav: Path) -> None:
    baseline = len(asyncio.all_tasks())
    sfu = FakeSfu()
    await sfu.gather()
    client = FakeTelegram(sfu)
    call = GroupCall(client, config=_config())
    client.group_call = call

    await call.join(CHAT_ID)
    await call.play(str(tone_wav))
    await asyncio.sleep(0.5)
    await call.leave()
    await sfu.close()
    await client.cleanup()
    await asyncio.sleep(0.4)

    assert call.player._reader_task is None  # noqa: SLF001
    assert call.player._process is None  # noqa: SLF001
    assert len(asyncio.all_tasks()) <= baseline, sorted(
        task.get_coro().__qualname__ for task in asyncio.all_tasks()
    )
