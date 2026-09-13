from __future__ import annotations

from typing import Any

import pytest

from tests.conftest import VIDEO_ID, FakeProvider, fixed_now, make_segments
from tubetrace_mcp.cache import TTLCache
from tubetrace_mcp.errors import ErrorCode, TubeTraceError
from tubetrace_mcp.schemas import GetTranscriptRequest, TranscriptFormat
from tubetrace_mcp.services.transcript_service import TranscriptService


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def make_service(
    provider: FakeProvider,
    *,
    clock: Clock | None = None,
    ttl: float = 3600,
    max_bytes: int = 200_000,
) -> TranscriptService:
    cache: TTLCache[Any] = TTLCache(max_entries=100, max_bytes=10_000_000, clock=clock or Clock())
    return TranscriptService(
        provider=provider, cache=cache, ttl_seconds=ttl, max_response_bytes=max_bytes, now=fixed_now
    )


async def test_pages_reuse_cached_full_transcript() -> None:
    provider = FakeProvider(segments=make_segments(30))
    service = make_service(provider)
    first = await service.get_transcript(GetTranscriptRequest(video=VIDEO_ID, limit=10))
    assert first.cache_hit is False
    assert first.next_offset == 10
    second = await service.get_transcript(GetTranscriptRequest(video=VIDEO_ID, limit=10, offset=10))
    third = await service.get_transcript(
        GetTranscriptRequest(video=f"https://youtu.be/{VIDEO_ID}", limit=10, offset=20)
    )
    assert second.cache_hit is True and third.cache_hit is True
    assert third.has_more is False
    assert provider.fetch_calls == 1
    assert second.retrieved_at == first.retrieved_at


async def test_different_language_specs_are_separate_cache_keys_but_share_track_data() -> None:
    provider = FakeProvider(segments=make_segments(3))
    service = make_service(provider)
    await service.get_transcript(GetTranscriptRequest(video=VIDEO_ID))
    page = await service.get_transcript(GetTranscriptRequest(video=VIDEO_ID, languages=["en"]))
    assert provider.fetch_calls == 2  # new spec must be resolved upstream once
    assert page.cache_hit is False
    again = await service.get_transcript(
        GetTranscriptRequest(video=VIDEO_ID, languages=["en"], offset=1)
    )
    assert again.cache_hit is True
    assert provider.fetch_calls == 2


async def test_prefer_manual_is_part_of_cache_key_and_selection_reported() -> None:
    provider = FakeProvider(segments=make_segments(2))
    service = make_service(provider)
    manual = await service.get_transcript(GetTranscriptRequest(video=VIDEO_ID, languages=["en"]))
    generated = await service.get_transcript(
        GetTranscriptRequest(video=VIDEO_ID, languages=["en"], prefer_manual=False)
    )
    assert manual.is_generated is False and manual.selection == "requested_language"
    assert generated.is_generated is True
    default = await service.get_transcript(GetTranscriptRequest(video=VIDEO_ID))
    assert default.selection == "default_policy"
    assert default.language_code == "en" and default.is_generated is False


async def test_errors_are_not_cached() -> None:
    provider = FakeProvider(
        error=TubeTraceError(ErrorCode.UPSTREAM_TIMEOUT, "slow", retryable=True)
    )
    service = make_service(provider)
    with pytest.raises(TubeTraceError):
        await service.get_transcript(GetTranscriptRequest(video=VIDEO_ID))
    provider.error = None
    page = await service.get_transcript(GetTranscriptRequest(video=VIDEO_ID))
    assert page.cache_hit is False
    assert provider.fetch_calls == 2


async def test_ttl_expiry_refetches() -> None:
    clock = Clock()
    provider = FakeProvider(segments=make_segments(2))
    service = make_service(provider, clock=clock, ttl=100)
    await service.get_transcript(GetTranscriptRequest(video=VIDEO_ID))
    clock.now = 99
    assert (await service.get_transcript(GetTranscriptRequest(video=VIDEO_ID))).cache_hit is True
    clock.now = 101
    assert (await service.get_transcript(GetTranscriptRequest(video=VIDEO_ID))).cache_hit is False
    assert provider.fetch_calls == 2


async def test_list_transcripts_cached_and_no_text_download() -> None:
    provider = FakeProvider()
    service = make_service(provider)
    first = await service.list_transcripts(VIDEO_ID)
    second = await service.list_transcripts(f"https://www.youtube.com/watch?v={VIDEO_ID}")
    assert [t.language_code for t in first.tracks] == ["en", "en", "uk"]
    assert first.cache_hit is False and second.cache_hit is True
    assert provider.list_calls == 1 and provider.fetch_calls == 0
    assert "not necessarily the video's original language" in first.default_selection_policy


async def test_no_matching_language_error_from_service() -> None:
    provider = FakeProvider()
    service = make_service(provider)
    with pytest.raises(TubeTraceError) as info:
        await service.get_transcript(GetTranscriptRequest(video=VIDEO_ID, languages=["fr"]))
    assert info.value.code == ErrorCode.NO_MATCHING_TRANSCRIPT
    assert {a["language_code"] for a in info.value.details["available"]} == {"en", "uk"}


async def test_text_format_page_and_invalid_video() -> None:
    provider = FakeProvider(segments=make_segments(4))
    service = make_service(provider)
    page = await service.get_transcript(
        GetTranscriptRequest(video=VIDEO_ID, format=TranscriptFormat.TEXT, limit=2)
    )
    assert page.text == "word 0\nword 1"
    assert page.segments is None
    with pytest.raises(TubeTraceError) as info:
        await service.get_transcript(
            GetTranscriptRequest(video="https://evil.example/watch?v=" + VIDEO_ID)
        )
    assert info.value.code == ErrorCode.INVALID_VIDEO_INPUT
