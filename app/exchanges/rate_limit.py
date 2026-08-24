import asyncio
from collections.abc import Awaitable, Callable
from time import monotonic
from typing import TypeVar

from app.exchanges.base import ExchangeError

T = TypeVar("T")


class RateLimitManager:
    """Per-adapter serialized request queue with bounded exponential backoff."""

    def __init__(self, requests_per_second: float = 8, max_retries: int = 3) -> None:
        if requests_per_second <= 0:
            raise ValueError("requests_per_second must be positive")
        self.minimum_interval = 1 / requests_per_second
        self.max_retries = max_retries
        self._lock = asyncio.Lock()
        self._last_request_at = 0.0

    async def execute(self, operation: Callable[[], Awaitable[T]]) -> T:
        async with self._lock:
            delay = self.minimum_interval - (monotonic() - self._last_request_at)
            if delay > 0:
                await asyncio.sleep(delay)
            for attempt in range(self.max_retries + 1):
                try:
                    result = await operation()
                    self._last_request_at = monotonic()
                    return result
                except ExchangeError:
                    self._last_request_at = monotonic()
                    if attempt == self.max_retries:
                        raise
                    await asyncio.sleep(min(0.25 * 2**attempt, 4))
        raise ExchangeError("request retry loop exhausted")
