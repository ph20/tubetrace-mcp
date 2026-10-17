from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from tests.conftest import (
    VIDEO_ID,
    FakeProvider,
    GoogleMock,
    NoSleep,
    fixed_now,
    google_error,
    make_search_client,
    make_segments,
    make_settings,
)
from tubetrace_mcp.cache import TTLCache
from tubetrace_mcp.errors import ErrorCode, TubeTraceError
from tubetrace_mcp.llm_client import LLMAnalysisOutput, LLMCitation, TranscriptLLMClient
from tubetrace_mcp.logging_config import SecretRedactor
from tubetrace_mcp.schemas import (
    AnalyzeTranscriptRequest,
    GetChannelRequest,
    GetPlaylistRequest,
    GetVideoRequest,
    ListChannelPlaylistsRequest,
    LLMUsage,
)
from tubetrace_mcp.services.analysis_service import TranscriptAnalysisService
from tubetrace_mcp.services.search_service import SearchService
from tubetrace_mcp.services.transcript_service import TranscriptService

CHANNEL_ID = "UCuAXFkgsw1L7xaCfnd5JJOw"
UPLOADS_ID = "UUuAXFkgsw1L7xaCfnd5JJOw"
PLAYLIST_ID = "PL1234567890abcdef"


def video_payload(video_id: str = VIDEO_ID) -> dict[str, Any]:
    return {
        "items": [
            {
                "id": video_id,
                "snippet": {
                    "publishedAt": "2009-10-25T06:57:33Z",
                    "channelId": CHANNEL_ID,
                    "title": "Never Gonna &amp; Give You Up",
                    "description": "Full description line 1\nLine 2",
                    "thumbnails": {"maxres": {"url": "https://i.ytimg.com/vi/dQw/maxres.jpg"}},
                    "channelTitle": "Rick &amp; Co",
                    "tags": ["music", "pop &amp; 80s"],
                    "categoryId": "10",
                    "liveBroadcastContent": "none",
                    "defaultLanguage": "en",
                    "defaultAudioLanguage": "en-GB",
                },
                "contentDetails": {
                    "duration": "PT3M33S",
                    "definition": "hd",
                    "caption": "true",
                },
                "statistics": {
                    "viewCount": "1500000000",
                    "likeCount": "17000000",
                    "commentCount": "2300000",
                },
            }
        ]
    }


def channel_payload(channel_id: str = CHANNEL_ID) -> dict[str, Any]:
    return {
        "items": [
            {
                "id": channel_id,
                "snippet": {
                    "title": "Official Channel &amp; Team",
                    "description": "Channel bio",
                    "customUrl": "@officialchannel",
                    "publishedAt": "2010-01-01T00:00:00Z",
                    "thumbnails": {"high": {"url": "https://yt3.ggpht.com/ch.jpg"}},
                    "country": "GB",
                },
                "contentDetails": {"relatedPlaylists": {"uploads": UPLOADS_ID}},
                "statistics": {
                    "viewCount": "2000000000",
                    "subscriberCount": "4000000",
                    "videoCount": "150",
                },
            }
        ]
    }


def playlist_payload(playlist_id: str = PLAYLIST_ID) -> dict[str, Any]:
    return {
        "items": [
            {
                "id": playlist_id,
                "snippet": {
                    "publishedAt": "2020-02-02T00:00:00Z",
                    "channelId": CHANNEL_ID,
                    "title": "Best Videos &amp; More",
                    "description": "Curated list",
                    "thumbnails": {"high": {"url": "https://i.ytimg.com/vi/pl/hq.jpg"}},
                    "channelTitle": "Rick &amp; Co",
                },
                "contentDetails": {"itemCount": 42},
            }
        ]
    }


def playlist_items_payload(
    count: int = 2,
    *,
    next_page_token: str | None = "NEXT_PL",  # noqa: S107 - not a secret
) -> dict[str, Any]:
    items = []
    for idx in range(count):
        items.append(
            {
                "snippet": {
                    "publishedAt": "2024-01-02T00:00:00Z",
                    "channelId": CHANNEL_ID,
                    "title": f"Item &amp; {idx}",
                    "description": f"Item desc {idx}",
                    "thumbnails": {"high": {"url": f"https://i.ytimg.com/vi/v{idx}/hq.jpg"}},
                    "channelTitle": "Rick &amp; Co",
                    "position": idx,
                    "resourceId": {"kind": "youtube#video", "videoId": f"vid{idx:08d}"},
                    "videoOwnerChannelTitle": "Owner &amp; Co",
                    "videoOwnerChannelId": CHANNEL_ID,
                },
                "contentDetails": {
                    "videoId": f"vid{idx:08d}",
                    "videoPublishedAt": "2024-01-01T12:00:00Z",
                },
                "status": {"privacyStatus": "public"},
            }
        )
    data: dict[str, Any] = {
        "items": items,
        "pageInfo": {"totalResults": 42, "resultsPerPage": count},
    }
    if next_page_token:
        data["nextPageToken"] = next_page_token
    return data


