"""Internals that the feature tests do not reach.

Three genuinely risky areas live here:

* the **update router**, which decides "the call ended" / "we were kicked" from raw TL
  updates — wrong classification means a bot that never notices it was removed;
* the **numpy-free volume path**, which is what runs on hosts without numpy, so the two
  gain implementations are asserted to agree bit for bit;
* the **buffer's awaiting paths** (timeouts, EOF, backpressure edges).
"""

from __future__ import annotations

import asyncio
import logging

import pytest

pyrogram = pytest.importorskip("pyrogram", reason="kurigram is not installed")
raw = pyrogram.raw

import aytgcalls.media.volume as volume_module  # noqa: E402
from aytgcalls.logger import dump_signaling, enable_debug, get_logger, redact  # noqa: E402
from aytgcalls.media.buffer import PcmRingBuffer  # noqa: E402
from aytgcalls.media.volume import apply_gain  # noqa: E402
from aytgcalls.telegram.updates import CallUpdateEvent, GroupCallUpdateRouter  # noqa: E402
from aytgcalls.types import BYTES_PER_FRAME  # noqa: E402

CALL_ID = 4242
INPUT_CALL = raw.types.InputGroupCall(id=CALL_ID, access_hash=1)


class RouterClient:
    """Minimal Kurigram stand-in that records handler registration."""

    def __init__(self) -> None:
        self.handlers: list[object] = []
        self.me = type("Me", (), {"id": 42, "is_bot": False, "username": "a"})()

    def add_handler(self, handler: object, group: int = 0) -> None:
        self.handlers.append(handler)

    def remove_handler(self, handler: object, group: int = 0) -> None:
        if handler in self.handlers:
            self.handlers.remove(handler)


def _discarded_update() -> object:
    return raw.types.UpdateGroupCall(
        call=raw.types.GroupCallDiscarded(id=CALL_ID, access_hash=1, duration=30)
    )


def _active_update(participants: int = 5) -> object:
    return raw.types.UpdateGroupCall(
        call=raw.types.GroupCall(
            id=CALL_ID,
            access_hash=1,
            participants_count=participants,
            title=None,
            version=1,
            unmuted_video_limit=0,
        )
    )


def _participants_update(*, left: bool = False, can_self_unmute: bool = True) -> object:
    return raw.types.UpdateGroupCallParticipants(
        call=INPUT_CALL,
        participants=[
            raw.types.GroupCallParticipant(
                peer=raw.types.PeerUser(user_id=42),
                date=0,
                source=999,
                muted=True,
                left=left or None,
                can_self_unmute=can_self_unmute or None,
            )
        ],
        version=2,
    )


# --------------------------------------------------------------------------- classify


@pytest.mark.parametrize(
    ("update_factory", "kind"),
    [
        (_discarded_update, "discarded"),
        (_active_update, "state"),
        (_participants_update, "participants"),
    ],
)
async def test_router_classifies_group_call_updates(update_factory, kind: str) -> None:
    router = GroupCallUpdateRouter(RouterClient())  # type: ignore[arg-type]
    event = router._classify(update_factory())  # noqa: SLF001
    assert event is not None
    assert event.kind == kind
    assert event.call_id == CALL_ID


async def test_router_classifies_connection_and_skips_presentation() -> None:
    router = GroupCallUpdateRouter(RouterClient())  # type: ignore[arg-type]
    main = raw.types.UpdateGroupCallConnection(
        params=raw.types.DataJSON(data='{"transport": {}}'), presentation=False
    )
    event = router._classify(main)  # noqa: SLF001
    assert event is not None and event.kind == "connection"
    assert event.extra["params"] == '{"transport": {}}'

    presentation = raw.types.UpdateGroupCallConnection(
        params=raw.types.DataJSON(data="{}"), presentation=True
    )
    # A presentation connection belongs to screen sharing, never to our audio.
    assert router._classify(presentation) is None  # noqa: SLF001


async def test_router_ignores_unrelated_updates() -> None:
    router = GroupCallUpdateRouter(RouterClient())  # type: ignore[arg-type]
    unrelated = raw.types.UpdateUserTyping(
        user_id=1, action=raw.types.SendMessageTypingAction()
    )
    assert router._classify(unrelated) is None  # noqa: SLF001


# --------------------------------------------------------------------------- dispatch


