"""Exponential backoff reconnect manager.

Pure logic, no I/O: the delay sequence is deterministic when jitter is 0, which is what
makes it unit-testable.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from ..logger import get_logger

logger = get_logger("call.reconnect")

__all__ = ["BackoffPolicy", "ReconnectManager"]


@dataclass(frozen=True)
class BackoffPolicy:
    """Exponential backoff with optional multiplicative jitter."""

    initial_delay: float = 1.0
    max_delay: float = 30.0
    multiplier: float = 2.0
    max_attempts: int = 8
    #: Fractional jitter: 0.3 means the delay is scaled by U(0.7, 1.3).
    jitter: float = 0.3

    def delay_for(self, attempt: int, *, rng: random.Random | None = None) -> float:
        """Delay before ``attempt`` (1-based)."""
        if attempt < 1:
            raise ValueError("attempt is 1-based")
        raw = min(self.initial_delay * (self.multiplier ** (attempt - 1)), self.max_delay)
        if self.jitter:
            source = rng or random
            raw *= 1.0 + source.uniform(-self.jitter, self.jitter)
        return max(0.0, raw)

    def should_retry(self, attempt: int) -> bool:
        return self.max_attempts <= 0 or attempt <= self.max_attempts


class ReconnectManager:
    """Runs ``connect`` with backoff until it succeeds or the policy is exhausted."""

    def __init__(
        self,
        connect: Callable[[], Awaitable[None]],
        *,
        policy: BackoffPolicy | None = None,
        on_give_up: Callable[[BaseException], Awaitable[None] | None] | None = None,
    ) -> None:
        self._connect = connect
        self.policy = policy or BackoffPolicy()
        self._on_give_up = on_give_up
        self._task: asyncio.Task[None] | None = None
        self.attempts = 0
        self._running = False

    @property
    def is_running(self) -> bool:
        return self._running

    def start(self) -> asyncio.Task[None]:
        """Kick off reconnection in the background (no-op if already running)."""
        if self._task is not None and not self._task.done():
            return self._task
        self._task = asyncio.ensure_future(self.run())
        return self._task

    async def run(self) -> None:
        """Retry ``connect`` until success or exhaustion."""
        self._running = True
        self.attempts = 0
        last_error: BaseException | None = None
        try:
            while self.policy.should_retry(self.attempts + 1):
                self.attempts += 1
                delay = self.policy.delay_for(self.attempts)
                logger.info(
                    "Reconnect attempt %d/%s in %.1fs",
                    self.attempts,
                    self.policy.max_attempts or "∞",
                    delay,
                )
                await asyncio.sleep(delay)
                try:
                    await self._connect()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    last_error = exc
                    logger.warning("Reconnect attempt %d failed: %s", self.attempts, exc)
                    continue
                logger.info("Reconnected after %d attempt(s)", self.attempts)
                return
            logger.error("Giving up after %d reconnect attempts", self.attempts)
            if self._on_give_up is not None and last_error is not None:
                result = self._on_give_up(last_error)
                if asyncio.iscoroutine(result):
                    await result
        finally:
            self._running = False

    async def cancel(self) -> None:
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._running = False
