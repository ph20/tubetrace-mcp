"""Provider interface for transcript retrieval.

Implementations must translate their own failures into :class:`TubeTraceError`
with the appropriate code, distinguishing missing subtitles from network
failures, blocking, rate limiting and unavailable videos.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ..schemas import TranscriptTrack


@dataclass(frozen=True, slots=True)
class RawSegment:
    text: str
    start: float
    duration: float


@dataclass(frozen=True, slots=True)
class RawTranscript:
    video_id: str
    language: str
    language_code: str
    is_generated: bool
    segments: tuple[RawSegment, ...]

    @property
    def approx_bytes(self) -> int:
        return sum(len(segment.text.encode("utf-8")) + 48 for segment in self.segments) + 256


TrackSelector = Callable[[Sequence[TranscriptTrack]], TranscriptTrack]
"""Pure function choosing one track from the available ones (raises NO_MATCHING_TRANSCRIPT)."""


@runtime_checkable
class TranscriptProvider(Protocol):
    """Minimal contract used by the transcript service."""

    @property
    def name(self) -> str:
        """Stable provider identifier reported in tool results."""

    async def list_transcripts(self, video_id: str) -> list[TranscriptTrack]:
        """Return the available caption tracks without downloading their text."""

    async def fetch_transcript(self, video_id: str, select: TrackSelector) -> RawTranscript:
        """List tracks, pick one with ``select`` and download only that track."""
