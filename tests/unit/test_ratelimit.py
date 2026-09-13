from __future__ import annotations

import pytest

from tubetrace_mcp.ratelimit import TokenBucket


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_burst_then_refill() -> None:
    clock = Clock()
    bucket = TokenBucket(rate_per_second=1.0, burst=2, clock=clock)
    assert bucket.try_acquire() is None
    assert bucket.try_acquire() is None
    wait = bucket.try_acquire()
    assert wait is not None and wait == pytest.approx(1.0)
    clock.now += 0.5
    wait = bucket.try_acquire()
    assert wait is not None and wait == pytest.approx(0.5)
    clock.now += 0.5
    assert bucket.try_acquire() is None


def test_invalid_configuration() -> None:
    with pytest.raises(ValueError):
        TokenBucket(rate_per_second=0, burst=1)
    with pytest.raises(ValueError):
        TokenBucket(rate_per_second=1, burst=0)
