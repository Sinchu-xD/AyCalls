"""Signaling, bot guard, reconnect backoff and GroupCall guard rails.

The signaling tests run against a fake Kurigram client that returns real TL objects, so
the request construction is checked against the actual schema without any network.
"""

from __future__ import annotations

import asyncio
import json
import random
from typing import Any

import pytest

pyrogram = pytest.importorskip("pyrogram", reason="kurigram is not installed")
raw = pyrogram.raw

from aytgcalls.call.reconnect import BackoffPolicy, ReconnectManager  # noqa: E402
from aytgcalls.exceptions import (  # noqa: E402
    BotClientNotAllowed,
    NotJoined,
    TelegramCallError,
    TransportError,
)
from aytgcalls.telegram.client import ensure_user_session  # noqa: E402
from aytgcalls.telegram.signaling import GroupCallSignaling  # noqa: E402

INPUT_CALL = raw.types.InputGroupCall(id=123456789, access_hash=987654321)


class FakeUser:
    def __init__(self, *, is_bot: bool = False, user_id: int = 42) -> None:
        self.is_bot = is_bot
        self.id = user_id
        self.username = "assistant"


class FakeClient:
    """Records every invoked TL request and returns canned responses."""

    def __init__(self, responses: list[Any] | None = None, *, is_bot: bool = False) -> None:
        self.requests: list[Any] = []
        self.responses = responses or []
        self.me = FakeUser(is_bot=is_bot)

    async def invoke(self, request: Any) -> Any:
        self.requests.append(request)
        if not self.responses:
            return None
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def _join_updates(params: dict[str, Any]) -> Any:
    return raw.types.Updates(
        updates=[
            raw.types.UpdateGroupCallConnection(
                params=raw.types.DataJSON(data=json.dumps(params)), presentation=False
            )
        ],
        users=[],
        chats=[],
        date=0,
        seq=0,
    )


# --------------------------------------------------------------------------- bot guard


async def test_bot_session_is_rejected() -> None:
    with pytest.raises(BotClientNotAllowed, match="cannot join a Telegram group voice chat"):
        await ensure_user_session(FakeClient(is_bot=True))


async def test_user_session_is_accepted() -> None:
    me = await ensure_user_session(FakeClient())
    assert me.id == 42


async def test_bot_guard_message_explains_the_workaround() -> None:
    with pytest.raises(BotClientNotAllowed) as info:
        await ensure_user_session(FakeClient(is_bot=True))
    text = str(info.value)
    assert "STRING_SESSION" in text
    assert "command interface" in text


# --------------------------------------------------------------------------- signaling


async def test_join_builds_correct_tl_request(telegram_join_response: dict) -> None:
    client = FakeClient([_join_updates(telegram_join_response)])
    signaling = GroupCallSignaling(client)
    params = json.dumps({"ssrc": 1, "ufrag": "a", "pwd": "b", "fingerprints": []})

    result = await signaling.join(INPUT_CALL, params_json=params, muted=False)

    request = client.requests[0]
    assert isinstance(request, raw.functions.phone.JoinGroupCall)
    assert request.call is INPUT_CALL
    assert isinstance(request.join_as, raw.types.InputPeerSelf)
    assert request.params.data == params
    assert request.muted is False
    assert request.video_stopped is True
    assert json.loads(result.params)["transport"]["ufrag"] == "9aBc"


async def test_join_ignores_presentation_connection(telegram_join_response: dict) -> None:
    """The presentation connection belongs to screen sharing, not to our audio."""
    updates = raw.types.Updates(
        updates=[
            raw.types.UpdateGroupCallConnection(
                params=raw.types.DataJSON(data='{"presentation": true}'), presentation=True
            ),
            raw.types.UpdateGroupCallConnection(
                params=raw.types.DataJSON(data=json.dumps(telegram_join_response)),
                presentation=False,
            ),
        ],
        users=[], chats=[], date=0, seq=0,
    )
    signaling = GroupCallSignaling(FakeClient([updates]))
    result = await signaling.join(INPUT_CALL, params_json="{}")
    assert "transport" in json.loads(result.params)


async def test_join_without_connection_update_raises() -> None:
    empty = raw.types.Updates(updates=[], users=[], chats=[], date=0, seq=0)
    signaling = GroupCallSignaling(FakeClient([empty]))
    with pytest.raises(TransportError, match="no UpdateGroupCallConnection"):
        await signaling.join(INPUT_CALL, params_json="{}")


async def test_rpc_errors_are_wrapped_with_an_explanation() -> None:
    error = pyrogram.errors.RPCError("GROUPCALL_SSRC_DUPLICATE_MUCH")
    error.ID = "GROUPCALL_SSRC_DUPLICATE_MUCH"
    signaling = GroupCallSignaling(FakeClient([error]))
    with pytest.raises(TelegramCallError) as info:
        await signaling.join(INPUT_CALL, params_json="{}")
    message = str(info.value)
    assert "phone.joinGroupCall failed" in message
    assert "already registered with the SFU" in message