async def test_get_video_with_and_without_playlist(
    mock_http: httpx.AsyncClient, google_mock: GoogleMock
) -> None:
    cache: TTLCache[Any] = TTLCache(max_entries=64, max_bytes=1024 * 1024)
    service = SearchService(client=make_search_client(mock_http), cache=cache, ttl_seconds=300)

    google_mock.queue(
        httpx.Response(200, json=video_payload()),
        httpx.Response(200, json=playlist_payload()),
    )
    url = f"https://www.youtube.com/watch?v={VIDEO_ID}&list={PLAYLIST_ID}"
    res = await service.get_video(GetVideoRequest(video=url))
    assert res.video_id == VIDEO_ID
    assert res.title == "Never Gonna & Give You Up"
    assert res.channel_id == CHANNEL_ID
    assert res.channel_url == f"https://www.youtube.com/channel/{CHANNEL_ID}"
    assert res.duration_iso == "PT3M33S"
    assert res.duration_seconds == 213
    assert res.has_captions is True
    assert res.view_count == 1500000000
    assert res.like_count == 17000000
    assert res.comment_count == 2300000
    assert res.tags == ["music", "pop & 80s"]
    assert res.playlist is not None
    assert res.playlist.playlist_id == PLAYLIST_ID
    assert res.playlist.title == "Best Videos & More"
    assert res.playlist.item_count == 42
    assert res.cache_hit is False
    assert len(google_mock.requests) == 2

    # Second call hits both video and playlist caches
    cached_res = await service.get_video(GetVideoRequest(video=url))
    assert cached_res.cache_hit is True
    assert len(google_mock.requests) == 2


async def test_get_video_not_found_and_playlist_not_found_handling(
    mock_http: httpx.AsyncClient, google_mock: GoogleMock
) -> None:
    cache: TTLCache[Any] = TTLCache(max_entries=64, max_bytes=1024 * 1024)
    service = SearchService(client=make_search_client(mock_http), cache=cache, ttl_seconds=300)

    google_mock.queue(httpx.Response(200, json={"items": []}))
    with pytest.raises(TubeTraceError) as info:
        await service.get_video(GetVideoRequest(video=VIDEO_ID))
    assert info.value.code == ErrorCode.VIDEO_NOT_FOUND

    # Implicit playlist in URL that is private/missing falls back to returning video metadata
    google_mock.queue(
        httpx.Response(200, json=video_payload()),
        httpx.Response(200, json={"items": []}),
    )
    fallback = await service.get_video(
        GetVideoRequest(video=f"https://www.youtube.com/watch?v={VIDEO_ID}&list={PLAYLIST_ID}")
    )
    assert fallback.video_id == VIDEO_ID
    assert fallback.playlist is None

    # Explicit playlist parameter that is missing raises PLAYLIST_NOT_FOUND
    google_mock.queue(httpx.Response(200, json={"items": []}))
    with pytest.raises(TubeTraceError) as pl_err:
        await service.get_video(GetVideoRequest(video=VIDEO_ID, playlist=PLAYLIST_ID))
    assert pl_err.value.code == ErrorCode.PLAYLIST_NOT_FOUND


