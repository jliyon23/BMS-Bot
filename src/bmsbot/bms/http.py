from __future__ import annotations

import asyncio
import time


class AsyncRateLimiter:
    """Small token bucket shared by every scraper task in a process."""

    def __init__(self, rate_per_second: float, burst: int | None = None) -> None:
        self.rate = max(0.1, rate_per_second)
        self.capacity = float(burst or max(1, int(self.rate)))
        self._tokens = self.capacity
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._updated
            self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
            self._updated = now
            if self._tokens < 1:
                wait = (1 - self._tokens) / self.rate
                await asyncio.sleep(wait)
                self._tokens = 0
                self._updated = time.monotonic()
            else:
                self._tokens -= 1
