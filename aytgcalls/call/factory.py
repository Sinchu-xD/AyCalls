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
import contextlib
from typing import TYPE_CHECKING, Any

from ..config import CallConfig
from ..exceptions import NotJoined
from ..logger import get_logger
from ..types import AudioSource, CallStats, LoopMode, PlaybackState, TrackInfo
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

    async def set_volume(self, chat_id: int | str, percent: float) -> None:
        """Set playback volume for ``chat_id``."""
        call = self._calls.get(chat_id)
        if call is not None:
            await call.set_volume(percent)

    async def mute(self, chat_id: int | str) -> None:
        await self._require(chat_id).mute()

    async def unmute(self, chat_id: int | str) -> None:
        await self._require(chat_id).unmute()

    def now_playing(self, chat_id: int | str) -> TrackInfo | None:
        """Current :class:`TrackInfo` for ``chat_id``, or ``None`` if not in that chat."""
        call = self._calls.get(chat_id)
        return call.now_playing if call is not None else None

    def position(self, chat_id: int | str) -> float:
        """Playback position in seconds for ``chat_id`` (0 if not joined)."""
        call = self._calls.get(chat_id)
        return call.position if call is not None and call.is_connected else 0.0

    def duration(self, chat_id: int | str) -> float | None:
        """Track duration in seconds for ``chat_id``, or ``None``."""
        call = self._calls.get(chat_id)
        return call.duration if call is not None and call.is_connected else None

    def volume(self, chat_id: int | str) -> float:
        """Current volume (0–200) for ``chat_id`` (100 if not joined)."""
        call = self._calls.get(chat_id)
        return call.volume if call is not None and call.is_connected else 100.0

    def playback_state(self, chat_id: int | str) -> PlaybackState:
        """Current :class:`~aytgcalls.PlaybackState` for ``chat_id``."""
        call = self._calls.get(chat_id)
        return call.playback_state if call is not None and call.is_connected else PlaybackState.IDLE

    async def get_stats(self, chat_id: int | str) -> CallStats | None:
        """Live :class:`~aytgcalls.CallStats` for ``chat_id``, or ``None``."""
        call = self._calls.get(chat_id)
        return await call.get_stats() if call is not None and call.is_connected else None

    async def get_participants(self, chat_id: int | str, *, limit: int = 100) -> list[dict[str, Any]]:
        """Participants currently in the voice chat for ``chat_id``."""
        return await self._require(chat_id).get_participants(limit=limit)

    async def set_title(self, chat_id: int | str, title: str) -> None:
        """Rename the voice chat for ``chat_id``."""
        await self._require(chat_id).set_title(title)

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

    async def play_video(self, chat_id: int | str, source: Any) -> None:
        """Start streaming video in ``chat_id``."""
        await self._require(chat_id).play_video(source)

    async def stop_video(self, chat_id: int | str) -> None:
        """Stop video in ``chat_id`` (audio continues)."""
        await self._require(chat_id).stop_video()

    async def stop_playback(self, chat_id: int | str) -> None:
        """Stop audio in ``chat_id`` but stay in the call."""
        await self._require(chat_id).stop_playback()

    async def remove(self, chat_id: int | str, index: int) -> AudioSource | None:
        """Remove track at ``index`` from ``chat_id``'s queue."""
        call = self._calls.get(chat_id)
        return await call.remove(index) if call is not None else None

    async def move(self, chat_id: int | str, source_index: int, target_index: int) -> bool:
        """Reorder the queue for ``chat_id``."""
        call = self._calls.get(chat_id)
        return await call.move(source_index, target_index) if call is not None else False

    async def shuffle(self, chat_id: int | str) -> None:
        """Shuffle the pending queue in ``chat_id``."""
        await self._require(chat_id).shuffle()

    async def clear_queue(self, chat_id: int | str) -> int:
        """Drop all pending tracks for ``chat_id``. Returns how many were removed."""
        call = self._calls.get(chat_id)
        return await call.clear_queue() if call is not None else 0

    async def end(self, chat_id: int | str) -> bool:
        """Stop playback, clear queue and fully tear down ``chat_id``."""
        call = self._calls.pop(chat_id, None)
        if call is None:
            return False
        with contextlib.suppress(Exception):
            await call.end()
        return True

    async def __aenter__(self) -> GroupCallFactory:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.leave_all()


#: Branded alias — this is the name the README uses.
AyFac = GroupCallFactory
