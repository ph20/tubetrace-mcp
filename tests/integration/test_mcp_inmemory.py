"""Real MCP protocol exchanges (initialize, tools/list, tools/call) over the in-memory transport."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from fastmcp import Client, FastMCP

from tests.conftest import (
    VIDEO_ID,
    FakeProvider,
    GoogleMock,
    decode_error,
    make_search_client,
    make_segments,
    make_settings,
)
from tubetrace_mcp.errors import ErrorCode, TubeTraceError
from tubetrace_mcp.server import create_server


@pytest.fixture
def server(fake_provider: FakeProvider, mock_http: httpx.AsyncClient) -> FastMCP[Any]:
    settings = make_settings()
    return create_server(
        settings, transcript_provider=fake_provider, search_client=make_search_client(mock_http)
    )


async def test_initialize_and_discovery(server: FastMCP[Any]) -> None:
    async with Client(server) as client:
        assert client.server_info is not None
        assert client.server_info.name == "tubetrace-mcp"
        assert client.instructions and "read-only" in client.instructions
        tools = {tool.name: tool for tool in await client.list_tools()}
        assert set(tools) == {
            "youtube_search_videos",
            "youtube_list_transcripts",
            "youtube_get_transcript",
        }
        for tool in tools.values():
            assert tool.annotations is not None
            assert tool.annotations.read_only_hint is True
            assert tool.annotations.open_world_hint is True
            assert tool.annotations.destructive_hint is False
            assert tool.output_schema is not None
            assert tool.description
        search_schema = tools["youtube_search_videos"].input_schema
        assert search_schema["properties"]["max_results"]["maximum"] == 50
        assert "closedCaption" in json.dumps(search_schema)
        assert "does NOT search inside transcript text" in (
            tools["youtube_search_videos"].description or ""
        )
        get_schema = tools["youtube_get_transcript"].input_schema
        assert get_schema["properties"]["limit"]["maximum"] == 500
        assert "api_key" not in json.dumps(search_schema).lower()


async def test_standard_handshake_mode_supports_ping(server: FastMCP[Any]) -> None:
    """Standard MCP clients (initialize handshake) can ping and list tools."""
    async with Client(server, mode="legacy") as client:
        assert await client.ping() is True
        assert client.initialize_result is not None
        assert client.initialize_result.server_info.name == "tubetrace-mcp"
        assert len(await client.list_tools()) == 3


async def test_search_returns_structured_items(
    server: FastMCP[Any], google_mock: GoogleMock
) -> None:
    async with Client(server) as client:
        result = await client.call_tool(
            "youtube_search_videos", {"query": "python", "max_results": 2}
        )
        assert result.is_error is False
        assert result.structured_content is not None
        assert result.structured_content["items"][0]["video_id"] == "vid00000000"
        assert result.structured_content["next_page_token"] == "NEXT_TOKEN"
        assert result.structured_content["cache_hit"] is False
        assert result.data.items[0].title == "Title & 0 'quoted'"
        again = await client.call_tool(
            "youtube_search_videos", {"query": "python", "max_results": 2}
        )
        assert again.structured_content is not None
        assert again.structured_content["cache_hit"] is True
        assert len(google_mock.requests) == 1
        page2 = await client.call_tool(
            "youtube_search_videos",
            {"query": "python", "max_results": 2, "page_token": "NEXT_TOKEN"},
        )
        assert page2.structured_content is not None
        assert page2.structured_content["cache_hit"] is False
        assert len(google_mock.requests) == 2


async def test_search_without_api_key_is_a_tool_error(
    fake_provider: FakeProvider, mock_http: httpx.AsyncClient
) -> None:
    server = create_server(
        make_settings(),
        transcript_provider=fake_provider,
        search_client=make_search_client(mock_http, api_key=None),
    )
    async with Client(server) as client:
        result = await client.call_tool(
            "youtube_search_videos", {"query": "python"}, raise_on_error=False
        )
        assert result.is_error is True
        error = decode_error(result.content[0].text)
        assert error["code"] == "GOOGLE_API_NOT_CONFIGURED"
        assert result.structured_content == {"error": error}
        tracks = await client.call_tool("youtube_list_transcripts", {"video": VIDEO_ID})
        assert tracks.is_error is False, "transcript tools work without a Google key"


async def test_google_quota_error_surfaces_code(
    server: FastMCP[Any], google_mock: GoogleMock
) -> None:
    google_mock.queue(
        httpx.Response(
            403,
            json={
                "error": {"code": 403, "message": "quota", "errors": [{"reason": "quotaExceeded"}]}
            },
        )
    )
    async with Client(server) as client:
        result = await client.call_tool(
            "youtube_search_videos", {"query": "python"}, raise_on_error=False
        )
        assert result.is_error is True
        assert decode_error(result.content[0].text)["code"] == "GOOGLE_QUOTA_EXCEEDED"


async def test_invalid_arguments_produce_error_results(server: FastMCP[Any]) -> None:
    async with Client(server) as client:
        bad_date = await client.call_tool(
            "youtube_search_videos",
            {"query": "x", "published_after": "yesterday"},
            raise_on_error=False,
        )
        assert bad_date.is_error is True
        assert decode_error(bad_date.content[0].text)["code"] == "INVALID_ARGUMENT"
        naive = await client.call_tool(
            "youtube_search_videos",
            {"query": "x", "published_after": "2024-01-01T00:00:00"},
            raise_on_error=False,
        )
        assert naive.is_error is True
        out_of_range = await client.call_tool(
            "youtube_search_videos", {"query": "x", "max_results": 51}, raise_on_error=False
        )
        assert out_of_range.is_error is True
        bad_video = await client.call_tool(
            "youtube_get_transcript", {"video": "https://evil.example/x"}, raise_on_error=False
        )
        assert decode_error(bad_video.content[0].text)["code"] == "INVALID_VIDEO_INPUT"
        with pytest.raises(Exception):  # noqa: B017 - raise_on_error=True default
            await client.call_tool("youtube_get_transcript", {"video": "https://evil.example/x"})


async def test_transcript_pages_and_language_selection(
    server: FastMCP[Any], fake_provider: FakeProvider
) -> None:
    async with Client(server) as client:
        listing = await client.call_tool(
            "youtube_list_transcripts", {"video": f"https://youtu.be/{VIDEO_ID}"}
        )
        assert listing.structured_content is not None
        assert [t["language_code"] for t in listing.structured_content["tracks"]] == [
            "en",
            "en",
            "uk",
        ]
        page = await client.call_tool(
            "youtube_get_transcript", {"video": VIDEO_ID, "languages": ["uk"], "limit": 2}
        )
        data = page.structured_content
        assert data is not None
        assert data["language_code"] == "uk" and data["is_generated"] is True
        assert data["selection"] == "requested_language"
        assert data["returned_segments"] == 2 and data["next_offset"] == 2
        assert data["segments"][0]["timestamp_url"].endswith("&t=0s")
        text = await client.call_tool(
            "youtube_get_transcript",
            {"video": VIDEO_ID, "languages": ["uk"], "format": "text", "offset": 2, "limit": 10},
        )
        assert text.structured_content is not None
        assert text.structured_content["text"] == "word 2\nword 3\nword 4"
        assert text.structured_content["cache_hit"] is True
        assert fake_provider.fetch_calls == 1
        missing = await client.call_tool(
            "youtube_get_transcript", {"video": VIDEO_ID, "languages": ["fr"]}, raise_on_error=False
        )
        assert decode_error(missing.content[0].text)["code"] == "NO_MATCHING_TRANSCRIPT"


async def test_long_transcript_recovered_over_sequential_pages(
    mock_http: httpx.AsyncClient,
) -> None:
    provider = FakeProvider(segments=make_segments(400, text="a-fairly-long-caption-line"))
    settings = make_settings(max_response_bytes=8000)
    server = create_server(
        settings, transcript_provider=provider, search_client=make_search_client(mock_http)
    )
    seen: list[int] = []
    offset = 0
    async with Client(server) as client:
        for _ in range(500):
            result = await client.call_tool(
                "youtube_get_transcript", {"video": VIDEO_ID, "offset": offset, "limit": 500}
            )
            data = result.structured_content
            assert data is not None
            assert data["returned_segments"] > 0
            seen.extend(s["index"] for s in data["segments"])
            if not data["has_more"]:
                break
            assert data["next_offset"] > offset
            offset = data["next_offset"]
        else:
            pytest.fail("pagination did not terminate")
    assert seen == list(range(400))
    assert provider.fetch_calls == 1


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (TubeTraceError(ErrorCode.TRANSCRIPTS_DISABLED, "disabled"), "TRANSCRIPTS_DISABLED"),
        (TubeTraceError(ErrorCode.VIDEO_UNAVAILABLE, "gone"), "VIDEO_UNAVAILABLE"),
        (TubeTraceError(ErrorCode.UPSTREAM_BLOCKED, "blocked"), "UPSTREAM_BLOCKED"),
        (TubeTraceError(ErrorCode.UPSTREAM_TIMEOUT, "slow", retryable=True), "UPSTREAM_TIMEOUT"),
    ],
)
async def test_provider_errors_are_distinct_tool_errors(
    mock_http: httpx.AsyncClient, error: TubeTraceError, expected: str
) -> None:
    provider = FakeProvider(error=error)
    server = create_server(
        make_settings(), transcript_provider=provider, search_client=make_search_client(mock_http)
    )
    async with Client(server) as client:
        result = await client.call_tool(
            "youtube_get_transcript", {"video": VIDEO_ID}, raise_on_error=False
        )
        assert result.is_error is True
        payload = decode_error(result.content[0].text)
        assert payload["code"] == expected
        assert payload["retryable"] is error.retryable


async def test_local_rate_limit(mock_http: httpx.AsyncClient, fake_provider: FakeProvider) -> None:
    settings = make_settings(rate_limit_per_minute=60, rate_limit_burst=2)
    server = create_server(
        settings, transcript_provider=fake_provider, search_client=make_search_client(mock_http)
    )
    async with Client(server) as client:
        await client.call_tool("youtube_list_transcripts", {"video": VIDEO_ID})
        await client.call_tool("youtube_list_transcripts", {"video": VIDEO_ID})
        limited = await client.call_tool(
            "youtube_list_transcripts", {"video": VIDEO_ID}, raise_on_error=False
        )
        assert limited.is_error is True
        payload = decode_error(limited.content[0].text)
        assert payload["code"] == "RATE_LIMITED"
        assert payload["retryable"] is True
        assert payload["details"]["retry_after_seconds"] > 0


async def test_tool_time_budget(mock_http: httpx.AsyncClient) -> None:
    import asyncio

    class SlowProvider(FakeProvider):
        async def list_transcripts(self, video_id: str) -> list[Any]:
            await asyncio.sleep(5)
            return []

    settings = make_settings(tool_timeout_seconds=0.05)
    server = create_server(
        settings, transcript_provider=SlowProvider(), search_client=make_search_client(mock_http)
    )
    async with Client(server) as client:
        result = await client.call_tool(
            "youtube_list_transcripts", {"video": VIDEO_ID}, raise_on_error=False
        )
        assert result.is_error is True
        assert decode_error(result.content[0].text)["code"] == "UPSTREAM_TIMEOUT"
