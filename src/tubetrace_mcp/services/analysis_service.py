"""Server-side single-pass transcript preprocessing and Q&A service."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from ..errors import ErrorCode, TubeTraceError
from ..llm_client import TranscriptLLM
from ..providers.base import RawSegment
from ..schemas import AnalyzeTranscriptRequest, AnalyzeTranscriptResult, TranscriptCitation
from ..video_input import canonical_video_url, timestamp_url
from .pagination import segment_overlaps
from .transcript_service import TranscriptService


def format_transcript_for_llm(segments: list[RawSegment]) -> str:
    """Format timed transcript segments as ``[125.4s] text`` lines."""
    return "\n".join(f"[{round(seg.start, 2):g}s] {seg.text}" for seg in segments)


class TranscriptAnalysisService:
    """Single-pass transcript Q&A backed by TranscriptService and TranscriptLLM."""

    def __init__(
        self,
        *,
        transcript_service: TranscriptService,
        llm_client: TranscriptLLM,
        max_input_chars: int = 2_000_000,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._transcript_service = transcript_service
        self._llm_client = llm_client
        self._max_input_chars = max_input_chars
        self._now = now

    @property
    def llm_provider(self) -> str:
        return self._llm_client.provider

    @property
    def llm_model(self) -> str:
        return self._llm_client.model

    async def analyze(self, request: AnalyzeTranscriptRequest) -> AnalyzeTranscriptResult:
        resolved = await self._transcript_service.resolve_transcript(
            video=request.video,
            languages=request.languages,
            prefer_manual=request.prefer_manual,
        )
        transcript = resolved.transcript
        matched = [
            seg
            for seg in transcript.segments
            if segment_overlaps(seg, request.start_seconds, request.end_seconds)
        ]
        if not matched:
            raise TubeTraceError(
                ErrorCode.INVALID_ARGUMENT,
                "No transcript segments overlap the requested time range.",
                details={
                    "start_seconds": request.start_seconds,
                    "end_seconds": request.end_seconds,
                    "total_segments": len(transcript.segments),
                },
            )

        formatted = format_transcript_for_llm(matched)
        char_count = len(formatted)
        if char_count > self._max_input_chars:
            raise TubeTraceError(
                ErrorCode.TRANSCRIPT_TOO_LARGE,
                "The selected transcript exceeds the configured LLM input limit; "
                "narrow the range with start_seconds and end_seconds.",
                details={
                    "transcript_characters": char_count,
                    "max_input_chars": self._max_input_chars,
                },
            )

        llm_output = await self._llm_client.analyze(
            prompt=request.prompt,
            formatted_transcript=formatted,
            video_id=resolved.video_id,
            language_code=transcript.language_code,
            output_language=request.output_language,
        )
        citations = [
            TranscriptCitation(
                start_seconds=round(item.start_seconds, 3),
                timestamp_url=timestamp_url(resolved.video_id, item.start_seconds),
                label=item.label,
            )
            for item in llm_output.citations
        ]
        return AnalyzeTranscriptResult(
            video_id=resolved.video_id,
            video_url=canonical_video_url(resolved.video_id),
            language=transcript.language,
            language_code=transcript.language_code,
            is_generated=transcript.is_generated,
            selection=resolved.selection,
            transcript_provider=self._transcript_service.provider_name,
            transcript_cache_hit=resolved.cache_hit,
            llm_provider=self._llm_client.provider,
            llm_model=self._llm_client.model,
            total_segments=len(transcript.segments),
            analyzed_segments=len(matched),
            transcript_characters=char_count,
            start_seconds=request.start_seconds,
            end_seconds=request.end_seconds,
            answer=llm_output.answer,
            citations=citations,
            usage=llm_output.usage,
            retrieved_at=self._now(),
        )
