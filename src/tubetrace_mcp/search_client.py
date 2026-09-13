"""Async client for the official YouTube Data API v3 ``search.list`` endpoint.

* The API key is sent in the ``X-Goog-Api-Key`` header, never in the URL, so
  request URLs can be logged safely.
* One call performs exactly one upstream request (one result page).
* Transient failures (429, 5xx, timeouts, network errors) are retried a bounded
  number of times with backoff/jitter, honouring ``Retry-After`` and a total time
  budget. Quota exhaustion and invalid credentials are never retried.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import random
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx
from pydantic import TypeAdapter, ValidationError

from .errors import ErrorCode, TubeTraceError
from .retry import backoff_delay, parse_retry_after
from .schemas import (
    PROVIDER_YOUTUBE_DATA_API,
    SearchVideoItem,
    SearchVideosRequest,
    SearchVideosResult,
)
from .video_input import canonical_video_url

logger = logging.getLogger(__name__)

_DATETIME = TypeAdapter(datetime)
_THUMBNAIL_PREFERENCE = ("maxres", "standard", "high", "medium", "default")
_FIELDS = (
    "nextPageToken,prevPageToken,regionCode,pageInfo,"
    "items(id,snippet(publishedAt,channelId,title,description,thumbnails,"
    "channelTitle,liveBroadcastContent))"
)


def _rfc3339(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_search_params(request: SearchVideosRequest) -> dict[str, str]:
    """Translate validated request fields into ``search.list`` query parameters."""
    params: dict[str, str] = {
        "part": "snippet",
        "type": "video",
        "q": request.query,
        "maxResults": str(request.max_results),
        "order": request.order.value,
        "videoDuration": request.video_duration.value,
        "videoCaption": request.caption_filter.value,
        "safeSearch": request.safe_search.value,
        "fields": _FIELDS,
    }
    if request.page_token:
        params["pageToken"] = request.page_token
    if request.channel_id:
        params["channelId"] = request.channel_id
    if request.published_after is not None:
        params["publishedAfter"] = _rfc3339(request.published_after)
    if request.published_before is not None:
        params["publishedBefore"] = _rfc3339(request.published_before)
    if request.relevance_language:
        params["relevanceLanguage"] = request.relevance_language
    if request.region_code:
        params["regionCode"] = request.region_code
    return params


async def _read_bounded(response: httpx.Response, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > max_bytes:
            raise TubeTraceError(
                ErrorCode.UPSTREAM_ERROR,
                "Google returned a response larger than the configured limit.",
                details={"max_bytes": max_bytes},
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _google_error(status: int, body: bytes, headers: httpx.Headers) -> TubeTraceError:
    reason: str | None = None
    message: str | None = None
    try:
        payload = json.loads(body.decode("utf-8", errors="replace")) if body else {}
        error = payload.get("error", {}) if isinstance(payload, dict) else {}
        errors = error.get("errors") if isinstance(error, dict) else None
        if isinstance(errors, list) and errors and isinstance(errors[0], dict):
            reason = errors[0].get("reason")
        if isinstance(error, dict):
            raw_message = error.get("message")
            if isinstance(raw_message, str):
                message = raw_message[:200]
            if reason is None and isinstance(error.get("status"), str):
                reason = error["status"]
    except (ValueError, AttributeError):
        pass

    details: dict[str, Any] = {"status": status}
    if reason:
        details["reason"] = reason
    retry_after = parse_retry_after(headers.get("Retry-After"))

    if status == 429 or reason in {"rateLimitExceeded", "userRateLimitExceeded"}:
        return TubeTraceError(
            ErrorCode.UPSTREAM_RATE_LIMITED,
            "Google rate-limited the request; retry later.",
            retryable=True,
            details=details,
            retry_after_seconds=retry_after,
        )
    if status == 403 and reason in {"quotaExceeded", "dailyLimitExceeded"}:
        details["hint"] = "The daily YouTube Data API quota resets at midnight Pacific Time."
        return TubeTraceError(
            ErrorCode.GOOGLE_QUOTA_EXCEEDED,
            "The YouTube Data API quota for this Google Cloud project is exhausted.",
            details=details,
        )
    if status in {401, 403} or (status == 400 and reason == "keyInvalid"):
        return TubeTraceError(
            ErrorCode.GOOGLE_API_KEY_INVALID,
            "Google rejected the API key, or the YouTube Data API v3 is not enabled or "
            "allowed for it. Check the key, its API restrictions and the project.",
            details=details,
        )
    if status == 400:
        if message:
            details["google_message"] = message
        return TubeTraceError(
            ErrorCode.INVALID_ARGUMENT,
            "Google rejected one of the search parameters.",
            details=details,
        )
    if status >= 500:
        return TubeTraceError(
            ErrorCode.UPSTREAM_ERROR,
            "Google returned a server error.",
            retryable=True,
            details=details,
            retry_after_seconds=retry_after,
        )
    return TubeTraceError(
        ErrorCode.UPSTREAM_ERROR, "Unexpected response from Google.", details=details
    )


def _pick_thumbnail(thumbnails: Any) -> str | None:
    if not isinstance(thumbnails, dict):
        return None
    for key in _THUMBNAIL_PREFERENCE:
        entry = thumbnails.get(key)
        if isinstance(entry, dict) and isinstance(entry.get("url"), str):
            return str(entry["url"])
    return None


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return _DATETIME.validate_python(value)
    except ValidationError:
        logger.warning("google_invalid_datetime", extra={"value": value[:40]})
        return None


def map_search_response(
    request: SearchVideosRequest, payload: dict[str, Any], *, retrieved_at: datetime
) -> SearchVideosResult:
    """Map Google's JSON into :class:`SearchVideosResult` (HTML entities decoded)."""
    items: list[SearchVideoItem] = []
    for raw in payload.get("items") or []:
        if not isinstance(raw, dict):
            continue
        identifier = raw.get("id") or {}
        video_id = identifier.get("videoId") if isinstance(identifier, dict) else None
        if not isinstance(video_id, str) or not video_id:
            continue
        snippet = raw.get("snippet") or {}
        if not isinstance(snippet, dict):
            snippet = {}
        items.append(
            SearchVideoItem(
                video_id=video_id,
                video_url=canonical_video_url(video_id),
                title=html.unescape(str(snippet.get("title") or "")),
                description=html.unescape(str(snippet.get("description") or "")),
                channel_id=str(snippet.get("channelId") or ""),
                channel_title=html.unescape(str(snippet.get("channelTitle") or "")),
                published_at=_parse_datetime(snippet.get("publishedAt")),
                thumbnail_url=_pick_thumbnail(snippet.get("thumbnails")),
                live_broadcast_content=(
                    str(snippet["liveBroadcastContent"])
                    if isinstance(snippet.get("liveBroadcastContent"), str)
                    else None
                ),
            )
        )
    page_info = payload.get("pageInfo") or {}
    if not isinstance(page_info, dict):
        page_info = {}
    total = page_info.get("totalResults")
    per_page = page_info.get("resultsPerPage")
    return SearchVideosResult(
        provider=PROVIDER_YOUTUBE_DATA_API,
        query=request.query,
        items=items,
        next_page_token=payload.get("nextPageToken") or None,
        prev_page_token=payload.get("prevPageToken") or None,
        results_per_page=int(per_page) if isinstance(per_page, int) else None,
        total_results_estimate=int(total) if isinstance(total, int) else None,
        region_code=payload.get("regionCode")
        if isinstance(payload.get("regionCode"), str)
        else None,
        retrieved_at=retrieved_at,
        cache_hit=False,
    )


