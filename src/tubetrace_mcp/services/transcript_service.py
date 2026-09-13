"""Transcript listing and paginated retrieval on top of a provider.

Caching is two-level: the full transcript of a concrete track is cached once per
``(provider, video, language_code, is_generated)``; the resolution of a request
specification (languages, prefer_manual) to a concrete track is cached
separately, so successive pages of the same transcript reuse the cached full
transcript without any upstream call. Errors are never cached.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ..cache import TTLCache
from ..providers.base import RawTranscript, TranscriptProvider
from ..schemas import (
    GetTranscriptRequest,
    TranscriptListResult,
    TranscriptPage,
    TranscriptTrack,
)
from ..video_input import canonical_video_url, parse_video_input
from .pagination import build_page
from .selection import DEFAULT_SELECTION_POLICY, select_track


@dataclass(frozen=True, slots=True)
class CachedTranscript:
    transcript: RawTranscript
    retrieved_at: datetime


@dataclass(frozen=True, slots=True)
class CachedTracks:
    tracks: tuple[TranscriptTrack, ...]
    retrieved_at: datetime


class TranscriptService:
    def __init__(
        self,
        *,
        provider: TranscriptProvider,
        cache: TTLCache[Any],
        ttl_seconds: float,
        max_response_bytes: int,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._provider = provider
        self._cache = cache
        self._ttl = ttl_seconds
        self._max_response_bytes = max_response_bytes
        self._now = now

    @property
    def provider_name(self) -> str:
        return self._provider.name

    async def list_transcripts(self, video: str) -> TranscriptListResult:
        video_id = parse_video_input(video)
        key = f"tracks:{self._provider.name}:{video_id}"
        cached = self._cache.get(key)
        if isinstance(cached, CachedTracks):
            return TranscriptListResult(
                video_id=video_id,
                video_url=canonical_video_url(video_id),
                provider=self._provider.name,
                tracks=list(cached.tracks),
                default_selection_policy=DEFAULT_SELECTION_POLICY,
                retrieved_at=cached.retrieved_at,
                cache_hit=True,
            )
        tracks = await self._provider.list_transcripts(video_id)
        retrieved_at = self._now()
        entry = CachedTracks(tracks=tuple(tracks), retrieved_at=retrieved_at)
        self._cache.set(key, entry, ttl_seconds=self._ttl, size_bytes=256 + 96 * len(tracks))
        return TranscriptListResult(
            video_id=video_id,
            video_url=canonical_video_url(video_id),
            provider=self._provider.name,
            tracks=list(tracks),
            default_selection_policy=DEFAULT_SELECTION_POLICY,
            retrieved_at=retrieved_at,
            cache_hit=False,
        )

    async def get_transcript(self, request: GetTranscriptRequest) -> TranscriptPage:
        video_id = parse_video_input(request.video)
        languages = tuple(request.languages) if request.languages else None
        spec_key = (
            f"resolve:{self._provider.name}:{video_id}:"
            f"{','.join(languages) if languages else '*'}:{int(request.prefer_manual)}"
        )
        cached_entry: CachedTranscript | None = None
        track_key = self._cache.get(spec_key)
        if isinstance(track_key, tuple):
            candidate = self._cache.get(self._transcript_key(video_id, track_key))
            if isinstance(candidate, CachedTranscript):
                cached_entry = candidate

        if cached_entry is None:
            transcript = await self._provider.fetch_transcript(
                video_id,
                lambda tracks: select_track(tracks, languages, request.prefer_manual),
            )
            retrieved_at = self._now()
            key = (transcript.language_code.lower(), transcript.is_generated)
            entry = CachedTranscript(transcript=transcript, retrieved_at=retrieved_at)
            self._cache.set(
                self._transcript_key(video_id, key),
                entry,
                ttl_seconds=self._ttl,
                size_bytes=transcript.approx_bytes,
            )
            self._cache.set(spec_key, key, ttl_seconds=self._ttl, size_bytes=128)
            cache_hit = False
        else:
            transcript = cached_entry.transcript
            retrieved_at = cached_entry.retrieved_at
            cache_hit = True

        page = build_page(transcript, request, max_bytes=self._max_response_bytes)
        return TranscriptPage(
            video_id=video_id,
            video_url=canonical_video_url(video_id),
            language=transcript.language,
            language_code=transcript.language_code,
            is_generated=transcript.is_generated,
            selection="requested_language" if languages else "default_policy",
            provider=self._provider.name,
            retrieved_at=retrieved_at,
            cache_hit=cache_hit,
            format=request.format,
            total_segments=page.total_segments,
            matched_segments=page.matched_segments,
            offset=page.offset,
            returned_segments=page.returned_segments,
            next_offset=page.next_offset,
            has_more=page.has_more,
            truncated_by_size_limit=page.truncated_by_size_limit,
            start_seconds=request.start_seconds,
            end_seconds=request.end_seconds,
            segments=page.segments,
            text=page.text,
        )

    def _transcript_key(self, video_id: str, track_key: tuple[str, bool]) -> str:
        return f"transcript:{self._provider.name}:{video_id}:{track_key[0]}:{int(track_key[1])}"
