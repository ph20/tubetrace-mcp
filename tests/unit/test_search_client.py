from __future__ import annotations

import json
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from tests.conftest import (
    GoogleMock,
    NoSleep,
    google_error,
    google_search_payload,
    make_search_client,
)
from tubetrace_mcp.errors import ErrorCode, TubeTraceError
from tubetrace_mcp.logging_config import SecretRedactor
from tubetrace_mcp.schemas import (
    CaptionFilter,
    SafeSearch,
    SearchOrder,
    SearchVideosRequest,
    VideoDuration,
)
from tubetrace_mcp.search_client import build_search_params

API_KEY = "test-google-key-0123456789"


def query_of(request: httpx.Request) -> dict[str, str]:
    parsed = parse_qs(urlsplit(str(request.url)).query)
    return {k: v[0] for k, v in parsed.items()}


async def test_request_parameters_and_key_in_header(
    mock_http: httpx.AsyncClient, google_mock: GoogleMock
) -> None:
    client = make_search_client(mock_http)
    request = SearchVideosRequest(
        query="  python asyncio ",
        max_results=25,
        page_token="CAoQAA",
        channel_id="UC" + "a" * 22,
        published_after=datetime(2024, 1, 1, tzinfo=UTC),
        published_before="2024-06-01T00:00:00+02:00",
        order=SearchOrder.VIEW_COUNT,
        relevance_language="uk",
        region_code="ua",
        video_duration=VideoDuration.LONG,
        caption_filter=CaptionFilter.CLOSED_CAPTION,
        safe_search=SafeSearch.STRICT,
    )
    result = await client.search(request)
    assert len(google_mock.requests) == 1
    sent = google_mock.requests[0]
    assert sent.method == "GET"
    assert str(sent.url).startswith("https://www.googleapis.com/youtube/v3/search?")
    assert sent.headers["X-Goog-Api-Key"] == API_KEY
    assert API_KEY not in str(sent.url)
    params = query_of(sent)
    assert params["part"] == "snippet"
    assert params["type"] == "video"
    assert params["q"] == "python asyncio"
    assert params["maxResults"] == "25"
    assert params["pageToken"] == "CAoQAA"
    assert params["channelId"] == "UC" + "a" * 22
    assert params["publishedAfter"] == "2024-01-01T00:00:00Z"
    assert params["publishedBefore"] == "2024-05-31T22:00:00Z"
    assert params["order"] == "viewCount"
    assert params["relevanceLanguage"] == "uk"
    assert params["regionCode"] == "UA"
    assert params["videoDuration"] == "long"
    assert params["videoCaption"] == "closedCaption"
    assert params["safeSearch"] == "strict"
    assert "key" not in params
    assert result.query == "python asyncio"


def test_build_params_defaults() -> None:
    params = build_search_params(SearchVideosRequest(query="x"))
    assert params["maxResults"] == "10"
    assert params["order"] == "relevance"
    assert params["safeSearch"] == "moderate"
    assert "pageToken" not in params
    assert "channelId" not in params


async def test_response_mapping_and_html_entities(
    mock_http: httpx.AsyncClient, google_mock: GoogleMock
) -> None:
    client = make_search_client(mock_http)
    result = await client.search(SearchVideosRequest(query="x"))
    assert result.provider == "youtube_data_api_v3"
    assert result.next_page_token == "NEXT_TOKEN"
    assert result.total_results_estimate == 1000
    assert "approximation" in result.total_results_note
    assert result.region_code == "UA"
    assert result.cache_hit is False
    assert result.retrieved_at == datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
    item = result.items[0]
    assert item.video_id == "vid00000000"
    assert item.video_url == "https://www.youtube.com/watch?v=vid00000000"
    assert item.title == "Title & 0 'quoted'"
    assert item.description == "Description <0>"
    assert item.channel_title == "Channel & Co"
    assert item.thumbnail_url == "https://i.ytimg.com/vi/vid0/hqdefault.jpg"
    assert item.published_at == datetime(2024, 5, 1, 10, 0, tzinfo=UTC)
    assert item.live_broadcast_content == "none"


async def test_empty_results_and_invalid_dates_and_non_video_items(
    mock_http: httpx.AsyncClient, google_mock: GoogleMock
) -> None:
    payload = google_search_payload(1, next_page_token=None)
    payload["items"][0]["snippet"]["publishedAt"] = "not-a-date"
    payload["items"].append({"id": {"kind": "youtube#channel", "channelId": "UCx"}, "snippet": {}})
    google_mock.queue(
        httpx.Response(200, json=payload), httpx.Response(200, json={"items": [], "pageInfo": {}})
    )
    client = make_search_client(mock_http)
    result = await client.search(SearchVideosRequest(query="x"))
    assert len(result.items) == 1
    assert result.items[0].published_at is None
    assert result.next_page_token is None
    empty = await client.search(SearchVideosRequest(query="nothing"))
    assert empty.items == []
    assert empty.total_results_estimate is None


