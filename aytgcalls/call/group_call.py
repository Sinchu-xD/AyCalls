"""The public :class:`GroupCall` API.

Ties the four layers together::

    discovery (Kurigram)  ->  signaling (phone.*)  ->  transport (aiortc)  ->  player (FFmpeg)

Everything is async, nothing blocks the Kurigram update loop, and every task/process is
cancelled and awaited on :meth:`GroupCall.leave`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from ..config import CallConfig
from ..exceptions import (
    AlreadyJoined,
    AytgcallsError,
    NotJoined,
    TelegramCallError,
    TransportError,
)
from ..logger import dump_signaling, get_logger
from ..media.telegram import TelegramDownloader, is_telegram_media
from ..media.volume import telegram_volume
from ..player.player import Player
from ..player.queue import TrackQueue
from ..telegram.client import ensure_user_session
from ..telegram.discovery import DiscoveredCall, discover_group_call
from ..telegram.signaling import GroupCallSignaling
from ..telegram.updates import CallUpdateEvent, GroupCallUpdateRouter
from ..transport.sdp import parse_join_response
from ..transport.track import PcmStreamTrack
from ..transport.webrtc import TelegramTransport
from ..types import (
    AudioSource,
    CallStats,
    DisconnectReason,
    LoopMode,
    PlaybackState,
    StreamEndReason,
    TrackInfo,
)
from .reconnect import BackoffPolicy, ReconnectManager

if TYPE_CHECKING:  # pragma: no cover
    from pyrogram import Client

logger = get_logger("call")

__all__ = ["GroupCall", "AyCall"]

StreamEndHandler = Callable[["GroupCall", AudioSource, StreamEndReason], Awaitable[None] | None]
DisconnectHandler = Callable[["GroupCall", DisconnectReason], Awaitable[None] | None]


class GroupCall:
    """Publish audio into one Telegram group voice chat.

    ``client`` must be a **user** session created by Kurigram; a bot session raises
    :class:`~aytgcalls.exceptions.BotClientNotAllowed` at join time.
    """

    def __init__(
        self,
        client: Client,
        chat_id: int | str | None = None,
        *,
        config: CallConfig | None = None,
        queue: TrackQueue | None = None,
    ) -> None:
        self.client = client
        self.config = config or CallConfig()
        self.stats = CallStats()

        self._signaling = GroupCallSignaling(client)
        self._track = PcmStreamTrack(stats=self.stats)
        self.player = Player(
            self._track,
            config=self.config,
            queue=queue,
            on_stream_end=self._handle_stream_end,
        )

        self._transport: TelegramTransport | None = None
        self._discovered: DiscoveredCall | None = None
        self._chat_id: int | str | None = chat_id
        self._downloader = TelegramDownloader(client, directory=self.config.download_dir)
        self._auto_leave_task: asyncio.Task[None] | None = None
        self._join_as: Any = None
        self._invite_hash: str | None = None
        self._joined = asyncio.Event()
        self._leaving = False
        self._lock = asyncio.Lock()

        self._keepalive_task: asyncio.Task[None] | None = None
        self._router: GroupCallUpdateRouter | None = None
        self._reconnect = ReconnectManager(
            self._reconnect_once,
            policy=BackoffPolicy(
                initial_delay=self.config.reconnect_initial_delay,
                max_delay=self.config.reconnect_max_delay,
                max_attempts=self.config.reconnect_max_attempts,
                jitter=self.config.reconnect_jitter,
            ),
            on_give_up=lambda _exc: self._fire_disconnect(DisconnectReason.TRANSPORT_FAILED),
        )

        self._stream_end_handlers: list[StreamEndHandler] = []
        self._disconnect_handlers: list[DisconnectHandler] = []

        # Auto-leave when the queue runs dry, cancelled the moment playback resumes.
        self.player.state.on_change(self._on_player_state)

    # -- introspection -----------------------------------------------------------------

    @property
    def queue(self) -> TrackQueue:
        return self.player.queue

    @property
    def is_connected(self) -> bool:
        return self._joined.is_set()

    @property
    def chat_id(self) -> int | str | None:
        return self._chat_id

    @property
    def ssrc(self) -> int | None:
        return self._transport.ssrc if self._transport else None

    @property
    def playback_state(self) -> PlaybackState:
        return self.player.playback_state

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return (
            f"<GroupCall chat={self._chat_id} connected={self.is_connected} "
            f"state={self.playback_state.value}>"
        )

    # -- events -------------------------------------------------------------------------

    def on_stream_end(self, handler: StreamEndHandler) -> StreamEndHandler:
        """Decorator: called with ``(call, source, reason)`` when a track finishes."""
        self._stream_end_handlers.append(handler)
        return handler

    def on_disconnect(self, handler: DisconnectHandler) -> DisconnectHandler:
        """Decorator: called with ``(call, reason)`` when the call ends."""
        self._disconnect_handlers.append(handler)
        return handler

    def _handle_stream_end(self, source: AudioSource, reason: StreamEndReason) -> None:
        # A looping track is about to play again, so keep its download around.
        repeating = self.queue.current is not None and self.queue.current.id == source.id
        if not repeating and self._downloader.owns(source):
            self._downloader.release(source)
        for handler in self._stream_end_handlers:
            try:
                result = handler(self, source, reason)
                if asyncio.iscoroutine(result):
                    asyncio.ensure_future(result)
            except Exception:
                logger.exception("on_stream_end handler failed")

    async def _fire_disconnect(self, reason: DisconnectReason) -> None:
        logger.info("Disconnected: %s", reason.value)
        for handler in self._disconnect_handlers:
            try:
                result = handler(self, reason)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                logger.exception("on_disconnect handler failed")

    # -- join / leave ----------------------------------------------------------------------

    async def join(
        self,
        chat_id: int | str,
        *,
        join_as: Any = None,
        invite_hash: str | None = None,
    ) -> None:
        """Join the active voice chat in ``chat_id``.

        :raises BotClientNotAllowed: the client is a bot session.
        :raises GroupCallNotFound: no voice chat is running in that chat.
        """
        async with self._lock:
            if self._joined.is_set():
                raise AlreadyJoined(f"Already joined the voice chat in {self._chat_id!r}")
            await ensure_user_session(self.client)
            self._chat_id = chat_id
            self._join_as = join_as
            self._invite_hash = invite_hash
            self._discovered = await discover_group_call(self.client, chat_id)
            await self._connect()
            self._start_keepalive()
            self._subscribe_updates()
            self._joined.set()
        logger.info("Joined voice chat in %s (ssrc=%s)", chat_id, self.ssrc)

    async def _connect(self) -> None:
        """Prepare the transport, run ``phone.joinGroupCall``, and bring up media."""
        assert self._discovered is not None
        transport = TelegramTransport(
            self._track,
            ice_servers=self.config.ice_servers,
            connect_timeout=self.config.connect_timeout,
            opus_bitrate=self.config.opus_bitrate,
            stats=self.stats,
        )
        # Publish the transport before connecting so `call.ssrc` is meaningful during the
        # handshake and so teardown can always find it.
        self._transport = transport
        try:
            payload = await transport.prepare()
            dump_signaling(logger, "join payload", payload.to_json())
            result = await self._signaling.join(
                self._discovered.input_call,
                params_json=payload.to_data_json(),
                join_as=self._join_as,
                muted=self.config.join_muted,
                video_stopped=True,
                invite_hash=self._invite_hash,
            )
            response = parse_join_response(result.params)
            if response.is_stream:
                raise TransportError(
                    "This voice chat is an RTMP/stream broadcast; audio cannot be published "
                    "over WebRTC. See PROTOCOL.md §7."
                )
            await transport.connect(response)
        except BaseException:
            self._transport = None
            await transport.close()
            raise

    async def leave(self, *, reason: DisconnectReason = DisconnectReason.REQUESTED) -> None:
        """Stop playback, tell Telegram we left, and release every resource."""
        if self._leaving:
            return
        self._leaving = True
        try:
            self._cancel_auto_leave()
            await self._reconnect.cancel()
            await self._stop_keepalive()
            self._unsubscribe_updates()
            with contextlib.suppress(Exception):
                await self.player.stop()
            await self.player.close()

            transport, self._transport = self._transport, None
            source = transport.ssrc if transport is not None else None
            if transport is not None:
                await transport.close()
            self._track.stop()

            if self._discovered is not None and source is not None and self._joined.is_set():
                try:
                    await self._signaling.leave(self._discovered.input_call, source)
                except TelegramCallError as exc:
                    logger.warning("phone.leaveGroupCall failed (ignoring): %s", exc)
            self._joined.clear()
            self._downloader.cleanup()
        finally:
            self._leaving = False
        await self._fire_disconnect(reason)

    # -- playback (delegates to the player) --------------------------------------------------

    def _require_joined(self) -> None:
        if not self._joined.is_set():
            raise NotJoined("Not joined to a voice chat; call await call.join(chat_id) first.")

    async def play(
        self,
        source: Any,
        *,
        chat_id: int | str | None = None,
        force: bool = False,
    ) -> tuple[AudioSource, bool]:
        """Play anything, handling everything else on its own.

        This is the only entry point you need:

        * **auto-joins** the voice chat if we are not in it yet;
        * **auto-queues** when something is already playing (no separate ``add``);
        * accepts a local path, an ``http(s)`` URL, **or a Telegram voice note / audio /
          document / video message**, which is downloaded through the same session;
        * the queue advances by itself, and the call **auto-leaves** when it runs dry.

        :param source: path, URL, Kurigram ``Message``, media object, or ``AudioSource``.
        :param chat_id: needed only if it was not given to the constructor.
        :param force: jump the queue and start this track immediately.
        :returns: ``(track, started_now)`` — ``started_now`` is ``False`` when it queued.
        """
        # A Telegram message/voice/audio has to be fetched before FFmpeg can see it.
        if is_telegram_media(source):
            source = await self._downloader.resolve(source)

        await self._ensure_joined(chat_id)
        self._cancel_auto_leave()

        if self.player.state.is_active and not force:
            track = await self.player.queue.add(source)
            logger.info("Queued %s (position %d)", track.display_name, len(self.queue))
            return track, False
        return await self.player.play(source), True

    async def add(
        self,
        source: Any,
        *,
        chat_id: int | str | None = None,
        force: bool = False,
    ) -> tuple[AudioSource, bool]:
        """Alias for :meth:`play` — kept so ``/add`` style commands keep working."""
        return await self.play(source, chat_id=chat_id, force=force)

    async def _ensure_joined(self, chat_id: int | str | None = None) -> None:
        """Join the voice chat if needed, using the chat id we were given."""
        if chat_id is not None and chat_id != self._chat_id and self._joined.is_set():
            raise AlreadyJoined(
                f"Already joined {self._chat_id!r}; use a separate AyCall (or AyFac) "
                f"for {chat_id!r}."
            )
        if self._joined.is_set():
            return
        target = chat_id if chat_id is not None else self._chat_id
        if target is None:
            raise NotJoined(
                "No chat id known. Either construct AyCall(client, chat_id) or call "
                "play(source, chat_id=...)."
            )
        if not self.config.auto_join:
            raise NotJoined("auto_join is disabled; call await join(chat_id) first.")
        logger.info("Auto-joining the voice chat in %s", target)
        await self.join(target)

    async def pause(self) -> None:
        self._require_joined()
        await self.player.pause()

    async def resume(self) -> None:
        self._require_joined()
        await self.player.resume()

    async def stop(self) -> None:
        """Stop everything: clear the queue, stop playback and leave the voice chat.

        Same as :meth:`end`. Use :meth:`stop_playback` to stop the audio but stay in the
        call.
        """
        await self.end()

    async def stop_playback(self, *, clear_queue: bool = True) -> None:
        """Stop the audio but stay in the voice chat."""
        self._require_joined()
        await self.player.stop(clear_queue=clear_queue)

    async def skip(self) -> AudioSource | None:
        self._require_joined()
        return await self.player.skip()

    async def previous(self) -> AudioSource | None:
        """Go back to the previously played track."""
        self._require_joined()
        return await self.player.previous()

    # -- seeking ---------------------------------------------------------------------------

    async def seek(self, position: float) -> float:
        """Jump to an absolute position, in seconds. Returns where we landed."""
        self._require_joined()
        return await self.player.seek(position)

    async def forward(self, seconds: float = 10.0) -> float:
        """Skip forward by ``seconds`` (default 10)."""
        self._require_joined()
        return await self.player.forward(seconds)

    async def rewind(self, seconds: float = 10.0) -> float:
        """Skip backward by ``seconds`` (default 10), clamped at the start."""
        self._require_joined()
        return await self.player.rewind(seconds)

    async def replay(self) -> float:
        """Restart the current track from the beginning."""
        self._require_joined()
        return await self.player.replay()

    # -- queue helpers ----------------------------------------------------------------------

    async def shuffle(self) -> None:
        """Shuffle the pending queue."""
        await self.queue.shuffle()

    async def clear_queue(self) -> int:
        """Drop every pending track; the current one keeps playing."""
        return await self.queue.clear()

    async def loop(self, value: Any = None) -> LoopMode:
        """One call for every repeat behaviour.

        ============================  ==========================================
        ``await call.loop(3)``        repeat the current track 3 more times
        ``await call.loop("track")``  repeat the current track forever
        ``await call.loop("queue")``  loop the whole queue
        ``await call.loop("shuffle")``shuffle now, then keep looping the queue
        ``await call.loop("off")``    stop looping
        ``await call.loop()``         just read the current mode
        ============================  ==========================================
        """
        if value is None:
            return self.queue.loop

        if isinstance(value, str) and value.strip().lower() in {"shuffle", "random", "mix"}:
            await self.queue.shuffle()
            self.queue.loop = LoopMode.QUEUE
            self.queue.auto_shuffle = True
            self.queue.loop_times = 0
            logger.info("Shuffled the queue and enabled queue loop")
            return self.queue.loop

        mode = LoopMode.from_any(value)
        self.queue.auto_shuffle = False
        if mode is LoopMode.TIMES:
            times = int(value) if not isinstance(value, LoopMode) else 1
            self.queue.loop_times = max(1, times)
            self.queue.loop = mode
            logger.info("Looping the current track %d more time(s)", self.queue.loop_times)
            return mode
        self.queue.loop_times = 0
        self.queue.loop = mode
        logger.info("Loop mode: %s", mode.value)
        return mode

    def __setattr__(self, name: str, value: Any) -> None:
        """Make ``call.loop = "queue"`` behave like ``call.set_loop("queue")``.

        ``loop`` is a method, so a plain assignment would silently replace it with a
        string and break every later call. Intercepting it keeps both styles working.
        """
        if name == "loop" and not callable(value):
            self.set_loop(value)
            return
        super().__setattr__(name, value)

    def set_loop(self, mode: Any) -> LoopMode:
        """Synchronous loop setter for simple modes (no shuffle)."""
        self.queue.loop = LoopMode.from_any(mode)
        if self.queue.loop is LoopMode.TIMES:
            self.queue.loop_times = max(1, int(mode))
        return self.queue.loop

    # -- playback state ---------------------------------------------------------------------

    @property
    def position(self) -> float:
        """Seconds of the current track already sent."""
        return self.player.position

    @property
    def duration(self) -> float | None:
        """Length of the current track, or ``None`` for live streams."""
        return self.player.duration

    @property
    def volume(self) -> float:
        return self.player.volume

    @property
    def is_playing(self) -> bool:
        return self.player.is_playing

    @property
    def is_paused(self) -> bool:
        return self.player.is_paused

    @property
    def now_playing(self) -> TrackInfo:
        """One object with title, state, position, duration, loop mode and queue length."""
        return self.player.now_playing

    async def set_volume(self, percent: float, *, server_side: bool = False) -> None:
        """Set volume in percent (100 = unity).

        Local PCM gain always applies. ``server_side=True`` additionally asks Telegram to
        scale this participant for everyone via ``phone.editGroupCallParticipant``.
        """
        await self.player.set_volume(percent)
        if server_side:
            self._require_joined()
            assert self._discovered is not None
            await self._signaling.edit_participant(
                self._discovered.input_call, volume=telegram_volume(percent)
            )

    async def mute(self, muted: bool = True) -> None:
        """Server-side mute/unmute of our own participant."""
        self._require_joined()
        assert self._discovered is not None
        await self._signaling.edit_participant(self._discovered.input_call, muted=muted)

    async def unmute(self) -> None:
        """Server-side unmute of our own participant.

        Note that an admin-imposed mute with ``can_self_unmute=False`` cannot be undone
        from here — only an admin can lift it.
        """
        await self.mute(False)

    async def end(self) -> None:
        """Stop playback, clear the queue and leave the voice chat.

        The one-call teardown: equivalent to ``await stop()`` followed by ``await leave()``,
        but safe to call even when nothing is playing.
        """
        if self._joined.is_set():
            with contextlib.suppress(AytgcallsError):
                await self.player.stop()
        await self.leave()

    @property
    def loop_mode(self) -> LoopMode:
        """The active :class:`LoopMode` (set it with :meth:`loop`)."""
        return self.queue.loop

    # -- auto-leave ---------------------------------------------------------------------------

    def _on_player_state(self, previous: PlaybackState, current: PlaybackState) -> None:
        """Arm or disarm auto-leave as playback starts and stops.

        The player only reaches ``IDLE`` when the queue is exhausted, which is exactly the
        moment a music bot should get out of the call.
        """
        if current is PlaybackState.PLAYING:
            self._cancel_auto_leave()
        elif current is PlaybackState.IDLE and self.config.auto_leave:
            self._schedule_auto_leave()

    def _schedule_auto_leave(self) -> None:
        self._cancel_auto_leave()
        if self._leaving or not self._joined.is_set():
            return
        self._auto_leave_task = asyncio.ensure_future(self._auto_leave_after_delay())

    def _cancel_auto_leave(self) -> None:
        task, self._auto_leave_task = self._auto_leave_task, None
        if task is not None and not task.done():
            task.cancel()

    async def _auto_leave_after_delay(self) -> None:
        """Leave once the queue has been empty for ``auto_leave_delay`` seconds."""
        try:
            await asyncio.sleep(self.config.auto_leave_delay)
            if self._leaving or not self._joined.is_set():
                return
            if self.player.state.is_active:
                return  # something started playing during the grace period
            if len(self.queue) or self.queue.current is not None:
                return  # a track was queued while we waited
            logger.info("Queue finished — leaving the voice chat automatically")
            await self.leave(reason=DisconnectReason.QUEUE_FINISHED)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Auto-leave failed")

    # -- keepalive ----------------------------------------------------------------------------

    def _start_keepalive(self) -> None:
        if self._keepalive_task is None or self._keepalive_task.done():
            self._keepalive_task = asyncio.ensure_future(self._keepalive_loop())

    async def _stop_keepalive(self) -> None:
        task, self._keepalive_task = self._keepalive_task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _keepalive_loop(self) -> None:
        """``phone.CheckGroupCall`` on a timer; an empty result means the SFU dropped us."""
        interval = self.config.keepalive_interval
        misses = 0
        try:
            while True:
                await asyncio.sleep(interval)
                if self._transport is None or self._discovered is None:
                    continue
                try:
                    alive = await self._signaling.check(
                        self._discovered.input_call, [self._transport.ssrc]
                    )
                except TelegramCallError as exc:
                    logger.warning("Keepalive failed: %s", exc)
                    misses += 1
                else:
                    misses = 0 if self._transport.ssrc in alive else misses + 1
                await self._transport.refresh_stats()
                if misses >= 2:
                    logger.error("SFU no longer knows our source; reconnecting")
                    await self._handle_drop(DisconnectReason.SFU_TIMEOUT)
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Keepalive loop crashed")
            raise

    # -- updates / reconnect -------------------------------------------------------------------

    def _subscribe_updates(self) -> None:
        if self._discovered is None:
            return
        self._router = GroupCallUpdateRouter.for_client(self.client)
        self._router.subscribe(getattr(self._discovered.input_call, "id", None), self._on_update)

    def _unsubscribe_updates(self) -> None:
        if self._router is not None and self._discovered is not None:
            self._router.unsubscribe(
                getattr(self._discovered.input_call, "id", None), self._on_update
            )
            self._router = None

    async def _on_update(self, event: CallUpdateEvent) -> None:
        if event.kind == "discarded":
            logger.info("Voice chat was ended by an admin")
            await self.leave(reason=DisconnectReason.CALL_ENDED)
        elif event.kind == "connection":
            with contextlib.suppress(Exception):
                extra = parse_join_response(event.extra["params"])
                if self._transport is not None and extra.transport.candidates:
                    logger.debug("Applying %d late ICE candidates", len(extra.transport.candidates))
                    await self._transport.add_remote_candidates(extra)
        elif event.kind == "participants":
            await self._check_self_participant(event)

    async def _check_self_participant(self, event: CallUpdateEvent) -> None:
        me_id = getattr(getattr(self.client, "me", None), "id", None)
        for participant in event.extra.get("participants", []):
            peer = getattr(participant, "peer", None)
            if getattr(peer, "user_id", None) != me_id:
                continue
            if getattr(participant, "left", False):
                logger.warning("We were removed from the voice chat")
                await self.leave(reason=DisconnectReason.KICKED)
                return
            if getattr(participant, "muted", False) and not getattr(
                participant, "can_self_unmute", True
            ):
                logger.warning(
                    "Server-side muted with can_self_unmute=False: an admin must unmute this "
                    "account, otherwise nobody will hear the audio."
                )

    async def _handle_drop(self, reason: DisconnectReason) -> None:
        """The media path died. Reconnect if configured, otherwise leave."""
        if self._leaving:
            return
        transport, self._transport = self._transport, None
        if transport is not None:
            await transport.close()
        self._joined.clear()
        if not self.config.auto_reconnect:
            await self.leave(reason=reason)
            return
        await self._fire_disconnect(reason)
        self._reconnect.start()

    async def _reconnect_once(self) -> None:
        """One reconnect attempt: rediscover (the call id may have changed) and rejoin."""
        if self._chat_id is None:
            raise AytgcallsError("Cannot reconnect: no chat id recorded")
        self._discovered = await discover_group_call(self.client, self._chat_id)
        await self._connect()
        self._joined.set()
        self._start_keepalive()
        logger.info("Rejoined voice chat in %s", self._chat_id)
        if self.player.playback_state in (PlaybackState.PLAYING, PlaybackState.PAUSED):
            logger.info("Playback continues on the new transport")

    # -- diagnostics ------------------------------------------------------------------------------

    async def get_stats(self) -> CallStats:
        """Refresh and return transport statistics (RTP packets sent, ICE/DTLS state)."""
        if self._transport is not None:
            await self._transport.refresh_stats()
        return self.stats

    def debug_snapshot(self) -> str:
        """A JSON blob for bug reports. Contains no secrets."""
        return json.dumps(
            {
                "chat_id": str(self._chat_id),
                "joined": self.is_connected,
                "ssrc": self.ssrc,
                "playback_state": self.playback_state.value,
                "current": self.player.current.display_name if self.player.current else None,
                "position": round(self.player.position, 2),
                "duration": self.player.duration,
                "queued": len(self.queue),
                "loop": self.queue.loop.value,
                "volume": self.player.volume,
                "buffered_ms": self.player.buffered_ms,
                "stats": self.stats.as_dict(),
            },
            indent=2,
        )

    # -- context manager ---------------------------------------------------------------------------

    async def __aenter__(self) -> GroupCall:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.leave()


#: Branded alias — this is the name the README uses.
AyCall = GroupCall