class YouTubeSearchClient:
    """Thin async wrapper around ``search.list`` with bounded retries."""

    def __init__(
        self,
        *,
        http: httpx.AsyncClient,
        api_key: str | None,
        base_url: str = "https://www.googleapis.com/youtube/v3",
        max_retries: int = 2,
        retry_budget_seconds: float = 20.0,
        max_response_bytes: int = 2 * 1024 * 1024,
        max_concurrency: int = 4,
        queue_timeout_seconds: float = 10.0,
        sleep: Callable[[float], Any] = asyncio.sleep,
        rng: Callable[[], float] = random.random,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._http = http
        self._api_key = api_key.strip() if api_key else None
        self._base_url = base_url.rstrip("/")
        self._max_retries = max_retries
        self._retry_budget = retry_budget_seconds
        self._max_response_bytes = max_response_bytes
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._queue_timeout = queue_timeout_seconds
        self._sleep = sleep
        self._rng = rng
        self._clock = clock
        self._now = now

    @property
    def configured(self) -> bool:
        return bool(self._api_key)

    async def search(self, request: SearchVideosRequest) -> SearchVideosResult:
        if not self._api_key:
            raise TubeTraceError(
                ErrorCode.GOOGLE_API_NOT_CONFIGURED,
                "YOUTUBE_API_KEY is not configured on the server, so video search is "
                "unavailable. Transcript tools keep working.",
            )
        deadline = self._clock() + self._retry_budget
        attempt = 0
        while True:
            try:
                return await self._search_once(request)
            except TubeTraceError as err:
                if not err.retryable or attempt >= self._max_retries:
                    raise
                delay = backoff_delay(attempt, retry_after=err.retry_after_seconds, rng=self._rng)
                if self._clock() + delay > deadline:
                    raise
                logger.info(
                    "google_search_retry",
                    extra={"attempt": attempt + 1, "error_code": str(err.code), "delay": delay},
                )
                await self._sleep(delay)
                attempt += 1

    async def _search_once(self, request: SearchVideosRequest) -> SearchVideosResult:
        assert self._api_key is not None
        params = build_search_params(request)
        headers = {"X-Goog-Api-Key": self._api_key, "Accept": "application/json"}
        http_request = self._http.build_request(
            "GET", f"{self._base_url}/search", params=params, headers=headers
        )
        try:
            await asyncio.wait_for(self._semaphore.acquire(), timeout=self._queue_timeout)
        except TimeoutError:
            raise TubeTraceError(
                ErrorCode.SERVER_BUSY,
                "Too many concurrent search requests; try again shortly.",
                retryable=True,
                retry_after_seconds=1.0,
            ) from None
        try:
            try:
                response = await self._http.send(http_request, stream=True)
            except httpx.TimeoutException:
                raise TubeTraceError(
                    ErrorCode.UPSTREAM_TIMEOUT,
                    "Timed out while contacting the YouTube Data API.",
                    retryable=True,
                ) from None
            except httpx.HTTPError as exc:
                raise TubeTraceError(
                    ErrorCode.UPSTREAM_ERROR,
                    "Network error while contacting the YouTube Data API.",
                    retryable=True,
                    details={"reason": type(exc).__name__},
                ) from None
            try:
                try:
                    body = await _read_bounded(response, self._max_response_bytes)
                except httpx.TimeoutException:
                    raise TubeTraceError(
                        ErrorCode.UPSTREAM_TIMEOUT,
                        "Timed out while reading the YouTube Data API response.",
                        retryable=True,
                    ) from None
                except httpx.HTTPError as exc:
                    raise TubeTraceError(
                        ErrorCode.UPSTREAM_ERROR,
                        "Network error while reading the YouTube Data API response.",
                        retryable=True,
                        details={"reason": type(exc).__name__},
                    ) from None
            finally:
                await response.aclose()
        finally:
            self._semaphore.release()

        if response.status_code != 200:
            raise _google_error(response.status_code, body, response.headers)
        try:
            payload = json.loads(body.decode("utf-8"))
        except ValueError:
            raise TubeTraceError(
                ErrorCode.UPSTREAM_ERROR, "Google returned a non-JSON response."
            ) from None
        if not isinstance(payload, dict):
            raise TubeTraceError(ErrorCode.UPSTREAM_ERROR, "Google returned an unexpected payload.")
        return map_search_response(request, payload, retrieved_at=self._now())
