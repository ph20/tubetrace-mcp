from __future__ import annotations

from tubetrace_mcp.cache import TTLCache


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_set_get_and_ttl_expiry() -> None:
    clock = Clock()
    cache: TTLCache[str] = TTLCache(max_entries=10, max_bytes=1000, clock=clock)
    assert cache.set("a", "value", ttl_seconds=10, size_bytes=5)
    assert cache.get("a") == "value"
    clock.now += 9.9
    assert cache.get("a") == "value"
    clock.now += 0.2
    assert cache.get("a") is None
    assert cache.stats()["entries"] == 0


def test_lru_eviction_by_entries() -> None:
    cache: TTLCache[int] = TTLCache(max_entries=2, max_bytes=1000)
    cache.set("a", 1, ttl_seconds=60, size_bytes=1)
    cache.set("b", 2, ttl_seconds=60, size_bytes=1)
    assert cache.get("a") == 1  # touch "a" so "b" becomes least recently used
    cache.set("c", 3, ttl_seconds=60, size_bytes=1)
    assert cache.get("b") is None
    assert cache.get("a") == 1
    assert cache.get("c") == 3
    assert cache.evictions == 1


def test_byte_budget_eviction_and_oversized_values() -> None:
    cache: TTLCache[str] = TTLCache(max_entries=100, max_bytes=100)
    assert cache.set("a", "x", ttl_seconds=60, size_bytes=60)
    assert cache.set("b", "y", ttl_seconds=60, size_bytes=60)
    assert cache.get("a") is None, "oldest entry evicted to respect the byte budget"
    assert cache.current_bytes == 60
    assert cache.set("huge", "z", ttl_seconds=60, size_bytes=101) is False
    assert cache.get("huge") is None
    assert cache.set("zero_ttl", "z", ttl_seconds=0, size_bytes=1) is False


def test_zero_capacity_disables_cache() -> None:
    cache: TTLCache[str] = TTLCache(max_entries=0, max_bytes=100)
    assert cache.set("a", "x", ttl_seconds=60, size_bytes=1) is False
    assert cache.get("a") is None


def test_overwrite_delete_clear_and_stats() -> None:
    cache: TTLCache[str] = TTLCache(max_entries=10, max_bytes=100)
    cache.set("a", "1", ttl_seconds=60, size_bytes=10)
    cache.set("a", "2", ttl_seconds=60, size_bytes=20)
    assert cache.get("a") == "2"
    assert cache.current_bytes == 20
    cache.delete("a")
    assert cache.get("a") is None
    cache.set("b", "3", ttl_seconds=60, size_bytes=5)
    cache.clear()
    assert len(cache) == 0
    stats = cache.stats()
    assert stats["hits"] == 1
    assert stats["misses"] == 1
