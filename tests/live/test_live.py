"""Opt-in live tests against real Google / YouTube endpoints.

Enable with::

    RUN_LIVE_TESTS=1 YOUTUBE_API_KEY=... TEST_VIDEO_ID=... uv run pytest tests/live -m live

The search test spends 1 call of the daily ``search.list`` quota. Provider
blocking (UPSTREAM_BLOCKED) is reported as a failure with diagnostics, never
hidden or replaced by a mocked success.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from tubetrace_mcp.errors import ErrorCode, TubeTraceError
from tubetrace_mcp.providers.youtube_transcript_api import YouTubeTranscriptApiProvider
from tubetrace_mcp.schemas import SearchVideosRequest
from tubetrace_mcp.search_client import YouTubeSearchClient
from tubetrace_mcp.services.selection import select_track

RUN_LIVE = os.environ.get("RUN_LIVE_TESTS") == "1"
API_KEY = os.environ.get("YOUTUBE_API_KEY", "").strip()
TEST_VIDEO_ID = os.environ.get("TEST_VIDEO_ID", "").strip()

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not RUN_LIVE,
        reason="live tests are opt-in: set RUN_LIVE_TESTS=1 (plus YOUTUBE_API_KEY, TEST_VIDEO_ID)",
    ),
]


@pytest.mark.skipif(not API_KEY, reason="YOUTUBE_API_KEY is required for the live search test")
async def test_live_search_one_page() -> None:
    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as http:
        client = YouTubeSearchClient(http=http, api_key=API_KEY, max_retries=1)
        result = await client.search(
            SearchVideosRequest(query="python asyncio tutorial", max_results=3)
        )
    assert result.provider == "youtube_data_api_v3"
    assert 1 <= len(result.items) <= 3
    for item in result.items:
        assert len(item.video_id) == 11
        assert item.video_url.startswith("https://www.youtube.com/watch?v=")
        assert item.title


@pytest.mark.skipif(
    not TEST_VIDEO_ID, reason="TEST_VIDEO_ID is required for the live transcript tests"
)
async def test_live_list_and_fetch_transcript() -> None:
    executor = ThreadPoolExecutor(max_workers=1)
    provider = YouTubeTranscriptApiProvider(
        executor=executor,
        max_concurrency=1,
        queue_timeout_seconds=5,
        connect_timeout_seconds=5,
        read_timeout_seconds=15,
        max_retries=1,
        retry_budget_seconds=20,
        max_segments=50_000,
        max_bytes=8 * 1024 * 1024,
    )
    try:
        try:
            tracks = await provider.list_transcripts(TEST_VIDEO_ID)
        except TubeTraceError as err:
            if err.code == ErrorCode.UPSTREAM_BLOCKED:
                pytest.fail(f"transcript provider is BLOCKED from this network: {err.to_dict()}")
            raise
        assert tracks, "expected at least one caption track for TEST_VIDEO_ID"
        transcript = await provider.fetch_transcript(
            TEST_VIDEO_ID, lambda available: select_track(available, None, prefer_manual=True)
        )
        assert transcript.segments
        assert transcript.language_code
        first = transcript.segments[0]
        assert first.text
        assert first.duration >= 0
    finally:
        executor.shutdown(wait=True)
