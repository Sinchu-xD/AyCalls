"""Multi-chat manager.

One Kurigram user session can be in several voice chats at once (Telegram allows it;
each call gets its own ICE/DTLS/RTP transport and its own FFmpeg pipeline, so the
practical ceiling is CPU and bandwidth, not the protocol).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from ..config import CallConfig
from ..logger import get_logger
from .group_call import GroupCall

if TYPE_CHECKING:  # pragma: no cover
    from pyrogram import Client

logger = get_logger("call.factory")

__all__ = ["GroupCallFactory"]


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
        return self._calls.get(chat_id)

    def __len__(self) -> int:
        return len(self._calls)

    def __contains__(self, chat_id: object) -> bool:
        return chat_id in self._calls

    # -- lifecycle ----------------------------------------------------------------------

    def create(self, chat_id: int | str, *, config: CallConfig | None = None) -> GroupCall:
        """Create (but do not join) a call object for ``chat_id``."""
        call = GroupCall(self.client, config=config or self.config)

        @call.on_disconnect
        async def _drop(closed: GroupCall, reason: Any) -> None:
            if self._calls.get(chat_id) is closed:
                self._calls.pop(chat_id, None)
                logger.debug(
                    "Removed %s from factory (%s)", chat_id, getattr(reason, "value", reason)
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
