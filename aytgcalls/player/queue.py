"""Track queue: FIFO with loop modes, shuffle and history.

The queue owns *what plays next*; the player owns *how it plays*. Everything is guarded
by an :class:`asyncio.Lock` so a command handler and the player's own advance cannot
corrupt the list.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Iterable, Iterator
from typing import Any

from ..logger import get_logger
from ..types import AudioSource, LoopMode

logger = get_logger("player.queue")

__all__ = ["TrackQueue", "AyQueue"]


class TrackQueue:
    """An async-safe playlist."""

    def __init__(
        self,
        *,
        loop: LoopMode = LoopMode.OFF,
        history_size: int = 50,
        loop_times: int = 0,
    ) -> None:
        self._items: list[AudioSource] = []
        self._current: AudioSource | None = None
        self._history: list[AudioSource] = []
        self._history_size = history_size
        self._lock = asyncio.Lock()
        self.loop: LoopMode = loop
        #: Remaining repeats when :attr:`loop` is :attr:`LoopMode.TIMES`.
        self.loop_times = loop_times
        #: Reshuffle the pending list every time the queue wraps around.
        self.auto_shuffle = False

    # -- introspection ------------------------------------------------------------------

    @property
    def current(self) -> AudioSource | None:
        """The track being played right now (not part of ``items``)."""
        return self._current

    @property
    def items(self) -> tuple[AudioSource, ...]:
        """Upcoming tracks, in order."""
        return tuple(self._items)

    @property
    def history(self) -> tuple[AudioSource, ...]:
        return tuple(self._history)

    def __len__(self) -> int:
        return len(self._items)

    def __iter__(self) -> Iterator[AudioSource]:
        return iter(tuple(self._items))

    def __bool__(self) -> bool:
        return bool(self._items) or self._current is not None

    def __repr__(self) -> str:
        return (
            f"<TrackQueue current={self._current} pending={len(self._items)} "
            f"loop={self.loop.value}>"
        )

    # -- mutation ------------------------------------------------------------------------

    async def add(self, source: Any, *, position: int | None = None) -> AudioSource:
        """Append (or insert) a track. Accepts a path, URL or :class:`AudioSource`."""
        track = AudioSource.from_any(source)
        async with self._lock:
            if position is None:
                self._items.append(track)
            else:
                self._items.insert(max(0, position), track)
        logger.debug("queued %s (pending=%d)", track.display_name, len(self._items))
        return track

    async def extend(self, sources: Iterable[Any]) -> list[AudioSource]:
        return [await self.add(source) for source in sources]

    async def remove(self, index: int) -> AudioSource:
        """Remove and return the queued track at ``index``."""
        async with self._lock:
            try:
                return self._items.pop(index)
            except IndexError as exc:
                raise IndexError(
                    f"Queue index {index} out of range (0..{len(self._items) - 1})"
                ) from exc

    async def clear(self) -> int:
        """Drop every pending track (the current one keeps playing)."""
        async with self._lock:
            count = len(self._items)
            self._items.clear()
        return count

    async def shuffle(self) -> None:
        """Shuffle the pending tracks in place."""
        async with self._lock:
            random.shuffle(self._items)

    async def move(self, source_index: int, target_index: int) -> None:
        async with self._lock:
            item = self._items.pop(source_index)
            self._items.insert(max(0, min(target_index, len(self._items))), item)

    # -- advancing -------------------------------------------------------------------------

    async def next(self) -> AudioSource | None:
        """Advance according to the loop mode and return the new current track.

        * ``LoopMode.TRACK`` — repeats the current track forever.
        * ``LoopMode.QUEUE`` — re-appends the finished track to the back.
        * ``LoopMode.OFF``  — plain FIFO; returns ``None`` when exhausted.
        """
        async with self._lock:
            finished = self._current
            if self.loop is LoopMode.TRACK and finished is not None:
                return finished
            if self.loop is LoopMode.TIMES and finished is not None and self.loop_times > 0:
                self.loop_times -= 1
                if self.loop_times == 0:
                    # Repeats used up: fall back to normal FIFO from here on.
                    self.loop = LoopMode.OFF
                return finished
            if finished is not None:
                self._push_history(finished)
                if self.loop is LoopMode.QUEUE:
                    self._items.append(finished)
                    if self.auto_shuffle:
                        random.shuffle(self._items)
            self._current = self._items.pop(0) if self._items else None
            return self._current

    async def previous(self) -> AudioSource | None:
        """Step back to the most recently finished track."""
        async with self._lock:
            if not self._history:
                return None
            track = self._history.pop()
            if self._current is not None:
                self._items.insert(0, self._current)
            self._current = track
            return track

    async def set_current(self, source: Any | None) -> AudioSource | None:
        """Force the current track (used by ``play()`` to jump the queue)."""
        async with self._lock:
            if source is None:
                self._current = None
                return None
            track = AudioSource.from_any(source)
            if self._current is not None:
                self._push_history(self._current)
            self._current = track
            return track

    async def replace_current(self, source: AudioSource) -> AudioSource:
        """Swap the current track in place, without touching the history.

        Used by :meth:`~aytgcalls.player.player.Player.seek`, which restarts the same
        track at a different offset and must not make it look like a track change.
        """
        async with self._lock:
            self._current = source
            return source

    async def reset(self) -> None:
        """Clear everything: pending, current and history."""
        async with self._lock:
            self._items.clear()
            self._history.clear()
            self._current = None
            self.loop_times = 0

    def _push_history(self, track: AudioSource) -> None:
        self._history.append(track)
        if len(self._history) > self._history_size:
            del self._history[0 : len(self._history) - self._history_size]


#: Branded alias.
AyQueue = TrackQueue
