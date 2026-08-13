"""Explicit playback state machine.

Keeping the legal transitions in one table means ``pause()`` on an idle player is a
clean :class:`NotPlaying` rather than a silent no-op that desynchronises the UI.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from ..exceptions import AlreadyPlaying, NotPlaying
from ..logger import get_logger
from ..types import PlaybackState

logger = get_logger("player.state")

__all__ = ["StateMachine", "TRANSITIONS"]

#: ``(from, to)`` pairs that are allowed.
TRANSITIONS: frozenset[tuple[PlaybackState, PlaybackState]] = frozenset(
    {
        (PlaybackState.IDLE, PlaybackState.PLAYING),
        (PlaybackState.IDLE, PlaybackState.STOPPED),
        (PlaybackState.PLAYING, PlaybackState.PAUSED),
        (PlaybackState.PLAYING, PlaybackState.STOPPED),
        (PlaybackState.PLAYING, PlaybackState.IDLE),
        (PlaybackState.PLAYING, PlaybackState.PLAYING),  # track change
        (PlaybackState.PAUSED, PlaybackState.PLAYING),
        (PlaybackState.PAUSED, PlaybackState.STOPPED),
        (PlaybackState.PAUSED, PlaybackState.IDLE),
        (PlaybackState.STOPPED, PlaybackState.PLAYING),
        (PlaybackState.STOPPED, PlaybackState.IDLE),
        (PlaybackState.STOPPED, PlaybackState.STOPPED),
        (PlaybackState.IDLE, PlaybackState.IDLE),
    }
)

Listener = Callable[[PlaybackState, PlaybackState], Awaitable[None] | None]


class StateMachine:
    """Guarded :class:`PlaybackState` holder."""

    def __init__(self, initial: PlaybackState = PlaybackState.IDLE) -> None:
        self._state = initial
        self._listeners: list[Listener] = []

    @property
    def state(self) -> PlaybackState:
        return self._state

    @property
    def is_active(self) -> bool:
        """Playing or paused — i.e. a track is loaded."""
        return self._state in (PlaybackState.PLAYING, PlaybackState.PAUSED)

    def can(self, target: PlaybackState) -> bool:
        return (self._state, target) in TRANSITIONS

    def transition(self, target: PlaybackState) -> PlaybackState:
        """Move to ``target``; raises if the transition is not legal."""
        if not self.can(target):
            raise self._error_for(target)
        previous, self._state = self._state, target
        if previous is not target:
            logger.debug("state %s -> %s", previous.value, target.value)
            for listener in self._listeners:
                listener(previous, target)
        return previous

    def _error_for(self, target: PlaybackState) -> Exception:
        if target is PlaybackState.PAUSED:
            return NotPlaying(f"Cannot pause while {self._state.value}")
        if target is PlaybackState.PLAYING and self._state is PlaybackState.PLAYING:
            return AlreadyPlaying("Already playing")
        return NotPlaying(f"Illegal transition {self._state.value} -> {target.value}")

    def require(self, *states: PlaybackState) -> None:
        if self._state not in states:
            raise NotPlaying(
                f"Requires state {' or '.join(s.value for s in states)}, "
                f"but player is {self._state.value}"
            )

    def on_change(self, listener: Listener) -> None:
        self._listeners.append(listener)
