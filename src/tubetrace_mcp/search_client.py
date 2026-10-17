"""Async client for the official YouTube Data API v3 read-only endpoints.

* Covers ``search.list``, ``videos.list``, ``channels.list``, ``playlists.list``, and
  ``playlistItems.list``.
* The API key is sent in the ``X-Goog-Api-Key`` header, never in the URL, so
  request URLs can be logged safely.
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
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
from pydantic import TypeAdapter, ValidationError

from .errors import ErrorCode, TubeTraceError
from .retry import backoff_delay, parse_retry_after
from .schemas import (
    PROVIDER_YOUTUBE_DATA_API,
    ChannelPlaylistItem,
    ChannelPlaylistsResult,
    ChannelResult,
    PlaylistResult,
    PlaylistVideoItem,
    SearchVideoItem,
    SearchVideosRequest,
    SearchVideosResult,
    VideoMetadataResult,
)
from .video_input import (
    ChannelTarget,
    canonical_channel_url,
    canonical_playlist_url,
    canonical_video_url,
    parse_iso8601_duration,
)

logger = logging.getLogger(__name__)

_DATETIME = TypeAdapter(datetime)
_THUMBNAIL_PREFERENCE = ("maxres", "standard", "high", "medium", "default")
_SEARCH_FIELDS = (
    "nextPageToken,prevPageToken,regionCode,pageInfo,"
    "items(id,snippet(publishedAt,channelId,title,description,thumbnails,"
    "channelTitle,liveBroadcastContent))"
)
_VIDEO_FIELDS = (
    "items(id,snippet(publishedAt,channelId,title,description,thumbnails,channelTitle,"
    "tags,categoryId,liveBroadcastContent,defaultLanguage,defaultAudioLanguage),"
    "contentDetails(duration,definition,caption),"
    "statistics(viewCount,likeCount,commentCount))"
)
_CHANNEL_FIELDS = (
    "items(id,snippet(title,description,customUrl,publishedAt,thumbnails,country),"
    "contentDetails(relatedPlaylists(uploads)),"
    "statistics(viewCount,subscriberCount,videoCount))"
)
_PLAYLIST_FIELDS = (
    "nextPageToken,prevPageToken,pageInfo,"
    "items(id,snippet(publishedAt,channelId,title,description,thumbnails,channelTitle),"
    "contentDetails(itemCount))"
)
_PLAYLIST_ITEMS_FIELDS = (
    "nextPageToken,prevPageToken,pageInfo,"
    "items(snippet(publishedAt,channelId,title,description,thumbnails,channelTitle,"
    "position,resourceId,videoOwnerChannelTitle,videoOwnerChannelId),"
    "contentDetails(videoId,videoPublishedAt),status(privacyStatus))"
)


@dataclass(frozen=True, slots=True)
class PlaylistItemsPage:
    """One page of items from ``playlistItems.list``."""

    items: list[PlaylistVideoItem]
    next_page_token: str | None
    prev_page_token: str | None
    retrieved_at: datetime


def _rfc3339(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


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
        "fields": _SEARCH_FIELDS,
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


def _google_error(
    status: int,
    body: bytes,
    headers: httpx.Headers,
    *,
    resource_type: str | None = None,
) -> TubeTraceError:
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
    if resource_type == "playlist" and (
        status == 404
        or reason in {"playlistNotFound", "playlistForbidden", "playlistOperationUnsupported"}
    ):
        return TubeTraceError(
            ErrorCode.PLAYLIST_NOT_FOUND,
            "The requested playlist was not found or is private.",
            details=details,
        )
    if resource_type == "video" and (status == 404 or reason == "videoNotFound"):
        return TubeTraceError(
            ErrorCode.VIDEO_NOT_FOUND,
            "The requested video was not found.",
            details=details,
        )
    if resource_type == "channel" and (status == 404 or reason == "channelNotFound"):
        return TubeTraceError(
            ErrorCode.CHANNEL_NOT_FOUND,
            "The requested channel was not found.",
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
            "Google rejected one of the request parameters.",
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


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def map_video_response(
    video_id: str, payload: dict[str, Any], *, retrieved_at: datetime
) -> VideoMetadataResult:
    """Map ``videos.list`` JSON into :class:`VideoMetadataResult`."""
    items = [item for item in (payload.get("items") or []) if isinstance(item, dict)]
    if not items:
        raise TubeTraceError(
            ErrorCode.VIDEO_NOT_FOUND,
            f"Video '{video_id}' was not found or is private.",
            details={"video_id": video_id},
        )
    raw = items[0]
    actual_id = str(raw.get("id") or video_id)
    snippet = _as_dict(raw.get("snippet"))
    content_details = _as_dict(raw.get("contentDetails"))
    statistics = _as_dict(raw.get("statistics"))

    channel_id = str(snippet.get("channelId") or "")
    duration_iso = (
        str(content_details["duration"])
        if isinstance(content_details.get("duration"), str)
        else None
    )
    caption_raw = content_details.get("caption")
    has_captions: bool | None = None
    if isinstance(caption_raw, bool):
        has_captions = caption_raw
    elif isinstance(caption_raw, str):
        if caption_raw.lower() == "true":
            has_captions = True
        elif caption_raw.lower() == "false":
            has_captions = False

    raw_tags = snippet.get("tags")
    tags = (
        [html.unescape(str(tag)) for tag in raw_tags if isinstance(tag, str)]
        if isinstance(raw_tags, list)
        else []
    )

    return VideoMetadataResult(
        provider=PROVIDER_YOUTUBE_DATA_API,
        video_id=actual_id,
        video_url=canonical_video_url(actual_id),
        title=html.unescape(str(snippet.get("title") or "")),
        description=html.unescape(str(snippet.get("description") or "")),
        channel_id=channel_id,
        channel_title=html.unescape(str(snippet.get("channelTitle") or "")),
        channel_url=canonical_channel_url(channel_id) if channel_id else "",
        published_at=_parse_datetime(snippet.get("publishedAt")),
        duration_iso=duration_iso,
        duration_seconds=parse_iso8601_duration(duration_iso),
        has_captions=has_captions,
        definition=(
            str(content_details["definition"])
            if isinstance(content_details.get("definition"), str)
            else None
        ),
        default_language=(
            str(snippet["defaultLanguage"])
            if isinstance(snippet.get("defaultLanguage"), str)
            else None
        ),
        default_audio_language=(
            str(snippet["defaultAudioLanguage"])
            if isinstance(snippet.get("defaultAudioLanguage"), str)
            else None
        ),
        tags=tags,
        category_id=(
            str(snippet["categoryId"]) if isinstance(snippet.get("categoryId"), str) else None
        ),
        view_count=_parse_int(statistics.get("viewCount")),
        like_count=_parse_int(statistics.get("likeCount")),
        comment_count=_parse_int(statistics.get("commentCount")),
        live_broadcast_content=(
            str(snippet["liveBroadcastContent"])
            if isinstance(snippet.get("liveBroadcastContent"), str)
            else None
        ),
        thumbnail_url=_pick_thumbnail(snippet.get("thumbnails")),
        retrieved_at=retrieved_at,
        cache_hit=False,
    )


def map_channel_response(
    target: ChannelTarget, payload: dict[str, Any], *, retrieved_at: datetime
) -> ChannelResult:
    """Map ``channels.list`` JSON into :class:`ChannelResult`."""
    items = [item for item in (payload.get("items") or []) if isinstance(item, dict)]
    if not items:
        raise TubeTraceError(
            ErrorCode.CHANNEL_NOT_FOUND,
            "The requested YouTube channel was not found.",
            details={"channel": target.channel_id or target.handle or ""},
        )
    raw = items[0]
    channel_id = str(raw.get("id") or target.channel_id or "")
    snippet = _as_dict(raw.get("snippet"))
    content_details = _as_dict(raw.get("contentDetails"))
    related = _as_dict(content_details.get("relatedPlaylists"))
    statistics = _as_dict(raw.get("statistics"))
    uploads_id = (
        str(related["uploads"])
        if isinstance(related.get("uploads"), str) and related.get("uploads")
        else None
    )

    return ChannelResult(
        provider=PROVIDER_YOUTUBE_DATA_API,
        channel_id=channel_id,
        channel_url=canonical_channel_url(channel_id) if channel_id else "",
        title=html.unescape(str(snippet.get("title") or "")),
        description=html.unescape(str(snippet.get("description") or "")),
        custom_url=(
            str(snippet["customUrl"]) if isinstance(snippet.get("customUrl"), str) else None
        ),
        country=str(snippet["country"]) if isinstance(snippet.get("country"), str) else None,
        published_at=_parse_datetime(snippet.get("publishedAt")),
        thumbnail_url=_pick_thumbnail(snippet.get("thumbnails")),
        subscriber_count=_parse_int(statistics.get("subscriberCount")),
        video_count=_parse_int(statistics.get("videoCount")),
        view_count=_parse_int(statistics.get("viewCount")),
        uploads_playlist_id=uploads_id,
        retrieved_at=retrieved_at,
        cache_hit=False,
    )


def map_playlist_response(
    playlist_id: str, payload: dict[str, Any], *, retrieved_at: datetime
) -> PlaylistResult:
    """Map ``playlists.list`` JSON into :class:`PlaylistResult`."""
    items = [item for item in (payload.get("items") or []) if isinstance(item, dict)]
    if not items:
        raise TubeTraceError(
            ErrorCode.PLAYLIST_NOT_FOUND,
            f"Playlist '{playlist_id}' was not found or is private.",
            details={"playlist_id": playlist_id},
        )
    raw = items[0]
    actual_id = str(raw.get("id") or playlist_id)
    snippet = _as_dict(raw.get("snippet"))
    content_details = _as_dict(raw.get("contentDetails"))
    return PlaylistResult(
        provider=PROVIDER_YOUTUBE_DATA_API,
        playlist_id=actual_id,
        playlist_url=canonical_playlist_url(actual_id),
        title=html.unescape(str(snippet.get("title") or "")),
        description=html.unescape(str(snippet.get("description") or "")),
        channel_id=str(snippet.get("channelId") or ""),
        channel_title=html.unescape(str(snippet.get("channelTitle") or "")),
        published_at=_parse_datetime(snippet.get("publishedAt")),
        item_count=_parse_int(content_details.get("itemCount")),
        thumbnail_url=_pick_thumbnail(snippet.get("thumbnails")),
        retrieved_at=retrieved_at,
        cache_hit=False,
    )


def map_playlist_items_response(
    payload: dict[str, Any], *, retrieved_at: datetime
) -> PlaylistItemsPage:
    """Map ``playlistItems.list`` JSON into :class:`PlaylistItemsPage`."""
    items: list[PlaylistVideoItem] = []
    for raw in payload.get("items") or []:
        if not isinstance(raw, dict):
            continue
        snippet = _as_dict(raw.get("snippet"))
        content_details = _as_dict(raw.get("contentDetails"))
        status = _as_dict(raw.get("status"))
        resource_id = _as_dict(snippet.get("resourceId"))
        video_id = content_details.get("videoId") or resource_id.get("videoId")
        if not isinstance(video_id, str) or not video_id:
            continue
        published_at = _parse_datetime(content_details.get("videoPublishedAt")) or _parse_datetime(
            snippet.get("publishedAt")
        )
        added_at = _parse_datetime(snippet.get("publishedAt"))
        owner_id = (
            str(snippet["videoOwnerChannelId"])
            if isinstance(snippet.get("videoOwnerChannelId"), str)
            else None
        )
        owner_title = (
            html.unescape(str(snippet["videoOwnerChannelTitle"]))
            if isinstance(snippet.get("videoOwnerChannelTitle"), str)
            else None
        )
        items.append(
            PlaylistVideoItem(
                position=_parse_int(snippet.get("position")),
                video_id=video_id,
                video_url=canonical_video_url(video_id),
                title=html.unescape(str(snippet.get("title") or "")),
                description=html.unescape(str(snippet.get("description") or "")),
                video_owner_channel_id=owner_id,
                video_owner_channel_title=owner_title,
                published_at=published_at,
                added_at=added_at,
                privacy_status=(
                    str(status["privacyStatus"])
                    if isinstance(status.get("privacyStatus"), str)
                    else None
                ),
                thumbnail_url=_pick_thumbnail(snippet.get("thumbnails")),
            )
        )
    return PlaylistItemsPage(
        items=items,
        next_page_token=payload.get("nextPageToken") or None,
        prev_page_token=payload.get("prevPageToken") or None,
        retrieved_at=retrieved_at,
    )


def map_channel_playlists_response(
    channel_id: str, payload: dict[str, Any], *, retrieved_at: datetime
) -> ChannelPlaylistsResult:
    """Map ``playlists.list`` (by channelId) into :class:`ChannelPlaylistsResult`."""
    playlists: list[ChannelPlaylistItem] = []
    for raw in payload.get("items") or []:
        if not isinstance(raw, dict):
            continue
        playlist_id = raw.get("id")
        if not isinstance(playlist_id, str) or not playlist_id:
            continue
        snippet = _as_dict(raw.get("snippet"))
        content_details = _as_dict(raw.get("contentDetails"))
        playlists.append(
            ChannelPlaylistItem(
                playlist_id=playlist_id,
                playlist_url=canonical_playlist_url(playlist_id),
                title=html.unescape(str(snippet.get("title") or "")),
                description=html.unescape(str(snippet.get("description") or "")),
                item_count=_parse_int(content_details.get("itemCount")),
                published_at=_parse_datetime(snippet.get("publishedAt")),
                thumbnail_url=_pick_thumbnail(snippet.get("thumbnails")),
            )
        )
    page_info = _as_dict(payload.get("pageInfo"))
    total = page_info.get("totalResults")
    per_page = page_info.get("resultsPerPage")
    return ChannelPlaylistsResult(
        provider=PROVIDER_YOUTUBE_DATA_API,
        channel_id=channel_id,
        channel_url=canonical_channel_url(channel_id),
        playlists=playlists,
        next_page_token=payload.get("nextPageToken") or None,
        prev_page_token=payload.get("prevPageToken") or None,
        results_per_page=int(per_page) if isinstance(per_page, int) else None,
        total_results_estimate=int(total) if isinstance(total, int) else None,
        retrieved_at=retrieved_at,
        cache_hit=False,
    )


class YouTubeSearchClient:
    """Async wrapper around YouTube Data API v3 read-only endpoints with bounded retries."""

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
        payload = await self._get_json("search", build_search_params(request))
        return map_search_response(request, payload, retrieved_at=self._now())

    async def get_video(self, video_id: str) -> VideoMetadataResult:
        params = {
            "part": "snippet,contentDetails,statistics",
            "id": video_id,
            "fields": _VIDEO_FIELDS,
        }
        payload = await self._get_json("videos", params, resource_type="video")
        return map_video_response(video_id, payload, retrieved_at=self._now())

    async def get_channel(self, target: ChannelTarget) -> ChannelResult:
        params: dict[str, str] = {
            "part": "snippet,contentDetails,statistics",
            "fields": _CHANNEL_FIELDS,
        }
        if target.channel_id is not None:
            params["id"] = target.channel_id
        else:
            assert target.handle is not None
            params["forHandle"] = target.handle
        payload = await self._get_json("channels", params, resource_type="channel")
        return map_channel_response(target, payload, retrieved_at=self._now())

    async def get_playlist(self, playlist_id: str) -> PlaylistResult:
        params = {
            "part": "snippet,contentDetails",
            "id": playlist_id,
            "fields": _PLAYLIST_FIELDS,
        }
        payload = await self._get_json("playlists", params, resource_type="playlist")
        return map_playlist_response(playlist_id, payload, retrieved_at=self._now())

    async def list_playlist_items(
        self,
        playlist_id: str,
        *,
        max_results: int = 25,
        page_token: str | None = None,
    ) -> PlaylistItemsPage:
        params: dict[str, str] = {
            "part": "snippet,contentDetails,status",
            "playlistId": playlist_id,
            "maxResults": str(max_results),
            "fields": _PLAYLIST_ITEMS_FIELDS,
        }
        if page_token:
            params["pageToken"] = page_token
        payload = await self._get_json("playlistItems", params, resource_type="playlist")
        return map_playlist_items_response(payload, retrieved_at=self._now())

    async def list_channel_playlists(
        self,
        channel_id: str,
        *,
        max_results: int = 25,
        page_token: str | None = None,
    ) -> ChannelPlaylistsResult:
        params: dict[str, str] = {
            "part": "snippet,contentDetails",
            "channelId": channel_id,
            "maxResults": str(max_results),
            "fields": _PLAYLIST_FIELDS,
        }
        if page_token:
            params["pageToken"] = page_token
        payload = await self._get_json("playlists", params, resource_type="channel")
        return map_channel_playlists_response(channel_id, payload, retrieved_at=self._now())

    async def _get_json(
        self,
        endpoint: str,
        params: dict[str, str],
        *,
        resource_type: str | None = None,
    ) -> dict[str, Any]:
        if not self._api_key:
            raise TubeTraceError(
                ErrorCode.GOOGLE_API_NOT_CONFIGURED,
                "YOUTUBE_API_KEY is not configured on the server, so video search and "
                "metadata tools are unavailable. Transcript tools keep working.",
            )
        deadline = self._clock() + self._retry_budget
        attempt = 0
        while True:
            try:
                return await self._request_once(endpoint, params, resource_type=resource_type)
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

    async def _request_once(
        self,
        endpoint: str,
        params: dict[str, str],
        *,
        resource_type: str | None = None,
    ) -> dict[str, Any]:
        assert self._api_key is not None
        headers = {"X-Goog-Api-Key": self._api_key, "Accept": "application/json"}
        http_request = self._http.build_request(
            "GET", f"{self._base_url}/{endpoint}", params=params, headers=headers
        )
        try:
            await asyncio.wait_for(self._semaphore.acquire(), timeout=self._queue_timeout)
        except TimeoutError:
            raise TubeTraceError(
                ErrorCode.SERVER_BUSY,
                "Too many concurrent YouTube Data API requests; try again shortly.",
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
            raise _google_error(
                response.status_code, body, response.headers, resource_type=resource_type
            )
        try:
            payload = json.loads(body.decode("utf-8"))
        except ValueError:
            raise TubeTraceError(
                ErrorCode.UPSTREAM_ERROR, "Google returned a non-JSON response."
            ) from None
        if not isinstance(payload, dict):
            raise TubeTraceError(ErrorCode.UPSTREAM_ERROR, "Google returned an unexpected payload.")
        return payload