async def test_router_delivers_to_the_right_subscriber() -> None:
    client = RouterClient()
    router = GroupCallUpdateRouter(client)  # type: ignore[arg-type]
    mine: list[CallUpdateEvent] = []
    others: list[CallUpdateEvent] = []
    everything: list[CallUpdateEvent] = []

    async def for_me(event: CallUpdateEvent) -> None:
        mine.append(event)

    async def for_other(event: CallUpdateEvent) -> None:
        others.append(event)

    async def for_all(event: CallUpdateEvent) -> None:
        everything.append(event)

    router.subscribe(CALL_ID, for_me)
    router.subscribe(9999, for_other)
    router.subscribe(None, for_all)          # wildcard
    assert client.handlers, "no raw update handler was registered"

    await router._on_raw_update(client, _discarded_update(), None, None)  # noqa: SLF001
    assert len(mine) == 1 and mine[0].kind == "discarded"
    assert others == []
    assert len(everything) == 1

    router.unsubscribe(CALL_ID, for_me)
    await router._on_raw_update(client, _discarded_update(), None, None)  # noqa: SLF001
    assert len(mine) == 1, "unsubscribed handler still received events"
    assert len(everything) == 2

    router.unsubscribe(None, for_all)
    router.unsubscribe(9999, for_other)
    assert client.handlers == [], "handler was not removed once nobody was listening"


