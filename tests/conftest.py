"""Shared fixtures: hermetic settings, fake transcript provider, mocked Google API."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator, Sequence
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from tubetrace_mcp.errors import TubeTraceError
from tubetrace_mcp.providers.base import RawSegment, RawTranscript, TrackSelector
from tubetrace_mcp.schemas import TranscriptTrack
from tubetrace_mcp.search_client import YouTubeSearchClient
from tubetrace_mcp.settings import Settings

ENV_VARS = [
    "APP_ENV",
    "HOST",
    "PORT",
    "YOUTUBE_API_KEY",
    "MCP_TOKEN_SHA256",
    "AUTH_DISABLED",
    "AUTH_MODE",
    "MCP_DOMAIN",
    "ACME_EMAIL",
    "ALLOWED_HOSTS",
    "ALLOWED_ORIGINS",
    "LOG_LEVEL",
    "LOG_FORMAT",
    "MCP_JSON_RESPONSE",
    "FORWARDED_ALLOW_IPS",
]

VIDEO_ID = "dQw4w9WgXcQ"
FIXED_NOW = datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep tests independent from the developer's shell environment and .env file."""
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def make_settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "_env_file": None,
        "app_env": "development",
        "auth_disabled": True,
        "host": "127.0.0.1",
        "port": 8000,
    }
    base.update(overrides)
    return Settings(**base)


def fixed_now() -> datetime:
    return FIXED_NOW


# ------------------------------------------------------------------ fake provider


def make_segments(
    count: int, *, text: str = "word", duration: float = 2.0
) -> tuple[RawSegment, ...]:
    return tuple(
        RawSegment(text=f"{text} {index}", start=index * duration, duration=duration)
        for index in range(count)
    )


DEFAULT_TRACKS: tuple[TranscriptTrack, ...] = (
    TranscriptTrack(language="English", language_code="en", is_generated=False),
    TranscriptTrack(language="English (auto-generated)", language_code="en", is_generated=True),
    TranscriptTrack(language="Ukrainian", language_code="uk", is_generated=True),
)


class FakeProvider:
    """In-memory provider honouring the TranscriptProvider protocol."""

    name = "fake_provider"

    def __init__(
        self,
        tracks: Sequence[TranscriptTrack] = DEFAULT_TRACKS,
        segments: Sequence[RawSegment] | None = None,
        error: TubeTraceError | None = None,
    ) -> None:
        self.tracks = list(tracks)
        self.segments = tuple(segments) if segments is not None else make_segments(5)
        self.error = error
        self.list_calls = 0
        self.fetch_calls = 0

    async def list_transcripts(self, video_id: str) -> list[TranscriptTrack]:
        self.list_calls += 1
        if self.error is not None:
            raise self.error
        return list(self.tracks)

    async def fetch_transcript(self, video_id: str, select: TrackSelector) -> RawTranscript:
        self.fetch_calls += 1
        if self.error is not None:
            raise self.error
        track = select(self.tracks)
        return RawTranscript(
            video_id=video_id,
            language=track.language,
            language_code=track.language_code,
            is_generated=track.is_generated,
            segments=self.segments,
        )


@pytest.fixture
def fake_provider() -> FakeProvider:
    return FakeProvider()


# -------------------------------------------------------------------- Google mock


def google_search_payload(
    count: int = 2,
    *,
    next_page_token: str | None = "NEXT_TOKEN",  # noqa: S107 - not a secret
    total_results: int = 1000,
) -> dict[str, Any]:
    items = []
    for index in range(count):
        items.append(
            {
                "kind": "youtube#searchResult",
                "id": {"kind": "youtube#video", "videoId": f"vid{index:08d}"},
                "snippet": {
                    "publishedAt": "2024-05-01T10:00:00Z",
                    "channelId": "UC" + "x" * 22,
                    "title": f"Title &amp; {index} &#39;quoted&#39;",
                    "description": f"Description &lt;{index}&gt;",
                    "thumbnails": {
                        "default": {"url": f"https://i.ytimg.com/vi/vid{index}/default.jpg"},
                        "high": {"url": f"https://i.ytimg.com/vi/vid{index}/hqdefault.jpg"},
                    },
                    "channelTitle": "Channel &amp; Co",
                    "liveBroadcastContent": "none",
                },
            }
        )
    payload: dict[str, Any] = {
        "kind": "youtube#searchListResponse",
        "regionCode": "UA",
        "pageInfo": {"totalResults": total_results, "resultsPerPage": count},
        "items": items,
    }
    if next_page_token:
        payload["nextPageToken"] = next_page_token
    return payload


class GoogleMock:
    """Records requests and serves queued responses (default: one page of results)."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.responses: list[httpx.Response | Callable[[httpx.Request], httpx.Response]] = []

    def queue(self, *responses: httpx.Response | Callable[[httpx.Request], httpx.Response]) -> None:
        self.responses.extend(responses)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.responses:
            response = self.responses.pop(0)
            return response(request) if callable(response) else response
        return httpx.Response(200, json=google_search_payload())


def google_error(
    status: int, reason: str, message: str = "error", **headers: str
) -> httpx.Response:
    body = {"error": {"code": status, "message": message, "errors": [{"reason": reason}]}}
    return httpx.Response(status, json=body, headers=headers)


@pytest.fixture
def google_mock() -> GoogleMock:
    return GoogleMock()


@pytest.fixture
def mock_http(google_mock: GoogleMock) -> Iterator[httpx.AsyncClient]:
    client = httpx.AsyncClient(transport=httpx.MockTransport(google_mock.handler))
    yield client


class NoSleep:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.calls.append(delay)


def make_search_client(
    http: httpx.AsyncClient,
    *,
    api_key: str | None = "test-google-key-0123456789",
    sleep: NoSleep | None = None,
    **kwargs: Any,
) -> YouTubeSearchClient:
    return YouTubeSearchClient(
        http=http,
        api_key=api_key,
        base_url="https://www.googleapis.com/youtube/v3",
        sleep=sleep or NoSleep(),
        rng=lambda: 0.0,
        now=fixed_now,
        **kwargs,
    )


def decode_error(text: str) -> dict[str, Any]:
    payload = json.loads(text)
    error: dict[str, Any] = payload["error"]
    return error
