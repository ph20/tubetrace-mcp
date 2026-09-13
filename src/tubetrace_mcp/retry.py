"""Helpers for bounded retries with exponential backoff and jitter."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

DEFAULT_BASE_DELAY = 0.5
DEFAULT_MAX_DELAY = 8.0


def parse_retry_after(value: str | None, *, now: datetime | None = None) -> float | None:
    """Parse an HTTP ``Retry-After`` header (delta-seconds or HTTP-date)."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        delta: float = float(value)
        return delta
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    current = now or datetime.now(UTC)
    seconds: float = float((when - current).total_seconds())
    return max(0.0, seconds)


def backoff_delay(
    attempt: int,
    *,
    retry_after: float | None = None,
    base: float = DEFAULT_BASE_DELAY,
    cap: float = DEFAULT_MAX_DELAY,
    rng: Callable[[], float],
) -> float:
    """Exponential backoff with jitter; honours ``retry_after`` when it is larger."""
    delay = min(cap, base * (2.0**attempt))
    delay += rng() * base
    if retry_after is not None:
        delay = max(delay, retry_after)
    return delay
