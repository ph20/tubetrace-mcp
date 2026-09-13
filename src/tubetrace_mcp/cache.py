"""Bounded in-memory TTL cache with an LRU eviction policy and a byte budget.

The cache is an optimisation only: correctness never depends on a hit. It is
process-local (not shared between workers, replicas or restarts).
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(slots=True)
class _Entry[T]:
    value: T
    expires_at: float
    size_bytes: int


class TTLCache[T]:
    """LRU cache bounded by entry count and by an approximate byte budget."""

    def __init__(
        self,
        *,
        max_entries: int,
        max_bytes: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_entries < 0 or max_bytes < 0:
            raise ValueError("max_entries and max_bytes must be non-negative")
        self._max_entries = max_entries
        self._max_bytes = max_bytes
        self._clock = clock
        self._entries: OrderedDict[str, _Entry[T]] = OrderedDict()
        self._current_bytes = 0
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0
        self.evictions = 0

    @property
    def current_bytes(self) -> int:
        return self._current_bytes

    def __len__(self) -> int:
        return len(self._entries)

    def get(self, key: str) -> T | None:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                self.misses += 1
                return None
            if entry.expires_at <= self._clock():
                self._remove(key)
                self.misses += 1
                return None
            self._entries.move_to_end(key)
            self.hits += 1
            return entry.value

    def set(self, key: str, value: T, *, ttl_seconds: float, size_bytes: int) -> bool:
        """Store ``value``; returns False when the value cannot be cached.

        Values larger than the whole budget, and non-positive TTLs, are not cached.
        """
        if ttl_seconds <= 0 or size_bytes < 0 or size_bytes > self._max_bytes:
            return False
        if self._max_entries == 0:
            return False
        with self._lock:
            self._purge_expired()
            if key in self._entries:
                self._remove(key)
            self._entries[key] = _Entry(
                value=value, expires_at=self._clock() + ttl_seconds, size_bytes=size_bytes
            )
            self._current_bytes += size_bytes
            while self._entries and (
                len(self._entries) > self._max_entries or self._current_bytes > self._max_bytes
            ):
                oldest_key = next(iter(self._entries))
                self._remove(oldest_key)
                self.evictions += 1
            return key in self._entries

    def delete(self, key: str) -> None:
        with self._lock:
            if key in self._entries:
                self._remove(key)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._current_bytes = 0

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "entries": len(self._entries),
                "bytes": self._current_bytes,
                "hits": self.hits,
                "misses": self.misses,
                "evictions": self.evictions,
            }

    def _remove(self, key: str) -> None:
        entry = self._entries.pop(key)
        self._current_bytes -= entry.size_bytes

    def _purge_expired(self) -> None:
        now = self._clock()
        expired = [key for key, entry in self._entries.items() if entry.expires_at <= now]
        for key in expired:
            self._remove(key)