async def test_a_broken_handler_does_not_break_the_others(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = RouterClient()
    router = GroupCallUpdateRouter(client)  # type: ignore[arg-type]
    survived: list[str] = []

    async def explodes(_event: CallUpdateEvent) -> None:
        raise RuntimeError("boom")

    async def survives(_event: CallUpdateEvent) -> None:
        survived.append("ok")

    router.subscribe(CALL_ID, explodes)
    router.subscribe(CALL_ID, survives)
    with caplog.at_level(logging.ERROR):
        await router._on_raw_update(client, _discarded_update(), None, None)  # noqa: SLF001
    assert survived == ["ok"], "one bad handler must not stop the rest"
    assert "boom" in caplog.text


async def test_participant_update_carries_the_records() -> None:
    router = GroupCallUpdateRouter(RouterClient())  # type: ignore[arg-type]
    event = router._classify(_participants_update(left=True))  # noqa: SLF001
    assert event is not None
    participants = event.extra["participants"]
    assert len(participants) == 1
    assert participants[0].left is True
    assert repr(event).startswith("<CallUpdateEvent participants")


def test_router_is_one_per_client() -> None:
    first, second = RouterClient(), RouterClient()
    a = GroupCallUpdateRouter.for_client(first)  # type: ignore[arg-type]
    b = GroupCallUpdateRouter.for_client(first)  # type: ignore[arg-type]
    c = GroupCallUpdateRouter.for_client(second)  # type: ignore[arg-type]
    assert a is b, "the same client must share one router"
    assert a is not c, "different clients must not share state"


# --------------------------------------------------------------------------- volume fallback


@pytest.fixture
def no_numpy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the pure-Python gain path, which is what hosts without numpy run."""
    monkeypatch.setattr(volume_module, "_np", None)


def _samples(*values: int) -> bytes:
    return b"".join(v.to_bytes(2, "little", signed=True) for v in values)


def _decode(pcm: bytes) -> list[int]:
    return [
        int.from_bytes(pcm[i : i + 2], "little", signed=True) for i in range(0, len(pcm), 2)
    ]


@pytest.mark.parametrize("gain", [0.5, 1.5, 2.0, 0.01])
def test_both_gain_implementations_agree(gain: float, monkeypatch: pytest.MonkeyPatch) -> None:
    """numpy and pure-Python must produce identical bytes, or volume changes with the host."""
    pcm = _samples(0, 1, -1, 1000, -1000, 30000, -30000, 32767, -32768, 12345)
    with_numpy = apply_gain(pcm, gain)
    monkeypatch.setattr(volume_module, "_np", None)
    without_numpy = apply_gain(pcm, gain)
    assert without_numpy == with_numpy, f"gain paths disagree at {gain}"


@pytest.mark.usefixtures("no_numpy")
def test_fallback_saturates_and_scales() -> None:
    doubled = apply_gain(_samples(1000, -1000, 30000, -30000), 2.0)
    assert _decode(doubled) == [2000, -2000, 32767, -32768]


@pytest.mark.usefixtures("no_numpy")
def test_fallback_handles_identity_mute_and_odd_length() -> None:
    pcm = _samples(500, -500)
    assert apply_gain(pcm, 1.0) is pcm
    assert apply_gain(pcm, 0.0) == b"\x00" * len(pcm)
    odd = pcm + b"\x7f"
    assert len(apply_gain(odd, 2.0)) == len(odd)
    assert apply_gain(b"", 2.0) == b""


# --------------------------------------------------------------------------- buffer edges


async def test_buffer_async_read_times_out_when_empty() -> None:
    buffer = PcmRingBuffer(capacity_ms=200, prefetch_ms=40)
    assert await buffer.read(timeout=0.05) is None


async def test_buffer_async_read_returns_frames_and_eof() -> None:
    buffer = PcmRingBuffer(capacity_ms=200, prefetch_ms=40)
    frame = b"\x02" * BYTES_PER_FRAME
    await buffer.write(frame)
    await buffer.mark_eof()
    assert await buffer.read(timeout=1) == frame
    assert await buffer.read(timeout=1) is None      # EOF sentinel
    assert buffer.at_eof


async def test_buffer_wait_primed_timeout_then_success() -> None:
    buffer = PcmRingBuffer(capacity_ms=200, prefetch_ms=60)   # 3 frames
    assert await buffer.wait_primed(timeout=0.05) is False
    for _ in range(3):
        await buffer.write(b"\x01" * BYTES_PER_FRAME)
    assert await buffer.wait_primed(timeout=0.5) is True
    assert await buffer.wait_primed() is True                 # already primed, no wait


async def test_buffer_wait_drained_reports_timeout() -> None:
    buffer = PcmRingBuffer(capacity_ms=200, prefetch_ms=40)
    await buffer.write(b"\x01" * BYTES_PER_FRAME)
    assert await buffer.wait_drained(timeout=0.05) is False   # nobody is reading
    buffer.read_nowait()
    assert await buffer.wait_drained(timeout=0.5) is True


async def test_closed_buffer_refuses_writes_and_reads() -> None:
    buffer = PcmRingBuffer(capacity_ms=200, prefetch_ms=40)
    buffer.close()
    assert buffer.try_write(b"\x00" * BYTES_PER_FRAME) is False
    await buffer.write(b"\x00" * BYTES_PER_FRAME)             # silently ignored
    assert buffer.read_nowait() is None
    assert await buffer.read(timeout=0.01) is None


async def test_buffer_reset_eof_allows_reuse() -> None:
    """``at_eof`` means "EOF marked *and* fully drained", not merely "EOF marked"."""
    buffer = PcmRingBuffer(capacity_ms=200, prefetch_ms=40)
    frame = b"\x03" * BYTES_PER_FRAME
    await buffer.mark_eof()
    assert not buffer.at_eof, "the EOF sentinel is still queued, so not drained yet"
    assert await buffer.read(timeout=1) is None      # consume the sentinel
    assert buffer.at_eof

    buffer.reset_eof()
    assert not buffer.at_eof, "reset_eof() must make the buffer reusable"
    await buffer.write(frame)
    assert buffer.read_nowait() == frame


# --------------------------------------------------------------------------- logging


def test_enable_debug_is_idempotent() -> None:
    logger = enable_debug(level=logging.DEBUG)
    before = len(logger.handlers)
    enable_debug(level=logging.DEBUG)
    assert len(logger.handlers) == before, "enable_debug() added a duplicate handler"
    logger.setLevel(logging.WARNING)


def test_redact_masks_short_and_long_secrets() -> None:
    masked = redact({"pwd": "short", "auth_key": "x" * 40, "keep": "visible"})
    assert masked["pwd"] == "***"
    assert masked["auth_key"].endswith("(40 chars)")
    assert masked["keep"] == "visible"
    assert redact(["a", {"token": "y" * 12}])[1]["token"].endswith("(12 chars)")


def test_dump_signaling_survives_non_json_text(caplog: pytest.LogCaptureFixture) -> None:
    logger = get_logger("test.nonjson")
    logger.setLevel(logging.DEBUG)
    with caplog.at_level(logging.DEBUG, logger="aytgcalls.test.nonjson"):
        dump_signaling(logger, "raw", "this is not json")
    assert "this is not json" in caplog.text


async def test_event_repr_is_useful() -> None:
    event = CallUpdateEvent("state", 7, None, participants_count=3)
    assert "state" in repr(event) and "7" in repr(event)


def test_asyncio_is_importable_here() -> None:
    assert asyncio.get_event_loop_policy() is not None
