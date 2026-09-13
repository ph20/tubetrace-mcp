"""Per-process token-bucket rate limiter (not shared across replicas)."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable


class TokenBucket:
    """Classic token bucket: ``rate_per_second`` refill, ``burst`` capacity."""

    def __init__(
        self,
        *,
        rate_per_second: float,
        burst: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if rate_per_second <= 0 or burst <= 0:
            raise ValueError("rate_per_second and burst must be positive")
        self._rate = rate_per_second
        self._capacity = float(burst)
        self._tokens = float(burst)
        self._clock = clock
        self._updated = clock()
        self._lock = threading.Lock()

    def _refill(self) -> None:
        now = self._clock()
        elapsed = max(0.0, now - self._updated)
        self._updated = now
        self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)

    def try_acquire(self, tokens: float = 1.0) -> float | None:
        """Take ``tokens`` if available.

        Returns ``None`` on success, otherwise the number of seconds until enough
        tokens will be available.
        """
        with self._lock:
            self._refill()
            if self._tokens >= tokens:
                self._tokens -= tokens
                return None
            deficit = tokens - self._tokens
            return deficit / self._rate