async def test_get_channel_and_pagination_reuses_cached_channel_metadata(
    mock_http: httpx.AsyncClient, google_mock: GoogleMock
) -> None:
    cache: TTLCache[Any] = TTLCache(max_entries=64, max_bytes=1024 * 1024)
    service = SearchService(client=make_search_client(mock_http), cache=cache, ttl_seconds=300)

    google_mock.queue(
        httpx.Response(200, json=channel_payload()),
        httpx.Response(200, json=playlist_items_payload(2, next_page_token="PAGE_2")),
        httpx.Response(200, json=playlist_items_payload(1, next_page_token=None)),
    )
    page1 = await service.get_channel(
        GetChannelRequest(channel="@officialchannel", include_videos=True, max_results=2)
    )
    assert page1.channel_id == CHANNEL_ID
    assert page1.title == "Official Channel & Team"
    assert page1.custom_url == "@officialchannel"
    assert page1.subscriber_count == 4000000
    assert page1.uploads_playlist_id == UPLOADS_ID
    assert page1.videos is not None and len(page1.videos) == 2
    assert page1.next_page_token == "PAGE_2"
    assert len(google_mock.requests) == 2

    # Next page by channel ID reuses the cached channel metadata -> only 1 upstream call!
    page2 = await service.get_channel(
        GetChannelRequest(
            channel=CHANNEL_ID, include_videos=True, max_results=2, page_token="PAGE_2"
        )
    )
    assert page2.videos is not None and len(page2.videos) == 1
    assert page2.next_page_token is None
    assert len(google_mock.requests) == 3


async def test_get_playlist_and_list_channel_playlists(
    mock_http: httpx.AsyncClient, google_mock: GoogleMock
) -> None:
    cache: TTLCache[Any] = TTLCache(max_entries=64, max_bytes=1024 * 1024)
    service = SearchService(client=make_search_client(mock_http), cache=cache, ttl_seconds=300)

    google_mock.queue(
        httpx.Response(200, json=playlist_payload()),
        httpx.Response(200, json=playlist_items_payload(2, next_page_token=None)),
        httpx.Response(200, json=playlist_payload()),
    )
    pl = await service.get_playlist(GetPlaylistRequest(playlist=PLAYLIST_ID, max_results=2))
    assert pl.playlist_id == PLAYLIST_ID
    assert pl.item_count == 42
    assert pl.items is not None and len(pl.items) == 2
    assert pl.items[0].video_id == "vid00000000"

    ch_pls = await service.list_channel_playlists(
        ListChannelPlaylistsRequest(channel=CHANNEL_ID, max_results=10)
    )
    assert ch_pls.channel_id == CHANNEL_ID
    assert len(ch_pls.playlists) == 1
    assert ch_pls.playlists[0].playlist_id == PLAYLIST_ID

    # 404 playlistNotFound on playlistItems maps to PLAYLIST_NOT_FOUND
    google_mock.queue(google_error(404, "playlistNotFound"))
    with pytest.raises(TubeTraceError) as err_info:
        await service._client.list_playlist_items(PLAYLIST_ID)
    assert err_info.value.code == ErrorCode.PLAYLIST_NOT_FOUND


async def test_gemini_and_openai_llm_client_and_retries() -> None:
    requests_seen: list[httpx.Request] = []
    responses: list[httpx.Response] = [
        httpx.Response(
            429, json={"error": {"message": "rate limit"}}, headers={"Retry-After": "1"}
        ),
        httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {"thought": True, "text": "internal thought"},
                                {
                                    "text": json.dumps(
                                        {
                                            "answer": "Gemini answer",
                                            "citations": [
                                                {"start_seconds": 12.5, "label": "Intro topic"}
                                            ],
                                        }
                                    )
                                },
                            ]
                        }
                    }
                ],
                "usageMetadata": {"promptTokenCount": 500, "candidatesTokenCount": 40},
            },
        ),
    ]

    def handler(req: httpx.Request) -> httpx.Response:
        requests_seen.append(req)
        return responses.pop(0)

    sleep = NoSleep()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        gemini = TranscriptLLMClient(
            http=http,
            provider="gemini",
            api_key="test-gemini-secret-key",
            model="gemini-3-flash-preview",
            base_url="https://generativelanguage.googleapis.com/v1beta",
            sleep=sleep,
            rng=lambda: 0.0,
        )
        out = await gemini.analyze(
            prompt="What is discussed?",
            formatted_transcript="[0s] hello\n[12.5s] intro",
            video_id=VIDEO_ID,
            language_code="en",
            output_language="Ukrainian",
        )
    assert out.answer == "Gemini answer"
    assert len(out.citations) == 1
    assert out.citations[0].start_seconds == 12.5
    assert out.usage == LLMUsage(input_tokens=500, output_tokens=40)
    assert len(requests_seen) == 2
    assert sleep.calls == [1.0]
    sent = requests_seen[0]
    assert sent.headers["x-goog-api-key"] == "test-gemini-secret-key"
    assert "test-gemini-secret-key" not in str(sent.url)
    body = json.loads(sent.content.decode("utf-8"))
    assert "<untrusted_transcript>" in body["contents"][0]["parts"][0]["text"]

    # OpenAI-compatible client with fenced JSON
    responses.append(
        httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": '```json\n{"answer": "OpenAI answer", "citations": []}\n```'
                        }
                    }
                ],
                "usage": {"prompt_tokens": 300, "completion_tokens": 20},
            },
        )
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        openai_client = TranscriptLLMClient(
            http=http,
            provider="openai",
            api_key="sk-test-openai-secret",
            model="gpt-4.1-mini",
            base_url="https://api.openai.com/v1",
            sleep=sleep,
            rng=lambda: 0.0,
        )
        out2 = await openai_client.analyze(
            prompt="Summarize",
            formatted_transcript="[0s] hi",
            video_id=VIDEO_ID,
            language_code="en",
        )
    assert out2.answer == "OpenAI answer"
    assert out2.usage == LLMUsage(input_tokens=300, output_tokens=20)
    assert requests_seen[-1].headers["Authorization"] == "Bearer sk-test-openai-secret"


