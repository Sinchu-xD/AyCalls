"""Find the active group call for a chat.

``full_chat.call`` is the ``InputGroupCall`` (id + access_hash) that every ``phone.*``
method needs. It is ``None`` when no voice chat is running.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..exceptions import GroupCallNotFound, NotInGroup, TelegramCallError
from ..logger import get_logger
from .client import import_pyrogram

if TYPE_CHECKING:  # pragma: no cover
    from pyrogram import Client

logger = get_logger("telegram.discovery")

__all__ = ["DiscoveredCall", "discover_group_call"]


@dataclass
class DiscoveredCall:
    """An active voice chat plus useful state."""

    input_call: Any
    chat_id: int | str
    peer: Any
    participants_count: int = 0
    is_scheduled: bool = False
    is_rtmp: bool = False
    title: str | None = None

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return f"<GroupCall chat={self.chat_id} participants={self.participants_count}>"


async def discover_group_call(client: Client, chat_id: int | str) -> DiscoveredCall:
    """Resolve ``chat_id`` and return its active :class:`DiscoveredCall`.

    :raises NotInGroup: the session cannot resolve/access the chat.
    :raises GroupCallNotFound: the chat has no active voice chat.
    """
    pyrogram = import_pyrogram()
    raw, errors = pyrogram.raw, pyrogram.errors

    try:
        peer = await client.resolve_peer(chat_id)
    except errors.RPCError as exc:
        raise NotInGroup(
            f"Could not resolve chat {chat_id!r}. The assistant account must be a member "
            f"of the chat (and have seen it at least once). Telegram said: {exc}"
        ) from exc

    try:
        if isinstance(peer, raw.types.InputPeerChannel):
            full = await client.invoke(
                raw.functions.channels.GetFullChannel(
                    channel=raw.types.InputChannel(
                        channel_id=peer.channel_id, access_hash=peer.access_hash
                    )
                )
            )
        elif isinstance(peer, raw.types.InputPeerChat):
            full = await client.invoke(
                raw.functions.messages.GetFullChat(chat_id=peer.chat_id)
            )
        else:
            raise NotInGroup(
                f"{chat_id!r} resolves to {type(peer).__name__}; group voice chats only exist "
                "in groups, supergroups and channels."
            )
    except errors.RPCError as exc:
        raise TelegramCallError(
            f"Fetching full chat for {chat_id!r} failed", rpc_error=exc
        ) from exc

    input_call = getattr(full.full_chat, "call", None)
    if input_call is None:
        raise GroupCallNotFound(
            f"No active voice chat in {chat_id!r}. Start the voice chat first "
            "(aytgcalls joins existing calls, it does not create them)."
        )

    discovered = DiscoveredCall(input_call=input_call, chat_id=chat_id, peer=peer)

    # Enrich with live state; failure here is non-fatal.
    try:
        state = await client.invoke(
            raw.functions.phone.GetGroupCall(call=input_call, limit=1)
        )
        call = state.call
        discovered.participants_count = getattr(call, "participants_count", 0) or 0
        discovered.is_scheduled = getattr(call, "schedule_date", None) is not None
        discovered.is_rtmp = bool(getattr(call, "rtmp_stream", False))
        discovered.title = getattr(call, "title", None)
    except errors.RPCError as exc:  # pragma: no cover - network dependent
        logger.debug("phone.getGroupCall enrichment failed (non-fatal): %s", exc)

    if discovered.is_scheduled:
        raise GroupCallNotFound(
            f"The voice chat in {chat_id!r} is scheduled but has not started yet."
        )
    logger.info("Discovered %s", discovered)
    return discovered