@pytest.mark.parametrize(
    "error_id",
    ["DATA_JSON_INVALID", "GROUPCALL_INVALID", "CHAT_ADMIN_REQUIRED", "JOIN_AS_PEER_INVALID"],
)
async def test_every_known_rpc_error_has_a_hint(error_id: str) -> None:
    error = pyrogram.errors.RPCError(error_id)
    error.ID = error_id
    signaling = GroupCallSignaling(FakeClient([error]))
    with pytest.raises(TelegramCallError) as info:
        await signaling.leave(INPUT_CALL, 1)
    assert TelegramCallError.EXPLANATIONS[error_id].split(".")[0] in str(info.value)


async def test_check_returns_live_sources() -> None:
    signaling = GroupCallSignaling(FakeClient([[111, 222]]))
    assert await signaling.check(INPUT_CALL, [111]) == [111, 222]


async def test_leave_sends_our_ssrc() -> None:
    client = FakeClient([None])
    await GroupCallSignaling(client).leave(INPUT_CALL, 555)
    request = client.requests[0]
    assert isinstance(request, raw.functions.phone.LeaveGroupCall)
    assert request.source == 555


async def test_edit_participant_clamps_volume() -> None:
    client = FakeClient([None, None])
    signaling = GroupCallSignaling(client)
    await signaling.edit_participant(INPUT_CALL, volume=999_999)
    await signaling.edit_participant(INPUT_CALL, volume=-5)
    assert client.requests[0].volume == 20_000
    assert client.requests[1].volume == 0
    assert isinstance(client.requests[0].participant, raw.types.InputPeerSelf)


# --------------------------------------------------------------------------- reconnect


def test_backoff_is_exponential_and_capped() -> None:
    policy = BackoffPolicy(initial_delay=1, max_delay=10, multiplier=2, jitter=0)
    assert [policy.delay_for(n) for n in range(1, 7)] == [1, 2, 4, 8, 10, 10]


def test_backoff_jitter_stays_within_bounds() -> None:
    policy = BackoffPolicy(initial_delay=4, max_delay=100, jitter=0.3)
    rng = random.Random(0)
    for _ in range(200):
        delay = policy.delay_for(1, rng=rng)
        assert 2.8 - 1e-9 <= delay <= 5.2 + 1e-9


def test_backoff_attempt_is_one_based() -> None:
    with pytest.raises(ValueError, match="1-based"):
        BackoffPolicy().delay_for(0)


def test_backoff_respects_max_attempts() -> None:
    policy = BackoffPolicy(max_attempts=3)
    assert policy.should_retry(3)
    assert not policy.should_retry(4)
    assert BackoffPolicy(max_attempts=0).should_retry(10_000)  # 0 == unlimited


async def test_reconnect_retries_until_success() -> None:
    attempts = {"n": 0}

    async def connect() -> None:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise ConnectionError("nope")

    manager = ReconnectManager(
        connect, policy=BackoffPolicy(initial_delay=0.01, max_delay=0.02, jitter=0)
    )
    await manager.run()
    assert attempts["n"] == 3
    assert manager.attempts == 3
    assert not manager.is_running


async def test_reconnect_gives_up_and_reports() -> None:
    gave_up: list[BaseException] = []

    async def connect() -> None:
        raise ConnectionError("always down")

    manager = ReconnectManager(
        connect,
        policy=BackoffPolicy(initial_delay=0.01, max_delay=0.01, jitter=0, max_attempts=3),
        on_give_up=lambda exc: gave_up.append(exc),
    )
    await manager.run()
    assert manager.attempts == 3
    assert isinstance(gave_up[0], ConnectionError)


async def test_reconnect_is_cancellable() -> None:
    async def connect() -> None:
        raise ConnectionError("down")

    manager = ReconnectManager(
        connect, policy=BackoffPolicy(initial_delay=5, jitter=0, max_attempts=10)
    )
    manager.start()
    await asyncio.sleep(0.05)
    await manager.cancel()
    assert not manager.is_running


# --------------------------------------------------------------------------- GroupCall


async def test_controls_before_join_are_rejected() -> None:
    """pause/resume/skip need a call; play() only needs to know *which* chat."""
    from aytgcalls import AyCall

    call = AyCall(FakeClient())
    for coro in (call.pause(), call.resume(), call.skip()):
        with pytest.raises(NotJoined, match="Not joined"):
            await coro
    # play() auto-joins, so the only thing it can be missing is the chat id.
    with pytest.raises(NotJoined, match="No chat id known"):
        await call.play("x.mp3")
    assert call.ssrc is None
    assert not call.is_connected


async def test_join_with_bot_client_is_rejected() -> None:
    from aytgcalls import GroupCall

    call = GroupCall(FakeClient(is_bot=True))
    with pytest.raises(BotClientNotAllowed):
        await call.join(-1001234567890)


async def test_debug_snapshot_is_json_and_secret_free() -> None:
    from aytgcalls import GroupCall

    call = GroupCall(FakeClient())
    snapshot = json.loads(call.debug_snapshot())
    assert snapshot["joined"] is False
    assert snapshot["playback_state"] == "idle"
    assert "access_hash" not in call.debug_snapshot()


async def test_factory_tracks_calls() -> None:
    from aytgcalls import GroupCallFactory

    factory = GroupCallFactory(FakeClient())
    call = factory.create(-100123)
    assert -100123 in factory
    assert factory.get(-100123) is call
    assert len(factory) == 1
    await factory.leave_all()
    assert len(factory) == 0