async def test_not_configured(mock_http: httpx.AsyncClient, google_mock: GoogleMock) -> None:
    client = make_search_client(mock_http, api_key=None)
    assert client.configured is False
    with pytest.raises(TubeTraceError) as info:
        await client.search(SearchVideosRequest(query="x"))
    assert info.value.code == ErrorCode.GOOGLE_API_NOT_CONFIGURED
    assert google_mock.requests == []


@pytest.mark.parametrize(
    ("response", "code", "retryable"),
    [
        (
            google_error(400, "keyInvalid", "API key not valid."),
            ErrorCode.GOOGLE_API_KEY_INVALID,
            False,
        ),
        (google_error(403, "forbidden"), ErrorCode.GOOGLE_API_KEY_INVALID, False),
        (google_error(403, "accessNotConfigured"), ErrorCode.GOOGLE_API_KEY_INVALID, False),
        (google_error(403, "quotaExceeded"), ErrorCode.GOOGLE_QUOTA_EXCEEDED, False),
        (
            google_error(400, "invalidSearchFilter", "Invalid value"),
            ErrorCode.INVALID_ARGUMENT,
            False,
        ),
        (httpx.Response(502, text="bad gateway"), ErrorCode.UPSTREAM_ERROR, True),
        (httpx.Response(200, text="<html>not json</html>"), ErrorCode.UPSTREAM_ERROR, False),
    ],
)
async def test_error_mapping_without_retries_for_non_transient(
    mock_http: httpx.AsyncClient,
    google_mock: GoogleMock,
    response: httpx.Response,
    code: ErrorCode,
    retryable: bool,
) -> None:
    google_mock.queue(response, response, response, response)
    client = make_search_client(mock_http, max_retries=2)
    with pytest.raises(TubeTraceError) as info:
        await client.search(SearchVideosRequest(query="x"))
    assert info.value.code == code
    assert info.value.retryable is retryable
    expected_calls = 3 if retryable else 1
    assert len(google_mock.requests) == expected_calls


async def test_rate_limit_retry_honours_retry_after(
    mock_http: httpx.AsyncClient, google_mock: GoogleMock
) -> None:
    google_mock.queue(google_error(429, "rateLimitExceeded", **{"Retry-After": "3"}))
    sleep = NoSleep()
    client = make_search_client(mock_http, sleep=sleep, max_retries=2, retry_budget_seconds=30)
    result = await client.search(SearchVideosRequest(query="x"))
    assert len(result.items) == 2
    assert len(google_mock.requests) == 2
    assert sleep.calls == [3.0]


async def test_retry_budget_stops_retries(
    mock_http: httpx.AsyncClient, google_mock: GoogleMock
) -> None:
    google_mock.queue(google_error(429, "rateLimitExceeded", **{"Retry-After": "120"}))
    sleep = NoSleep()
    client = make_search_client(mock_http, sleep=sleep, max_retries=3, retry_budget_seconds=10)
    with pytest.raises(TubeTraceError) as info:
        await client.search(SearchVideosRequest(query="x"))
    assert info.value.code == ErrorCode.UPSTREAM_RATE_LIMITED
    assert info.value.retry_after_seconds == 120
    assert sleep.calls == []


async def test_timeout_maps_and_retries(google_mock: GoogleMock) -> None:
    def raise_timeout(request: httpx.Request) -> httpx.Response:
        google_mock.requests.append(request)
        raise httpx.ReadTimeout("slow", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(raise_timeout)) as http:
        client = make_search_client(http, max_retries=1)
        with pytest.raises(TubeTraceError) as info:
            await client.search(SearchVideosRequest(query="x"))
    assert info.value.code == ErrorCode.UPSTREAM_TIMEOUT
    assert info.value.retryable is True
    assert len(google_mock.requests) == 2
    assert API_KEY not in json.dumps(info.value.to_dict())


async def test_network_error_maps(google_mock: GoogleMock) -> None:
    def raise_connect(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(raise_connect)) as http:
        client = make_search_client(http, max_retries=0)
        with pytest.raises(TubeTraceError) as info:
            await client.search(SearchVideosRequest(query="x"))
    assert info.value.code == ErrorCode.UPSTREAM_ERROR
    assert info.value.details["reason"] == "ConnectError"


async def test_oversized_upstream_body_is_rejected(
    mock_http: httpx.AsyncClient, google_mock: GoogleMock
) -> None:
    google_mock.queue(httpx.Response(200, content=b"x" * 5000))
    client = make_search_client(mock_http, max_response_bytes=1024, max_retries=0)
    with pytest.raises(TubeTraceError) as info:
        await client.search(SearchVideosRequest(query="x"))
    assert info.value.code == ErrorCode.UPSTREAM_ERROR
    assert info.value.details["max_bytes"] == 1024


def test_key_is_redacted_from_logs_and_error_text() -> None:
    redactor = SecretRedactor([API_KEY])
    line = f"GET https://www.googleapis.com/youtube/v3/search?q=x&key={API_KEY} failed"
    assert API_KEY not in redactor.redact(line)
    assert "[REDACTED]" in redactor.redact(line)
    assert "AIza" not in redactor.redact("AIzaSyD-1234567890abcdefghijklmnopqrstu leaked")