async def test_analysis_service_time_slice_and_size_guard() -> None:
    provider = FakeProvider(segments=make_segments(10, text="segment text", duration=10.0))
    cache: TTLCache[Any] = TTLCache(max_entries=64, max_bytes=1024 * 1024)
    transcript_service = TranscriptService(
        provider=provider, cache=cache, ttl_seconds=3600, max_response_bytes=200_000, now=fixed_now
    )

    class FakeLLM:
        provider = "gemini"
        model = "gemini-3-flash-preview"

        def __init__(self) -> None:
            self.last_transcript = ""

        async def analyze(
            self,
            *,
            prompt: str,
            formatted_transcript: str,
            video_id: str,
            language_code: str,
            output_language: str | None = None,
        ) -> LLMAnalysisOutput:
            self.last_transcript = formatted_transcript
            return LLMAnalysisOutput(
                answer=f"Answered: {prompt}",
                citations=[LLMCitation(start_seconds=20.0, label="Segment 2")],
                usage=LLMUsage(input_tokens=100, output_tokens=25),
            )

    fake_llm = FakeLLM()
    analysis = TranscriptAnalysisService(
        transcript_service=transcript_service,
        llm_client=fake_llm,
        max_input_chars=5000,
        now=fixed_now,
    )

    result = await analysis.analyze(
        AnalyzeTranscriptRequest(
            video=VIDEO_ID,
            prompt="What happens between 15s and 35s?",
            start_seconds=15.0,
            end_seconds=35.0,
        )
    )
    assert result.total_segments == 10
    assert result.analyzed_segments == 3  # segments at 10-20s, 20-30s, 30-40s
    assert result.transcript_cache_hit is False
    assert result.citations[0].timestamp_url == f"https://www.youtube.com/watch?v={VIDEO_ID}&t=20s"
    assert "[10s] segment text 1" in fake_llm.last_transcript

    # Second call reuses the cached transcript
    result2 = await analysis.analyze(
        AnalyzeTranscriptRequest(video=VIDEO_ID, prompt="Full summary")
    )
    assert result2.transcript_cache_hit is True
    assert provider.fetch_calls == 1

    # Exceeding max_input_chars raises TRANSCRIPT_TOO_LARGE
    tiny_analysis = TranscriptAnalysisService(
        transcript_service=transcript_service,
        llm_client=fake_llm,
        max_input_chars=10,
        now=fixed_now,
    )
    with pytest.raises(TubeTraceError) as err:
        await tiny_analysis.analyze(AnalyzeTranscriptRequest(video=VIDEO_ID, prompt="Too big"))
    assert err.value.code == ErrorCode.TRANSCRIPT_TOO_LARGE


def test_llm_settings_validation_and_redaction() -> None:
    s_default = make_settings()
    assert s_default.llm_configured is False
    assert s_default.effective_llm_model == "gemini-3-flash-preview"

    s_gemini = make_settings(transcript_llm_api_key="AQ.Ab8RN6L_test_secret_key_value")
    assert s_gemini.llm_configured is True
    redactor = SecretRedactor(s_gemini.redaction_secrets())
    assert "AQ.Ab8RN6L_test_secret_key_value" not in redactor.redact(
        "key=AQ.Ab8RN6L_test_secret_key_value"
    )

    # OpenAI provider with key but no model fails closed
    with pytest.raises(ValidationError, match="TRANSCRIPT_LLM_MODEL"):
        make_settings(
            transcript_llm_provider="openai",
            transcript_llm_api_key="sk-test-1234567890",
            transcript_llm_model=None,
        )
