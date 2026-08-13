"""Multi-chat manager.

One Kurigram user session can be in several voice chats at once (Telegram allows it; each
call gets its own ICE/DTLS/RTP transport and its own FFmpeg pipeline, so the practical
ceiling is CPU and bandwidth, not the protocol).

:class:`AyFac` is the one-liner front end for a bot: ``await fac.play(chat_id, source)``
creates the call, joins, queues or starts playback, and lets it auto-leave when the queue
runs out. Every per-call control is mirrored here with a ``chat_id`` first argument.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from ..config import CallConfig
from ..exceptions import NotJoined
from ..logger import get_logger
from ..types import AudioSource, LoopMode, TrackInfo
from .group_call import GroupCall

if TYPE_CHECKING:  # pragma: no cover
    from pyrogram import Client

logger = get_logger("call.factory")

__all__ = ["GroupCallFactory", "AyFac"]


class GroupCallFactory:
    """Creates and tracks one :class:`GroupCall` per chat."""

    def __init__(self, client: Client, *, config: CallConfig | None = None) -> None:
        self.client = client
        self.config = config or CallConfig()
        self._calls: dict[int | str, GroupCall] = {}
        self._lock = asyncio.Lock()

    # -- accessors ---------------------------------------------------------------------

    @property
    def calls(self) -> dict[int | str, GroupCall]:
        return dict(self._calls)

    def get(self, chat_id: int | str) -> GroupCall | None:
        """The call for ``chat_id``, or ``None`` if we are not in that chat."""
        return self._calls.get(chat_id)

    def __len__(self) -> int:
        return len(self._calls)

    def __contains__(self, chat_id: object) -> bool:
        return chat_id in self._calls

    def __getitem__(self, chat_id: int | str) -> GroupCall:
        call = self._calls.get(chat_id)
        if call is None:
            raise KeyError(f"Not in a voice chat in {chat_id!r}")
        return call

    def _require(self, chat_id: int | str) -> GroupCall:
        call = self._calls.get(chat_id)
        if call is None or not call.is_connected:
            raise NotJoined(f"Not in a voice chat in {chat_id!r}")
        return call

    # -- lifecycle ----------------------------------------------------------------------

    def create(self, chat_id: int | str, *, config: CallConfig | None = None) -> GroupCall:
        """Create (but do not join) a call object for ``chat_id``."""
        call = GroupCall(self.client, chat_id, config=config or self.config)

        @call.on_disconnect
        async def _drop(closed: GroupCall, reason: Any) -> None:
            if self._calls.get(chat_id) is closed:
                self._calls.pop(chat_id, None)
                logger.debug(
                    "Removed %s from the factory (%s)",
                    chat_id,
                    getattr(reason, "value", reason),
                )

        self._calls[chat_id] = call
        return call

    async def get_or_create(self, chat_id: int | str, **join_kwargs: Any) -> GroupCall:
        """Return the joined call for ``chat_id``, joining it if necessary."""
        async with self._lock:
            call = self._calls.get(chat_id)
            if call is not None and call.is_connected:
                return call
            if call is None:
                call = self.create(chat_id)
        if not call.is_connected:
            await call.join(chat_id, **join_kwargs)
        return call

    # -- the one-liner ------------------------------------------------------------------

    async def play(
        self, chat_id: int | str, source: Any, *, force: bool = False
    ) -> tuple[AudioSource, bool]:
        """Play (or queue) ``source`` in ``chat_id`` — joining first if needed.

        Accepts a path, an ``http(s)`` URL, or a Telegram voice/audio message.

        :returns: ``(track, started_now)`` — ``started_now`` is ``False`` when it queued.
        """
        async with self._lock:
            call = self._calls.get(chat_id) or self.create(chat_id)
        return await call.play(source, chat_id=chat_id, force=force)

    # -- per-chat controls, mirrored so a bot never touches the call object -------------

    async def pause(self, chat_id: int | str) -> None:
        await self._require(chat_id).pause()

    async def resume(self, chat_id: int | str) -> None:
        await self._require(chat_id).resume()

    async def skip(self, chat_id: int | str) -> AudioSource | None:
        return await self._require(chat_id).skip()

    async def previous(self, chat_id: int | str) -> AudioSource | None:
        return await self._require(chat_id).previous()

    async def seek(self, chat_id: int | str, position: float) -> float:
        return await self._require(chat_id).seek(position)

    async def forward(self, chat_id: int | str, seconds: float = 10.0) -> float:
        return await self._require(chat_id).forward(seconds)

    async def rewind(self, chat_id: int | str, seconds: float = 10.0) -> float:
        return await self._require(chat_id).rewind(seconds)

    async def replay(self, chat_id: int | str) -> float:
        return await self._require(chat_id).replay()

    async def loop(self, chat_id: int | str, value: Any = None) -> LoopMode:
        return await self._require(chat_id).loop(value)

    async def volume(self, chat_id: int | str, percent: float) -> None:
        await self._require(chat_id).set_volume(percent)

    async def mute(self, chat_id: int | str) -> None:
        await self._require(chat_id).mute()

    async def unmute(self, chat_id: int | str) -> None:
        await self._require(chat_id).unmute()

    def now_playing(self, chat_id: int | str) -> TrackInfo | None:
        """Current :class:`TrackInfo` for ``chat_id``, or ``None`` if not in that chat."""
        call = self._calls.get(chat_id)
        return call.now_playing if call is not None else None

    async def stop(self, chat_id: int | str) -> bool:
        """Stop playback and leave that chat. ``False`` if we were not there."""
        return await self.leave(chat_id)

    async def leave(self, chat_id: int | str) -> bool:
        """Leave one call. Returns ``False`` if there was nothing to leave."""
        call = self._calls.pop(chat_id, None)
        if call is None:
            return False
        await call.leave()
        return True

    async def leave_all(self) -> None:
        """Leave every call concurrently; used on shutdown."""
        calls = list(self._calls.values())
        self._calls.clear()
        if calls:
            await asyncio.gather(*(call.leave() for call in calls), return_exceptions=True)
        logger.info("Left %d call(s)", len(calls))

    async def __aenter__(self) -> GroupCallFactory:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.leave_all()


#: Branded alias — this is the name the README uses.
AyFac = GroupCallFactory
