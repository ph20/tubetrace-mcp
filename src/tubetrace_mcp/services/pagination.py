"""Time-range filtering, offset/limit pagination and response-size trimming.

Semantics:

* ``index`` of a segment is its zero-based position in the **full** transcript
  and never changes with filters.
* A time range keeps segments whose ``[start, start + duration)`` interval
  intersects ``[start_seconds, end_seconds)``; zero-length segments are kept when
  their start lies inside the range. Segment timings are never altered.
* ``offset``/``limit`` apply to the filtered ("matched") selection.
* If the serialized page would exceed ``max_bytes``, the page is shortened and
  ``next_offset`` reflects the segments actually returned. A single segment that
  cannot fit on its own raises ``RESPONSE_TOO_LARGE`` instead of returning an
  empty page, so pagination can never loop forever.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..errors import ErrorCode, TubeTraceError
from ..providers.base import RawSegment, RawTranscript
from ..schemas import GetTranscriptRequest, TranscriptFormat, TranscriptSegment
from ..video_input import timestamp_url

ENVELOPE_OVERHEAD_BYTES = 1024


@dataclass(slots=True)
class PageComputation:
    total_segments: int
    matched_segments: int
    offset: int
    returned_segments: int
    next_offset: int | None
    has_more: bool
    truncated_by_size_limit: bool
    segments: list[TranscriptSegment] | None
    text: str | None


def segment_overlaps(segment: RawSegment, start: float | None, end: float | None) -> bool:
    if start is None and end is None:
        return True
    seg_start = segment.start
    seg_end = segment.start + max(segment.duration, 0.0)
    if segment.duration <= 0:
        return (start is None or seg_start >= start) and (end is None or seg_start < end)
    if start is not None and seg_end <= start:
        return False
    return not (end is not None and seg_start >= end)


def _to_segment(index: int, segment: RawSegment, video_id: str) -> TranscriptSegment:
    end = segment.start + max(segment.duration, 0.0)
    return TranscriptSegment(
        index=index,
        text=segment.text,
        start_seconds=round(segment.start, 3),
        duration_seconds=round(max(segment.duration, 0.0), 3),
        end_seconds=round(end, 3),
        timestamp_url=timestamp_url(video_id, segment.start),
    )


def build_page(
    transcript: RawTranscript, request: GetTranscriptRequest, *, max_bytes: int
) -> PageComputation:
    matched = [
        (index, segment)
        for index, segment in enumerate(transcript.segments)
        if segment_overlaps(segment, request.start_seconds, request.end_seconds)
    ]
    total = len(transcript.segments)
    matched_count = len(matched)
    offset = request.offset
    window = matched[offset : offset + request.limit]

    budget = max_bytes - ENVELOPE_OVERHEAD_BYTES
    used = 0
    truncated = False
    segments_out: list[TranscriptSegment] = []
    texts: list[str] = []
    for position, (index, raw_segment) in enumerate(window):
        if request.format is TranscriptFormat.SEGMENTS:
            model = _to_segment(index, raw_segment, transcript.video_id)
            size = len(model.model_dump_json().encode("utf-8")) + 2
        else:
            model = None
            size = len(raw_segment.text.encode("utf-8")) + 1
        if used + size > budget:
            if position == 0:
                raise TubeTraceError(
                    ErrorCode.RESPONSE_TOO_LARGE,
                    "A single transcript segment exceeds the configured response size limit; "
                    "raise MAX_RESPONSE_BYTES on the server to retrieve it.",
                    details={"segment_index": index, "segment_bytes": size, "max_bytes": max_bytes},
                )
            truncated = True
            break
        used += size
        if model is not None:
            segments_out.append(model)
        else:
            texts.append(raw_segment.text)

    returned = len(segments_out) if request.format is TranscriptFormat.SEGMENTS else len(texts)
    has_more = offset + returned < matched_count
    return PageComputation(
        total_segments=total,
        matched_segments=matched_count,
        offset=offset,
        returned_segments=returned,
        next_offset=offset + returned if has_more else None,
        has_more=has_more,
        truncated_by_size_limit=truncated,
        segments=segments_out if request.format is TranscriptFormat.SEGMENTS else None,
        text="\n".join(texts) if request.format is TranscriptFormat.TEXT else None,
    )
