from __future__ import annotations

import pytest

from tubetrace_mcp.errors import ErrorCode, TubeTraceError
from tubetrace_mcp.providers.base import RawSegment, RawTranscript
from tubetrace_mcp.schemas import GetTranscriptRequest, TranscriptFormat
from tubetrace_mcp.services.pagination import build_page, segment_overlaps

VID = "dQw4w9WgXcQ"


def transcript(segments: list[RawSegment]) -> RawTranscript:
    return RawTranscript(
        video_id=VID,
        language="English",
        language_code="en",
        is_generated=False,
        segments=tuple(segments),
    )


def make(count: int, text: str = "t") -> RawTranscript:
    return transcript(
        [RawSegment(text=f"{text}{i}", start=i * 2.0, duration=2.0) for i in range(count)]
    )


def request(**kwargs: object) -> GetTranscriptRequest:
    return GetTranscriptRequest(video=VID, **kwargs)


def test_segment_overlap_semantics() -> None:
    seg = RawSegment(text="x", start=10.0, duration=5.0)  # [10, 15)
    assert segment_overlaps(seg, None, None)
    assert segment_overlaps(seg, 14.9, None)
    assert not segment_overlaps(seg, 15.0, None)
    assert segment_overlaps(seg, None, 10.1)
    assert not segment_overlaps(seg, None, 10.0)
    assert segment_overlaps(seg, 12.0, 13.0)
    point = RawSegment(text="p", start=10.0, duration=0.0)
    assert segment_overlaps(point, 10.0, 11.0)
    assert not segment_overlaps(point, 10.5, 11.0)
    assert not segment_overlaps(point, 9.0, 10.0)


def test_basic_pagination_and_segment_fields() -> None:
    page = build_page(make(5), request(limit=2), max_bytes=100_000)
    assert page.total_segments == 5
    assert page.matched_segments == 5
    assert page.returned_segments == 2
    assert page.has_more is True
    assert page.next_offset == 2
    assert page.segments is not None
    first = page.segments[0]
    assert first.index == 0
    assert first.start_seconds == 0.0
    assert first.duration_seconds == 2.0
    assert first.end_seconds == 2.0
    assert first.timestamp_url == f"https://www.youtube.com/watch?v={VID}&t=0s"

    last = build_page(make(5), request(limit=2, offset=4), max_bytes=100_000)
    assert last.returned_segments == 1
    assert last.has_more is False
    assert last.next_offset is None


def test_offset_beyond_end_returns_empty_page_without_next_offset() -> None:
    page = build_page(make(3), request(offset=10), max_bytes=100_000)
    assert page.returned_segments == 0
    assert page.has_more is False
    assert page.next_offset is None
    assert page.segments == []


def test_time_range_then_offset_limit_keeps_absolute_indices() -> None:
    page = build_page(
        make(10), request(start_seconds=5.0, end_seconds=13.0, limit=2), max_bytes=100_000
    )
    # segments [4,6),[6,8),[8,10),[10,12),[12,14) overlap -> indices 2..6
    assert page.matched_segments == 5
    assert page.segments is not None
    assert [s.index for s in page.segments] == [2, 3]
    assert page.next_offset == 2
    page2 = build_page(
        make(10), request(start_seconds=5.0, end_seconds=13.0, limit=2, offset=2), max_bytes=100_000
    )
    assert page2.segments is not None
    assert [s.index for s in page2.segments] == [4, 5]
    assert page2.segments[0].start_seconds == 8.0  # original timings are preserved


def test_text_format_returns_only_page_text() -> None:
    page = build_page(
        make(4), request(format=TranscriptFormat.TEXT, limit=2, offset=1), max_bytes=100_000
    )
    assert page.segments is None
    assert page.text == "t1\nt2"
    assert page.returned_segments == 2
    assert page.next_offset == 3


def test_size_limit_truncates_page_and_next_offset_follows_actual_segments() -> None:
    long = transcript([RawSegment(text="x" * 300, start=i * 1.0, duration=1.0) for i in range(50)])
    page = build_page(long, request(limit=50), max_bytes=1024 + 1600)  # room for ~4 segments
    assert page.truncated_by_size_limit is True
    assert 0 < page.returned_segments < 50
    assert page.next_offset == page.returned_segments
    assert page.has_more is True


def test_sequential_pages_recover_the_whole_transcript_without_loss() -> None:
    long = transcript(
        [
            RawSegment(text=f"segment-{i}-" + "y" * 120, start=i * 1.5, duration=1.5)
            for i in range(200)
        ]
    )
    collected: list[int] = []
    offset = 0
    for _ in range(1000):  # hard bound: no infinite loop
        page = build_page(long, request(limit=500, offset=offset), max_bytes=6000)
        assert page.segments is not None
        collected.extend(s.index for s in page.segments)
        if not page.has_more:
            break
        assert page.next_offset is not None and page.next_offset > offset
        offset = page.next_offset
    assert collected == list(range(200))

    texts: list[str] = []
    offset = 0
    while True:
        page = build_page(
            long, request(limit=500, offset=offset, format=TranscriptFormat.TEXT), max_bytes=6000
        )
        assert page.text is not None
        texts.append(page.text)
        if not page.has_more:
            break
        assert page.next_offset is not None
        offset = page.next_offset
    assert "\n".join(texts) == "\n".join(s.text for s in long.segments)


def test_single_oversized_segment_is_an_explicit_error() -> None:
    huge = transcript([RawSegment(text="z" * 5000, start=0.0, duration=1.0)])
    with pytest.raises(TubeTraceError) as info:
        build_page(huge, request(), max_bytes=4096)
    assert info.value.code == ErrorCode.RESPONSE_TOO_LARGE
    assert info.value.details["segment_index"] == 0


def test_request_validation() -> None:
    with pytest.raises(ValueError):
        GetTranscriptRequest(video=VID, start_seconds=10, end_seconds=5)
    with pytest.raises(ValueError):
        GetTranscriptRequest(video=VID, languages=["not a code!"])
    with pytest.raises(ValueError):
        GetTranscriptRequest(video=VID, languages=[])
    req = GetTranscriptRequest(video=VID, languages=["EN", "en", "uk"])
    assert req.languages == ["en", "uk"]
