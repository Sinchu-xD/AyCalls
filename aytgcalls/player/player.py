"""The audio player: FFmpeg -> ring buffer -> gain -> track -> Opus/RTP.

Task layout (one asyncio task each, all cancelled and awaited on stop):

* **reader** — owns the FFmpeg subprocess, writes 20 ms PCM frames into the ring buffer,
  and swaps to the next queue item the moment the current one hits EOF (which is what
  makes transitions near-gapless: the frame stream feeding the sender never stops).
* **pacer/encoder/sender** — provided by :class:`~aytgcalls.transport.track.PcmStreamTrack`
  (20 ms wall clock) and aiortc's ``RTCRtpSender`` (Opus + RTP + SRTP). The player never
  touches the event loop's timing itself.
* **metadata probes** — one short-lived task per source, for duration/seekability.

Because the reader runs ahead of the sender by up to ``buffer_ms``, "the track being
decoded" and "the track you can hear" are not always the same. The player tracks both: a
FIFO of sources being decoded, and frame-exact end markers that tell us when playback
actually crosses from one source to the next. That is what makes :attr:`position` and
:meth:`seek` correct rather than approximate.

``play()`` returns as soon as the source is validated and the reader task is running; it
never blocks the Kurigram update loop.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import replace as dataclass_replace
from typing import Any

from ..config import CallConfig
from ..exceptions import AytgcallsError, FFmpegError, MediaSourceError, NotPlaying
from ..logger import get_logger
from ..media.buffer import PcmRingBuffer
from ..media.ffmpeg import FFmpegProcess
from ..media.metadata import MediaInfo, probe_media_info
from ..media.source import validate_source
from ..media.video_ffmpeg import FFmpegVideoProcess
from ..media.volume import apply_gain, percent_to_gain
from ..media.youtube import is_ytdlp_url, resolve_url
from ..transport.track import PcmStreamTrack
from ..transport.video import H264StreamTrack
from ..types import (
    BYTES_PER_FRAME,
    FRAME_MS,
    AudioSource,
    PlaybackState,
    SourceKind,
    StreamEndReason,
    TrackInfo,
)
from .queue import TrackQueue
from .state import StateMachine

logger = get_logger("player")

__all__ = ["Player", "AyPlayer"]

StreamEndCallback = Callable[[AudioSource, StreamEndReason], Awaitable[None] | None]

_FRAME_SECONDS = FRAME_MS / 1000.0


class Player:
    """Drives audio from a queue of sources into a :class:`PcmStreamTrack`.

    Optionally manages a parallel video track: pass ``video_track`` and call
    :meth:`play_video` to start streaming H.264 alongside the audio.
    """

    def __init__(
        self,
        track: PcmStreamTrack,
        *,
        config: CallConfig | None = None,
        queue: TrackQueue | None = None,
        on_stream_end: StreamEndCallback | None = None,
        video_track: H264StreamTrack | None = None,
    ) -> None:
        self.config = config or CallConfig()
        self.track = track
        self.queue = queue or TrackQueue()
        self.state = StateMachine()
        self.on_stream_end = on_stream_end
        self._video_track = video_track

        self._buffer = PcmRingBuffer(
            capacity_ms=self.config.buffer_ms,
            prefetch_ms=self.config.prefetch_ms,
        )
        self._gain = percent_to_gain(self.config.volume)
        self._volume = float(self.config.volume)
        self._reader_task: asyncio.Task[None] | None = None
        self._process: FFmpegProcess | None = None
        self._skip_event = asyncio.Event()
        self._resume_event = asyncio.Event()
        self._resume_event.set()
        self._lock = asyncio.Lock()

        #: ``buffer.frames_read`` value at which the source at the head finishes.
        self._end_markers: list[tuple[int, AudioSource]] = []
        #: Sources handed to FFmpeg, oldest first. The head is what you can hear.
        self._decoding: collections.deque[AudioSource] = collections.deque()
        self._frames_played = 0
        self._position_offset = 0.0
        self._info: dict[str, MediaInfo] = {}
        self._info_tasks: dict[str, asyncio.Task[MediaInfo]] = {}
        self._closed = False

        # Video pipeline state
        self._video_process: FFmpegVideoProcess | None = None
        self._video_reader_task: asyncio.Task[None] | None = None
        self._video_source: AudioSource | None = None

        self.track.set_provider(self._provide_frame)

    # -- introspection -------------------------------------------------------------------

    @property
    def playback_state(self) -> PlaybackState:
        return self.state.state

    @property
    def current(self) -> AudioSource | None:
        """The track you can hear right now (not the one being decoded ahead)."""
        return self._decoding[0] if self._decoding else self.queue.current

    @property
    def volume(self) -> float:
        return self._volume

    @property
    def buffered_ms(self) -> int:
        return self._buffer.buffered_ms

    @property
    def is_playing(self) -> bool:
        return self.state.state is PlaybackState.PLAYING

    @property
    def is_paused(self) -> bool:
        return self.state.state is PlaybackState.PAUSED

    @property
    def position(self) -> float:
        """Seconds of the current track that have actually been sent."""
        return self._position_offset + self._frames_played * _FRAME_SECONDS

    @property
    def duration(self) -> float | None:
        """Length of the current track in seconds, or ``None`` if unknown/live."""
        return self.media_info.duration

    @property
    def media_info(self) -> MediaInfo:
        source = self.current
        if source is None:
            return MediaInfo()
        return self._info.get(source.id, MediaInfo())

    @property
    def now_playing(self) -> TrackInfo:
        """Everything a ``/now`` command needs, in one object."""
        info = self.media_info
        return TrackInfo(
            source=self.current,
            state=self.state.state,
            position=self.position,
            duration=info.duration,
            volume=self._volume,
            loop=self.queue.loop,
            queued=len(self.queue),
            is_live=info.is_live,
        )

    # -- frame provider (called by the track every 20 ms) ---------------------------------

    async def _provide_frame(self) -> bytes | None:
        if self._closed or self.state.state is not PlaybackState.PLAYING:
            return None
        if not self._buffer.is_primed:
            return None
        frame = self._buffer.read_nowait()
        if frame is None:
            return None
        self._frames_played += 1
        self._check_end_markers()
        if self._gain != 1.0:
            frame = apply_gain(frame, self._gain)
        return frame

    def _check_end_markers(self) -> None:
        """Fire ``on_stream_end`` exactly when the last frame of a source has been sent."""
        while self._end_markers and self._buffer.frames_read >= self._end_markers[0][0]:
            _, source = self._end_markers.pop(0)
            if self._decoding and self._decoding[0].id == source.id:
                self._decoding.popleft()
            self._info.pop(source.id, None)
            # Playback has crossed into whatever is next.
            self._frames_played = 0
            self._position_offset = self._decoding[0].start_at if self._decoding else 0.0
            self._emit_stream_end(source, StreamEndReason.COMPLETED)

    def _emit_stream_end(self, source: AudioSource, reason: StreamEndReason) -> None:
        if self.on_stream_end is None:
            return
        result = self.on_stream_end(source, reason)
        if asyncio.iscoroutine(result):
            task = asyncio.ensure_future(result)
            task.add_done_callback(_log_task_exception)

    # -- commands ---------------------------------------------------------------------------

    async def play(self, source: Any | None = None, *, replace: bool = True) -> AudioSource:
        """Start (or replace) playback.

        With ``replace=False`` the source is appended to the queue instead of interrupting
        whatever is playing.
        """
        async with self._lock:
            if self._closed:
                raise AytgcallsError("Player is closed")
            if source is not None:
                track = validate_source(source)
                if (
                    track.kind is SourceKind.URL
                    and is_ytdlp_url(track.uri)
                ):
                    resolved = await resolve_url(track.uri)
                    if resolved is not None:
                        track = resolved
                        logger.info("Resolved via yt-dlp: %s", track.display_name)
                if not replace and self.state.is_active:
                    await self.queue.add(track)
                    logger.info("Queued %s", track.display_name)
                    return track
                await self.queue.set_current(track)
            elif self.queue.current is None and not await self.queue.next():
                raise NotPlaying("Nothing to play: the queue is empty")

            current = self.queue.current
            assert current is not None

            # When replacing an active source, stop the old reader and drain the
            # buffer so stale audio from the previous track never leaks through.
            if self.state.is_active and source is not None:
                await self._stop_reader()
                self._reset_stream_state()

            await self._restart_from(current)
            logger.info("Playing %s", current.display_name)
            return current

    async def pause(self) -> None:
        """Pause: the track keeps emitting silence so the SFU keeps the source alive."""
        async with self._lock:
            self.state.transition(PlaybackState.PAUSED)
            self._resume_event.clear()
        logger.info("Paused at %.1fs", self.position)

    async def resume(self) -> None:
        async with self._lock:
            self.state.transition(PlaybackState.PLAYING)
            self._resume_event.set()
        logger.info("Resumed at %.1fs", self.position)

    async def skip(self) -> AudioSource | None:
        """Stop the current track and advance the queue. Returns the new track, if any."""
        async with self._lock:
            if not self.state.is_active:
                raise NotPlaying("Nothing is playing")
            finished = self.current
            self._skip_event.set()
            await self._stop_reader()
            self._reset_stream_state()
            if finished is not None:
                self._emit_stream_end(finished, StreamEndReason.SKIPPED)
            following = await self.queue.next()
            if following is None:
                self.state.transition(PlaybackState.IDLE)
                logger.info("Queue exhausted after skip")
                return None
            await self._restart_from(following)
            logger.info("Skipped to %s", following.display_name)
            return following

    async def previous(self) -> AudioSource | None:
        """Go back to the previously played track."""
        async with self._lock:
            earlier = await self.queue.previous()
            if earlier is None:
                raise NotPlaying("No previous track in the history")
            await self._stop_reader()
            self._reset_stream_state()
            await self._restart_from(earlier)
            logger.info("Went back to %s", earlier.display_name)
            return earlier

    async def stop(self, *, clear_queue: bool = True) -> None:
        """Stop playback, kill FFmpeg, drop buffered audio and video."""
        async with self._lock:
            finished = self.current
            await self._stop_reader()
            await self._stop_video()
            self._reset_stream_state()
            if clear_queue:
                await self.queue.reset()
            else:
                await self.queue.set_current(None)
            if self.state.state is not PlaybackState.STOPPED:
                self.state.transition(PlaybackState.STOPPED)
            if finished is not None:
                self._emit_stream_end(finished, StreamEndReason.STOPPED)
        logger.info("Stopped")

    async def set_volume(self, percent: float) -> None:
        """Set local PCM gain, 0..200 %. Takes effect on the next frame."""
        self._gain = percent_to_gain(percent)
        self._volume = float(percent)
        logger.info("Volume set to %.0f%%", percent)

    # -- seeking -----------------------------------------------------------------------------

    async def seek(self, position: float) -> float:
        """Jump to ``position`` seconds in the current track.

        Local files seek with FFmpeg's ``-ss``. URLs that advertise ``Accept-Ranges`` seek
        by byte offset (estimated from duration and length — exact for CBR, within a frame
        or two for VBR). Live streams cannot be seeked and raise :class:`NotPlaying`.
        """
        async with self._lock:
            source = self.current
            if source is None or not self.state.is_active:
                raise NotPlaying("Nothing is playing, so there is nothing to seek")

            info = self._info.get(source.id, MediaInfo())

            # Start a background probe so subsequent seeks benefit. Never block playback.
            self._probe_in_background(source)

            # If we already know this is a live stream (no duration), refuse to seek.
            if info.duration is None and source.kind.value == "url":
                raise NotPlaying(
                    f"{source.display_name!r} is a live stream with no known duration; "
                    "seeking is not possible."
                )

            target = max(0.0, float(position))
            if info.duration is not None:
                # Leave a moment at the end so a seek does not land past EOF.
                target = min(target, max(0.0, info.duration - 0.5))

            byte_offset = info.byte_offset_for(target) or 0
            seeked = dataclass_replace(source, start_at=target, byte_offset=byte_offset)
            self._info[seeked.id] = info

            was_paused = self.state.state is PlaybackState.PAUSED
            await self._stop_reader()
            self._reset_stream_state()
            await self.queue.replace_current(seeked)
            await self._restart_from(seeked, keep_paused=was_paused)
            logger.info(
                "Seeked %s to %.1fs (%s)",
                seeked.display_name,
                target,
                f"byte offset {byte_offset}" if byte_offset else "container seek",
            )
            return target

    async def forward(self, seconds: float = 10.0) -> float:
        """Jump forward, relative to the current position."""
        if seconds < 0:
            return await self.rewind(-seconds)
        return await self.seek(self.position + seconds)

    async def rewind(self, seconds: float = 10.0) -> float:
        """Jump backward, relative to the current position (clamped at the start)."""
        if seconds < 0:
            return await self.forward(-seconds)
        return await self.seek(max(0.0, self.position - seconds))

    async def replay(self) -> float:
        """Restart the current track from the beginning."""
        return await self.seek(0.0)

    # -- lifecycle ----------------------------------------------------------------------------

    async def close(self) -> None:
        """Release every resource. Idempotent."""
        if self._closed:
            return
        self._closed = True
        async with self._lock:
            await self._stop_reader()
            await self._stop_video()
            self._buffer.close()
            self.track.set_provider(None)
        logger.debug("Player closed")

    # -- video ---------------------------------------------------------------------------

    @property
    def video_track(self) -> H264StreamTrack | None:
        return self._video_track

    @property
    def is_playing_video(self) -> bool:
        return self._video_process is not None and self._video_process.is_running

    async def play_video(self, source: Any, *, replace: bool = True) -> AudioSource:
        """Start (or replace) video playback.

        ``source`` is classified the same way as :meth:`play`.  The audio track
        is unaffected; this method only starts the H.264 decode pipeline.
        """
        if self._video_track is None:
            raise AytgcallsError("No video track — create Player with video_track=...")
        async with self._lock:
            track = validate_source(source)
            if not replace and self.is_playing_video:
                raise AytgcallsError("Video already playing; pass replace=True to switch")
            await self._stop_video()
            self._video_source = track
            binary = self.config.resolve_ffmpeg()
            process = FFmpegVideoProcess(
                track,
                binary=binary,
                http_fetch=self.config.fetch_urls_with_python,
                http_headers=self.config.http_headers,
            )
            self._video_process = process
            await process.start()
            self._video_track.set_provider(self._video_nalu_provider)
            logger.info("Playing video: %s", track.display_name)
            return track

    async def stop_video(self) -> None:
        """Stop video playback without touching the audio track."""
        async with self._lock:
            await self._stop_video()

    async def _stop_video(self) -> None:
        task, self._video_reader_task = self._video_reader_task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        process, self._video_process = self._video_process, None
        if process is not None:
            await process.stop()
        if self._video_track is not None:
            self._video_track.set_provider(None)
        self._video_source = None

    async def _video_nalu_provider(self) -> bytes | None:
        if self._video_process is None:
            return None
        return await self._video_process.read_nalu()

    # -- reader task ---------------------------------------------------------------------------

    async def _restart_from(self, source: AudioSource, *, keep_paused: bool = False) -> None:
        """(Re)start decoding at ``source``. Caller holds the lock."""
        self._decoding.clear()
        self._frames_played = 0
        self._position_offset = source.start_at
        if keep_paused:
            self._resume_event.clear()
            if self.state.state is not PlaybackState.PAUSED:
                self.state.transition(PlaybackState.PAUSED)
        else:
            self.state.transition(PlaybackState.PLAYING)
            self._resume_event.set()
        self._reader_task = asyncio.ensure_future(self._reader_loop())
        self._reader_task.add_done_callback(_log_task_exception)

    def _reset_stream_state(self) -> None:
        self._buffer.clear()
        self._end_markers.clear()
        self._decoding.clear()
        self._skip_event.clear()
        self._frames_played = 0

    async def _stop_reader(self) -> None:
        task, self._reader_task = self._reader_task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        process, self._process = self._process, None
        if process is not None:
            await process.stop()

    async def _ensure_info(self, source: AudioSource) -> MediaInfo:
        """Return metadata for ``source``, waiting for an in-flight probe if needed."""
        if source.id in self._info:
            return self._info[source.id]
        task = self._info_tasks.get(source.id)
        if task is None:
            task = asyncio.ensure_future(probe_media_info(source))
            self._info_tasks[source.id] = task
        try:
            info = await task
        except Exception as exc:  # probing must never break playback
            logger.debug("metadata probe failed: %s", exc)
            info = MediaInfo()
        finally:
            self._info_tasks.pop(source.id, None)
        self._info[source.id] = info
        return info

    def _probe_in_background(self, source: AudioSource) -> None:
        if source.id in self._info or source.id in self._info_tasks:
            return
        task = asyncio.ensure_future(self._ensure_info(source))
        task.add_done_callback(_log_task_exception)

    async def _reader_loop(self) -> None:
        """Decode the current track, then the next one, into the shared ring buffer."""
        binary = self.config.resolve_ffmpeg()
        try:
            while not self._closed:
                source = self.queue.current
                if source is None:
                    break
                self._decoding.append(source)
                self._probe_in_background(source)
                try:
                    await self._decode_source(source, binary)
                except asyncio.CancelledError:
                    raise
                except (FFmpegError, MediaSourceError) as exc:
                    logger.error("Playback of %s failed: %s", source.display_name, exc)
                    with contextlib.suppress(ValueError):
                        self._decoding.remove(source)
                    self._emit_stream_end(source, StreamEndReason.ERROR)
                if self._skip_event.is_set():
                    return  # skip() drives the transition itself
                following = await self.queue.next()
                if following is None:
                    await self._drain_and_idle()
                    return
                logger.info("Advancing to %s", following.display_name)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Reader loop crashed")
            raise

    async def _decode_source(self, source: AudioSource, binary: str) -> None:
        process = FFmpegProcess(
            source,
            binary=binary,
            chunk_size=BYTES_PER_FRAME,
            http_fetch=self.config.fetch_urls_with_python,
            http_headers=self.config.http_headers,
        )
        self._process = process
        await process.start()
        try:
            while True:
                await self._resume_event.wait()  # pause stops us consuming the pipe
                chunk = await process.read(BYTES_PER_FRAME)
                if not chunk:
                    break
                if len(chunk) < BYTES_PER_FRAME:
                    chunk = chunk + b"\x00" * (BYTES_PER_FRAME - len(chunk))
                await self._buffer.write(chunk)  # backpressure lives here
        finally:
            self._process = None
            await process.stop()
        # Everything written for this source; note the frame index where it ends.
        self._end_markers.append((self._buffer.frames_written, source))

    async def _drain_and_idle(self) -> None:
        """Wait for the buffered tail to actually play out, then go idle."""
        # Bounded: if the sender stopped pulling we must not hang here forever.
        timeout = (self._buffer.capacity_frames + 2) * _FRAME_SECONDS * 3
        await self._buffer.wait_drained(timeout=timeout)
        self._check_end_markers()
        # Any marker still pending (buffer cleared under us) fires now.
        while self._end_markers:
            _, source = self._end_markers.pop(0)
            self._emit_stream_end(source, StreamEndReason.COMPLETED)
        self._decoding.clear()
        if self.state.state is PlaybackState.PLAYING:
            self.state.transition(PlaybackState.IDLE)
        logger.info("Queue exhausted; player idle")

    async def __aenter__(self) -> Player:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()


def _log_task_exception(task: asyncio.Task[Any]) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("Background task failed: %r", exc, exc_info=exc)


#: Branded alias.
AyPlayer = Player
