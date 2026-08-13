"""Bounded PCM jitter/prefetch buffer.

A writer task (the FFmpeg reader) pushes frames in; the RTP track pulls frames out every
20 ms. The buffer bounds memory (nothing is ever fully loaded into RAM), applies
backpressure to the writer, and holds the first ``prefetch`` frames back so FFmpeg
startup latency and network jitter do not cause an immediate underrun.
"""

from __future__ import annotations

import asyncio

from ..logger import get_logger
from ..types import BYTES_PER_FRAME, FRAME_MS

logger = get_logger("media.buffer")

__all__ = ["PcmRingBuffer"]


class PcmRingBuffer:
    """An asyncio FIFO of fixed-size PCM frames with prefetch gating."""

    def __init__(
        self,
        *,
        capacity_ms: int = 400,
        prefetch_ms: int = 200,
        frame_bytes: int = BYTES_PER_FRAME,
        frame_ms: int = FRAME_MS,
    ) -> None:
        self._frame_bytes = frame_bytes
        self._frame_ms = frame_ms
        self._capacity = max(2, capacity_ms // frame_ms)
        self._prefetch = max(1, min(prefetch_ms // frame_ms, self._capacity))
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=self._capacity)
        self._primed = asyncio.Event()
        #: Set whenever the buffer holds nothing; lets the player await drain instead of
        #: polling for it.
        self._drained = asyncio.Event()
        self._drained.set()
        self._eof = False
        self._closed = False
        self.frames_written = 0
        self.frames_read = 0

    # -- introspection ----------------------------------------------------------------

    @property
    def capacity_frames(self) -> int:
        return self._capacity

    @property
    def prefetch_frames(self) -> int:
        return self._prefetch

    @property
    def size(self) -> int:
        return self._queue.qsize()

    @property
    def buffered_ms(self) -> int:
        return self._queue.qsize() * self._frame_ms

    @property
    def is_primed(self) -> bool:
        return self._primed.is_set()

    @property
    def is_closed(self) -> bool:
        return self._closed

    @property
    def at_eof(self) -> bool:
        """EOF marker pushed *and* everything drained."""
        return self._eof and self._queue.empty()

    # -- writing ----------------------------------------------------------------------

    async def write(self, frame: bytes) -> None:
        """Append one frame, awaiting space (this is the writer's backpressure)."""
        if self._closed:
            return
        await self._queue.put(frame)
        self.frames_written += 1
        self._drained.clear()
        if not self._primed.is_set() and self._queue.qsize() >= self._prefetch:
            self._primed.set()

    def try_write(self, frame: bytes) -> bool:
        """Non-blocking write; ``False`` when full."""
        if self._closed:
            return False
        try:
            self._queue.put_nowait(frame)
        except asyncio.QueueFull:
            return False
        self.frames_written += 1
        self._drained.clear()
        if not self._primed.is_set() and self._queue.qsize() >= self._prefetch:
            self._primed.set()
        return True

    async def mark_eof(self) -> None:
        """Signal that no more frames will be written for the current stream."""
        self._eof = True
        self._primed.set()  # release any reader waiting on prefetch
        await self._queue.put(None)

    def reset_eof(self) -> None:
        """Clear the EOF flag when a new source starts feeding the same buffer."""
        self._eof = False

    # -- reading ----------------------------------------------------------------------

    async def wait_primed(self, timeout: float | None = None) -> bool:
        """Block until the prefetch threshold (or EOF) is reached."""
        if self._primed.is_set():
            return True
        try:
            await asyncio.wait_for(self._primed.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return False
        return True

    async def wait_drained(self, timeout: float | None = None) -> bool:
        """Block until every buffered frame has been consumed (or ``timeout``)."""
        if self._queue.empty():
            return True
        try:
            await asyncio.wait_for(self._drained.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return False
        return True

    def read_nowait(self) -> bytes | None:
        """Pop one frame, or ``None`` when empty/EOF. Never blocks — the pacer owns time."""
        if self._closed:
            return None
        try:
            frame = self._queue.get_nowait()
        except asyncio.QueueEmpty:
            self._drained.set()
            return None
        if self._queue.empty():
            self._drained.set()
        if frame is None:  # EOF sentinel
            self._eof = True
            return None
        self.frames_read += 1
        return frame

    async def read(self, timeout: float | None = None) -> bytes | None:
        """Pop one frame, waiting up to ``timeout`` seconds."""
        if self._closed:
            return None
        try:
            frame = (
                await asyncio.wait_for(self._queue.get(), timeout=timeout)
                if timeout is not None
                else await self._queue.get()
            )
        except asyncio.TimeoutError:
            return None
        if self._queue.empty():
            self._drained.set()
        if frame is None:
            self._eof = True
            return None
        self.frames_read += 1
        return frame

    # -- lifecycle --------------------------------------------------------------------

    def clear(self) -> None:
        """Drop everything buffered (used by skip/stop so stale audio never plays)."""
        while True:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        self._primed.clear()
        self._drained.set()
        self._eof = False

    def close(self) -> None:
        self._closed = True
        self.clear()
        self._primed.set()
