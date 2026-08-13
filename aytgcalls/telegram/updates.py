"""Handlers for ``UpdateGroupCall*`` updates.

A single :class:`GroupCallUpdateRouter` is registered on the Kurigram client with a
raw-update handler and dispatches to whichever :class:`GroupCall` owns that call id.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from ..logger import get_logger
from .client import import_pyrogram

if TYPE_CHECKING:  # pragma: no cover
    from pyrogram import Client

logger = get_logger("telegram.updates")

__all__ = ["CallUpdateEvent", "GroupCallUpdateRouter"]

Handler = Callable[["CallUpdateEvent"], Awaitable[None]]


class CallUpdateEvent:
    """Normalised group-call update."""

    __slots__ = ("kind", "call_id", "raw", "extra")

    def __init__(self, kind: str, call_id: int | None, raw: Any, **extra: Any) -> None:
        #: One of ``connection``, ``discarded``, ``state``, ``participants``.
        self.kind = kind
        self.call_id = call_id
        self.raw = raw
        self.extra = extra

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"<CallUpdateEvent {self.kind} call_id={self.call_id} {self.extra}>"


class GroupCallUpdateRouter:
    """Registers one raw-update handler per client and fans out to subscribers.

    The router is intentionally per-client (not global) so multiple clients in one
    process do not share state.
    """

    _instances: dict[int, GroupCallUpdateRouter] = {}

    def __init__(self, client: Client) -> None:
        self._client = client
        self._subscribers: dict[int, list[Handler]] = {}
        self._wildcard: list[Handler] = []
        self._registered = False
        self._handler: Any = None

    @classmethod
    def for_client(cls, client: Client) -> GroupCallUpdateRouter:
        """Return the router bound to ``client``, creating it on first use."""
        key = id(client)
        router = cls._instances.get(key)
        if router is None:
            router = cls(client)
            cls._instances[key] = router
        return router

    # -- subscription -----------------------------------------------------------------

    def subscribe(self, call_id: int | None, handler: Handler) -> None:
        if call_id is None:
            self._wildcard.append(handler)
        else:
            self._subscribers.setdefault(call_id, []).append(handler)
        self._ensure_registered()

    def unsubscribe(self, call_id: int | None, handler: Handler) -> None:
        bucket = self._wildcard if call_id is None else self._subscribers.get(call_id, [])
        if handler in bucket:
            bucket.remove(handler)
        if call_id is not None and not self._subscribers.get(call_id):
            self._subscribers.pop(call_id, None)
        if not self._subscribers and not self._wildcard:
            self._unregister()

    # -- registration -----------------------------------------------------------------

    def _ensure_registered(self) -> None:
        if self._registered:
            return
        pyrogram = import_pyrogram()
        self._handler = pyrogram.handlers.RawUpdateHandler(self._on_raw_update)
        self._client.add_handler(self._handler, group=-1)
        self._registered = True
        logger.debug("Registered raw update handler")

    def _unregister(self) -> None:
        if not self._registered:
            return
        try:
            self._client.remove_handler(self._handler, group=-1)
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("Removing raw update handler failed: %s", exc)
        self._registered = False
        self._instances.pop(id(self._client), None)
        logger.debug("Removed raw update handler")

    # -- dispatch ---------------------------------------------------------------------

    async def _on_raw_update(self, client: Client, update: Any, users: Any, chats: Any) -> None:
        event = self._classify(update)
        if event is None:
            return
        handlers = list(self._wildcard)
        if event.call_id is not None:
            handlers += list(self._subscribers.get(event.call_id, ()))
        for handler in handlers:
            try:
                await handler(event)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Group call update handler raised for %r", event)

    def _classify(self, update: Any) -> CallUpdateEvent | None:
        raw = import_pyrogram().raw
        types = raw.types
        name = type(update).__name__

        if name == "UpdateGroupCallConnection":
            if getattr(update, "presentation", False):
                return None
            return CallUpdateEvent("connection", None, update, params=update.params.data)

        if name == "UpdateGroupCall":
            call = getattr(update, "call", None)
            call_id = getattr(call, "id", None)
            discarded_cls = getattr(types, "GroupCallDiscarded", None)
            if discarded_cls is not None and isinstance(call, discarded_cls):
                return CallUpdateEvent("discarded", call_id, update)
            return CallUpdateEvent(
                "state",
                call_id,
                update,
                participants_count=getattr(call, "participants_count", None),
            )

        if name == "UpdateGroupCallParticipants":
            call_id = getattr(getattr(update, "call", None), "id", None)
            return CallUpdateEvent(
                "participants",
                call_id,
                update,
                participants=list(getattr(update, "participants", []) or []),
            )
        return None
