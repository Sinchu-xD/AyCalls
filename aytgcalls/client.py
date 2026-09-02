"""The ``pytgcalls``-style unified client: ``client.play(chat_id, source)``, ``client.pause()`` etc.

:class:`AyClient` is the one class most bot authors need.  It owns a
:class:`~aytgcalls.call.factory.GroupCallFactory` internally and re-exposes every
per-chat control as ``await client.<action>(chat_id, ...)``.

Quick start::

    from aytgcalls import AyClient, AyCreds
    from aytgcalls.telegram import build_user_client

    client = AyClient(AyCreds.from_env())
    await client.start()

    # all controls take chat_id first
    await client.play(-1001234567890, "song.mp3")
    await client.pause(-1001234567890)
    await client.resume(-1001234567890)
    await client.seek(-1001234567890, 45)
    await client.skip(-1001234567890)
    await client.end(-1001234567890)

    await client.stop()
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any

from .call.factory import GroupCallFactory
from .call.group_call import GroupCall
from .config import CallConfig, TelegramCredentials
from .exceptions import NotJoined
from .logger import get_logger
from .telegram.client import build_user_client

if TYPE_CHECKING:
    from pyrogram import Client

__all__ = ["AyClient"]

logger = get_logger("client")


class AyClient:
    """One Kurigram user session, many voice chats — the ``client.*()`` API.

    :param client: an optional pre-built :class:`pyrogram.Client` (user session).
        When ``None``, one is built from :class:`~aytgcalls.TelegramCredentials`.
    :param credentials: user credentials (``API_ID`` / ``API_HASH`` / ``STRING_SESSION``).
        Ignored when ``client`` is given.
    :param config: shared :class:`~aytgcalls.CallConfig` for every call.
    """

    def __init__(
        self,
        client: "Client | None" = None,
        *,
        credentials: TelegramCredentials | None = None,
        config: CallConfig | None = None,
    ) -> None:
        self._client = client
        self._credentials = credentials or TelegramCredentials.from_env()
        self._config = config or CallConfig()
        self._factory: GroupCallFactory | None = None

    # -- lifecycle --------------------------------------------------------------------------

    async def start(self) -> None:
        """Start the Kurigram user session (no-op if a client was already passed)."""
        if self._client is None:
            self._client = build_user_client(self._credentials)
        if not getattr(self._client, "is_connected", False):
            await self._client.start()
        self._factory = GroupCallFactory(self._client, config=self._config)
        logger.info("AyClient started (session=%s)", getattr(getattr(self._client, "me", None), "id", "?"))

    async def stop(self, chat_id: int | str | None = None) -> bool | None:
        """Shutdown.

        - ``await client.stop()`` — leave every call and stop the Kurigram session.
        - ``await client.stop(chat_id)`` — stop playback and leave that chat.
        """
        if chat_id is None:
            if self._factory is not None:
                await self._factory.leave_all()
            if self._client is not None and getattr(self._client, "is_connected", False):
                with contextlib.suppress(Exception):
                    await self._client.stop()
            return None
        return await self.end_call(chat_id)

    #: Alias for :meth:`stop` — both share the same overloaded signature.
    end = stop

    async def __aenter__(self) -> AyClient:
        await self.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.stop()

    # -- internal helpers -------------------------------------------------------------------

    def _require_factory(self) -> GroupCallFactory:
        if self._factory is None:
            raise NotJoined("AyClient not started — call await client.start() first.")
        return self._factory

    def _call(self, chat_id: int | str) -> GroupCall:
        return self._require_factory()._require(chat_id)

    # -- join / leave -----------------------------------------------------------------------

    async def play(
        self,
        chat_id: int | str,
        source: Any,
        *,
        force: bool = False,
    ) -> tuple[Any, bool]:
        """Play (or queue) ``source`` in ``chat_id``, joining first if needed.

        This is the single entry point most bot authors use.  It auto-joins, handles
        Telegram media resolution, and returns immediately.

        :param chat_id: target group / channel.
        :param source: local path, URL, Telegram message, or :class:`~aytgcalls.AudioSource`.
        :param force: if ``True``, jump the queue and start now.
        :returns: ``(track, started_now)`` — ``started_now`` is ``False`` when queued.
        """
        return await self._require_factory().play(chat_id, source, force=force)

    #: Alias for :meth:`play` — kept for backward compat with ``/add`` commands.
    add = play

    async def join(self, chat_id: int | str, **kwargs: Any) -> GroupCall:
        """Join ``chat_id`` without playing anything."""
        return await self._require_factory().get_or_create(chat_id, **kwargs)

    async def leave(self, chat_id: int | str) -> bool:
        """Leave the voice chat in ``chat_id``."""
        return await self._require_factory().leave(chat_id)

    async def end_call(self, chat_id: int | str) -> bool:
        """Stop playback, clear the queue and fully tear down ``chat_id``."""
        return await self._require_factory().end(chat_id)

    async def stop_playback(self, chat_id: int | str) -> None:
        """Stop audio in ``chat_id`` but stay in the call."""
        await self._require_factory().stop_playback(chat_id)

    # -- playback controls ------------------------------------------------------------------

    async def pause(self, chat_id: int | str) -> None:
        """Pause playback in ``chat_id`` (audio stays alive via silence)."""
        await self._require_factory().pause(chat_id)

    async def resume(self, chat_id: int | str) -> None:
        """Resume paused playback in ``chat_id``."""
        await self._require_factory().resume(chat_id)

    async def skip(self, chat_id: int | str) -> Any:
        """Skip the current track, advancing the queue."""
        return await self._require_factory().skip(chat_id)

    async def previous(self, chat_id: int | str) -> Any:
        """Go back to the previously played track."""
        return await self._require_factory().previous(chat_id)

    # -- seeking -----------------------------------------------------------------------------

    async def seek(self, chat_id: int | str, position: float) -> float:
        """Jump to ``position`` seconds. Returns the position we landed at."""
        return await self._require_factory().seek(chat_id, position)

    async def forward(self, chat_id: int | str, seconds: float = 10.0) -> float:
        """Skip forward by ``seconds`` (default 10). Returns new position."""
        return await self._require_factory().forward(chat_id, seconds)

    async def rewind(self, chat_id: int | str, seconds: float = 10.0) -> float:
        """Skip backward by ``seconds`` (default 10). Returns new position."""
        return await self._require_factory().rewind(chat_id, seconds)

    async def replay(self, chat_id: int | str) -> float:
        """Restart the current track from the beginning."""
        return await self._require_factory().replay(chat_id)

    # -- queue -------------------------------------------------------------------------------

    async def remove(self, chat_id: int | str, index: int) -> Any:
        """Remove the track at ``index`` from the pending queue. Returns it or ``None``."""
        return await self._require_factory().remove(chat_id, index)

    async def move(self, chat_id: int | str, source_index: int, target_index: int) -> bool:
        """Reorder the pending queue by moving a track. Returns ``True`` on success."""
        return await self._require_factory().move(chat_id, source_index, target_index)

    async def shuffle(self, chat_id: int | str) -> None:
        """Shuffle the pending queue in ``chat_id``."""
        await self._require_factory().shuffle(chat_id)

    async def clear_queue(self, chat_id: int | str) -> int:
        """Drop all pending tracks. Returns how many were removed."""
        return await self._require_factory().clear_queue(chat_id)

    async def loop(self, chat_id: int | str, value: Any = None) -> Any:
        """Get or set the loop mode.

        :param value: ``"off"``, ``"track"``, ``"queue"``, an ``int`` (times), or ``None``
            to just read the current mode.
        :returns: the active :class:`~aytgcalls.LoopMode`.
        """
        return await self._require_factory().loop(chat_id, value)

    # -- volume / mute -----------------------------------------------------------------------

    async def volume(self, chat_id: int | str, percent: float) -> None:
        """Set playback volume to ``percent`` (100 = unity)."""
        await self._require_factory().set_volume(chat_id, percent)

    async def mute(self, chat_id: int | str) -> None:
        """Server-side mute our participant in ``chat_id``."""
        await self._require_factory().mute(chat_id)

    async def unmute(self, chat_id: int | str) -> None:
        """Server-side unmute our participant in ``chat_id``."""
        await self._require_factory().unmute(chat_id)

    # -- video -------------------------------------------------------------------------------

    async def play_video(self, chat_id: int | str, source: Any) -> None:
        """Start streaming ``source`` as video in ``chat_id``."""
        await self._require_factory().play_video(chat_id, source)

    async def stop_video(self, chat_id: int | str) -> None:
        """Stop video streaming (audio continues)."""
        await self._require_factory().stop_video(chat_id)

    # -- introspection -----------------------------------------------------------------------

    def get_call(self, chat_id: int | str) -> GroupCall | None:
        """The :class:`~aytgcalls.GroupCall` for ``chat_id``, or ``None``."""
        factory = self._factory
        return factory.get(chat_id) if factory is not None else None

    def now_playing(self, chat_id: int | str) -> Any:
        """Current track info for ``chat_id``, or ``None``."""
        factory = self._require_factory()
        return factory.now_playing(chat_id)

    def position(self, chat_id: int | str) -> float:
        """Playback position in seconds for ``chat_id`` (0 if not joined)."""
        return self._require_factory().position(chat_id)

    def duration(self, chat_id: int | str) -> float | None:
        """Track duration in seconds for ``chat_id``, or ``None``."""
        return self._require_factory().duration(chat_id)

    def get_volume(self, chat_id: int | str) -> float:
        """Current volume (0–200) for ``chat_id`` (100 if not joined)."""
        return self._require_factory().volume(chat_id)

    def playback_state(self, chat_id: int | str) -> Any:
        """Current :class:`~aytgcalls.PlaybackState` for ``chat_id``."""
        return self._require_factory().playback_state(chat_id)

    async def get_stats(self, chat_id: int | str) -> Any:
        """Live :class:`~aytgcalls.CallStats` for ``chat_id``, or ``None``."""
        return await self._require_factory().get_stats(chat_id)

    async def get_participants(self, chat_id: int | str, *, limit: int = 100) -> list[dict[str, Any]]:
        """List participants currently in the voice chat."""
        return await self._require_factory().get_participants(chat_id, limit=limit)

    async def set_title(self, chat_id: int | str, title: str) -> None:
        """Rename the voice chat (admin only)."""
        await self._require_factory().set_title(chat_id, title)

    def is_connected(self, chat_id: int | str) -> bool:
        """Whether we are currently in the voice chat for ``chat_id``."""
        call = self.get_call(chat_id)
        return call.is_connected if call is not None else False

    @property
    def active_calls(self) -> dict[int | str, GroupCall]:
        """All currently joined calls."""
        return self._factory.calls if self._factory is not None else {}

    def __len__(self) -> int:
        return len(self.active_calls)

    def __repr__(self) -> str:
        n = len(self.active_calls)
        return f"<AyClient calls={n}>"
