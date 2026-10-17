"""Pydantic models for tool inputs and structured tool outputs."""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_QUERY_LENGTH = 256
MAX_VIDEO_INPUT_LENGTH = 2048
MAX_PAGE_TOKEN_LENGTH = 512
MAX_LANGUAGES = 10
MAX_PROMPT_LENGTH = 4000
PAGE_TOKEN_PATTERN = r"^[A-Za-z0-9_=\-]+$"  # noqa: S105 - regex pattern, not a secret

PROVIDER_YOUTUBE_DATA_API = "youtube_data_api_v3"
TOTAL_RESULTS_NOTE = (
    "Google reports totalResults as an approximation; it does not guarantee the exact "
    "number of results that can actually be retrieved through pagination."
)
LANGUAGE_CODE_PATTERN = r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*$"


def _normalize_language_list(value: list[str] | None) -> list[str] | None:
    if value is None:
        return None
    normalized: list[str] = []
    for code in value:
        code = code.strip()
        if not re.match(LANGUAGE_CODE_PATTERN, code):
            raise ValueError(f"invalid language code: {code!r}")
        lowered = code.lower()
        if lowered not in normalized:
            normalized.append(lowered)
    if not normalized:
        raise ValueError("languages must not be empty when provided")
    return normalized


class SearchOrder(StrEnum):
    RELEVANCE = "relevance"
    DATE = "date"
    VIEW_COUNT = "viewCount"
    RATING = "rating"
    TITLE = "title"


class VideoDuration(StrEnum):
    ANY = "any"
    SHORT = "short"
    MEDIUM = "medium"
    LONG = "long"


class CaptionFilter(StrEnum):
    ANY = "any"
    CLOSED_CAPTION = "closedCaption"
    NONE = "none"


class SafeSearch(StrEnum):
    MODERATE = "moderate"
    STRICT = "strict"
    NONE = "none"


class TranscriptFormat(StrEnum):
    SEGMENTS = "segments"
    TEXT = "text"


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- inputs


class SearchVideosRequest(_StrictModel):
    """Validated parameters for one YouTube Data API ``search.list`` page."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    query: str = Field(min_length=1, max_length=MAX_QUERY_LENGTH)
    max_results: int = Field(default=10, ge=1, le=50)
    page_token: str | None = Field(
        default=None, max_length=MAX_PAGE_TOKEN_LENGTH, pattern=PAGE_TOKEN_PATTERN
    )
    channel_id: str | None = Field(default=None, pattern=r"^UC[A-Za-z0-9_\-]{22}$")
    published_after: AwareDatetime | None = None
    published_before: AwareDatetime | None = None
    order: SearchOrder = SearchOrder.RELEVANCE
    relevance_language: str | None = Field(default=None, pattern=LANGUAGE_CODE_PATTERN)
    region_code: str | None = Field(default=None, pattern=r"^[A-Za-z]{2}$")
    video_duration: VideoDuration = VideoDuration.ANY
    caption_filter: CaptionFilter = CaptionFilter.ANY
    safe_search: SafeSearch = SafeSearch.MODERATE

    @field_validator("query")
    @classmethod
    def _strip_query(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("query must not be blank")
        return stripped

    @field_validator("region_code")
    @classmethod
    def _upper_region(cls, value: str | None) -> str | None:
        return value.upper() if value else value

    @model_validator(mode="after")
    def _check_date_range(self) -> SearchVideosRequest:
        if (
            self.published_after is not None
            and self.published_before is not None
            and self.published_after >= self.published_before
        ):
            raise ValueError("published_after must be earlier than published_before")
        return self

    def cache_key(self) -> str:
        """Stable cache key covering every parameter that influences the upstream call."""
        return self.model_dump_json()


class GetVideoRequest(_StrictModel):
    """Validated parameters for fetching one video's metadata (and optional playlist summary)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    video: str = Field(min_length=1, max_length=MAX_VIDEO_INPUT_LENGTH)
    playlist: str | None = Field(default=None, min_length=1, max_length=MAX_VIDEO_INPUT_LENGTH)


class GetChannelRequest(_StrictModel):
    """Validated parameters for fetching channel metadata and one page of uploads."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    channel: str = Field(min_length=1, max_length=MAX_VIDEO_INPUT_LENGTH)
    include_videos: bool = True
    max_results: int = Field(default=20, ge=1, le=50)
    page_token: str | None = Field(
        default=None, max_length=MAX_PAGE_TOKEN_LENGTH, pattern=PAGE_TOKEN_PATTERN
    )


class GetPlaylistRequest(_StrictModel):
    """Validated parameters for fetching playlist metadata and one page of items."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    playlist: str = Field(min_length=1, max_length=MAX_VIDEO_INPUT_LENGTH)
    include_videos: bool = True
    max_results: int = Field(default=25, ge=1, le=50)
    page_token: str | None = Field(
        default=None, max_length=MAX_PAGE_TOKEN_LENGTH, pattern=PAGE_TOKEN_PATTERN
    )


class ListChannelPlaylistsRequest(_StrictModel):
    """Validated parameters for listing public playlists owned by a channel."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    channel: str = Field(min_length=1, max_length=MAX_VIDEO_INPUT_LENGTH)
    max_results: int = Field(default=25, ge=1, le=50)
    page_token: str | None = Field(
        default=None, max_length=MAX_PAGE_TOKEN_LENGTH, pattern=PAGE_TOKEN_PATTERN
    )


class GetTranscriptRequest(_StrictModel):
    """Validated parameters for one transcript page."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    video: str = Field(min_length=1, max_length=MAX_VIDEO_INPUT_LENGTH)
    languages: list[str] | None = Field(default=None, max_length=MAX_LANGUAGES)
    prefer_manual: bool = True
    start_seconds: float | None = Field(default=None, ge=0)
    end_seconds: float | None = Field(default=None, ge=0)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=100, ge=1, le=500)
    format: TranscriptFormat = TranscriptFormat.SEGMENTS

    @field_validator("languages")
    @classmethod
    def _normalize_languages(cls, value: list[str] | None) -> list[str] | None:
        return _normalize_language_list(value)

    @model_validator(mode="after")
    def _check_time_range(self) -> GetTranscriptRequest:
        if (
            self.start_seconds is not None
            and self.end_seconds is not None
            and self.end_seconds <= self.start_seconds
        ):
            raise ValueError("end_seconds must be greater than start_seconds")
        return self


class AnalyzeTranscriptRequest(_StrictModel):
    """Validated parameters for server-side single-pass transcript Q&A / analysis."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    video: str = Field(min_length=1, max_length=MAX_VIDEO_INPUT_LENGTH)
    prompt: str = Field(min_length=1, max_length=MAX_PROMPT_LENGTH)
    languages: list[str] | None = Field(default=None, max_length=MAX_LANGUAGES)
    prefer_manual: bool = True
    start_seconds: float | None = Field(default=None, ge=0)
    end_seconds: float | None = Field(default=None, ge=0)
    output_language: str | None = Field(default=None, max_length=64)

    @field_validator("prompt")
    @classmethod
    def _strip_prompt(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("prompt must not be blank")
        return stripped

    @field_validator("output_language")
    @classmethod
    def _strip_output_language(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None

    @field_validator("languages")
    @classmethod
    def _normalize_languages(cls, value: list[str] | None) -> list[str] | None:
        return _normalize_language_list(value)

    @model_validator(mode="after")
    def _check_time_range(self) -> AnalyzeTranscriptRequest:
        if (
            self.start_seconds is not None
            and self.end_seconds is not None
            and self.end_seconds <= self.start_seconds
        ):
            raise ValueError("end_seconds must be greater than start_seconds")
        return self


# -------------------------------------------------------------------------- outputs


class ErrorPayload(BaseModel):
    """Public shape of an expected error (see :mod:`tubetrace_mcp.errors`)."""

    code: str
    message: str
    retryable: bool
    details: dict[str, Any] | None = None


class ErrorEnvelope(BaseModel):
    error: ErrorPayload


class SearchVideoItem(BaseModel):
    video_id: str
    video_url: str
    title: str
    description: str
    channel_id: str
    channel_title: str
    published_at: datetime | None = Field(
        default=None, description="Publish time reported by YouTube (RFC 3339)."
    )
    thumbnail_url: str | None = Field(
        default=None, description="Highest-resolution thumbnail URL returned in the snippet."
    )
    live_broadcast_content: str | None = Field(
        default=None, description="YouTube liveBroadcastContent value: none, live or upcoming."
    )


class SearchVideosResult(BaseModel):
    provider: str = PROVIDER_YOUTUBE_DATA_API
    query: str
    items: list[SearchVideoItem]
    next_page_token: str | None = Field(
        default=None, description="Pass as page_token to fetch the next page (one page per call)."
    )
    prev_page_token: str | None = None
    results_per_page: int | None = None
    total_results_estimate: int | None = Field(
        default=None,
        description="Google's approximate total (pageInfo.totalResults). See total_results_note.",
    )
    total_results_note: str = TOTAL_RESULTS_NOTE
    region_code: str | None = None
    retrieved_at: datetime = Field(description="When the data was fetched from Google (UTC).")
    cache_hit: bool = False


class PlaylistSummary(BaseModel):
    """Compact playlist metadata attached to a video when a playlist ID/URL is present."""

    playlist_id: str
    playlist_url: str
    title: str
    channel_id: str
    channel_title: str
    item_count: int | None = None


class VideoMetadataResult(BaseModel):
    provider: str = PROVIDER_YOUTUBE_DATA_API
    video_id: str
    video_url: str
    title: str
    description: str
    channel_id: str
    channel_title: str
    channel_url: str
    published_at: datetime | None = None
    duration_iso: str | None = Field(
        default=None, description="ISO-8601 duration reported by YouTube (e.g. 'PT3M33S')."
    )
    duration_seconds: int | None = Field(
        default=None, description="Total video duration in seconds."
    )
    has_captions: bool | None = Field(
        default=None,
        description="Whether YouTube Data API contentDetails.caption reports captions available.",
    )
    definition: str | None = Field(
        default=None, description="Video definition reported by YouTube ('hd' or 'sd')."
    )
    default_language: str | None = None
    default_audio_language: str | None = None
    tags: list[str] = Field(default_factory=list)
    category_id: str | None = None
    view_count: int | None = None
    like_count: int | None = None
    comment_count: int | None = None
    live_broadcast_content: str | None = None
    thumbnail_url: str | None = None
    playlist: PlaylistSummary | None = Field(
        default=None,
        description="Populated when the video URL contained list=... or 'playlist' was provided.",
    )
    retrieved_at: datetime
    cache_hit: bool = False


class PlaylistVideoItem(BaseModel):
    """One video item inside a channel uploads playlist or a user playlist."""

    position: int | None = None
    video_id: str
    video_url: str
    title: str
    description: str
    video_owner_channel_id: str | None = None
    video_owner_channel_title: str | None = None
    published_at: datetime | None = Field(
        default=None, description="Original video publish time (contentDetails.videoPublishedAt)."
    )
    added_at: datetime | None = Field(
        default=None, description="When the item was added to the playlist (snippet.publishedAt)."
    )
    privacy_status: str | None = None
    thumbnail_url: str | None = None


class ChannelResult(BaseModel):
    provider: str = PROVIDER_YOUTUBE_DATA_API
    channel_id: str
    channel_url: str
    title: str
    description: str
    custom_url: str | None = Field(
        default=None, description="Channel handle/custom URL such as '@channelname'."
    )
    country: str | None = None
    published_at: datetime | None = None
    thumbnail_url: str | None = None
    subscriber_count: int | None = None
    video_count: int | None = None
    view_count: int | None = None
    uploads_playlist_id: str | None = Field(
        default=None, description="Playlist ID (UU...) containing all public channel uploads."
    )
    videos: list[PlaylistVideoItem] | None = Field(
        default=None, description="Present when include_videos=true."
    )
    next_page_token: str | None = None
    prev_page_token: str | None = None
    retrieved_at: datetime
    cache_hit: bool = False


class PlaylistResult(BaseModel):
    provider: str = PROVIDER_YOUTUBE_DATA_API
    playlist_id: str
    playlist_url: str
    title: str
    description: str
    channel_id: str
    channel_title: str
    published_at: datetime | None = None
    item_count: int | None = None
    thumbnail_url: str | None = None
    items: list[PlaylistVideoItem] | None = Field(
        default=None, description="Present when include_videos=true."
    )
    next_page_token: str | None = None
    prev_page_token: str | None = None
    retrieved_at: datetime
    cache_hit: bool = False


class ChannelPlaylistItem(BaseModel):
    playlist_id: str
    playlist_url: str
    title: str
    description: str
    item_count: int | None = None
    published_at: datetime | None = None
    thumbnail_url: str | None = None


class ChannelPlaylistsResult(BaseModel):
    provider: str = PROVIDER_YOUTUBE_DATA_API
    channel_id: str
    channel_url: str
    playlists: list[ChannelPlaylistItem]
    next_page_token: str | None = None
    prev_page_token: str | None = None
    results_per_page: int | None = None
    total_results_estimate: int | None = None
    retrieved_at: datetime
    cache_hit: bool = False


class TranscriptTrack(BaseModel):
    """Metadata of one caption track as reported by the transcript provider."""

    model_config = ConfigDict(frozen=True)

    language: str = Field(description="Human-readable language name reported by YouTube.")
    language_code: str = Field(description="BCP-47 style language code, e.g. 'en' or 'pt-BR'.")
    is_generated: bool = Field(description="True for automatically generated (ASR) captions.")
    is_translatable: bool = Field(
        default=False, description="Whether YouTube offers machine translation for this track."
    )

    @property
    def key(self) -> tuple[str, bool]:
        return (self.language_code.lower(), self.is_generated)


class TranscriptListResult(BaseModel):
    video_id: str
    video_url: str
    provider: str
    tracks: list[TranscriptTrack]
    default_selection_policy: str = Field(
        description="How youtube_get_transcript picks a track when 'languages' is omitted."
    )
    retrieved_at: datetime
    cache_hit: bool = False


class TranscriptSegment(BaseModel):
    index: int = Field(description="Zero-based position within the full transcript.")
    text: str
    start_seconds: float
    duration_seconds: float
    end_seconds: float
    timestamp_url: str = Field(description="YouTube watch URL that starts at the segment.")


class TranscriptPage(BaseModel):
    video_id: str
    video_url: str
    language: str
    language_code: str
    is_generated: bool
    selection: str = Field(
        description="Why this track was chosen: 'requested_language' or 'default_policy'."
    )
    provider: str
    retrieved_at: datetime
    cache_hit: bool
    format: TranscriptFormat
    total_segments: int = Field(description="Number of segments in the full transcript.")
    matched_segments: int = Field(
        description="Segments overlapping the requested time range (all segments if no range)."
    )
    offset: int = Field(description="Offset (within matched segments) this page starts at.")
    returned_segments: int
    next_offset: int | None = Field(
        default=None, description="Offset to pass for the next page; null when has_more is false."
    )
    has_more: bool
    truncated_by_size_limit: bool = Field(
        default=False,
        description="True when the page was shortened to respect the response size limit.",
    )
    start_seconds: float | None = None
    end_seconds: float | None = None
    segments: list[TranscriptSegment] | None = Field(
        default=None, description="Present when format=segments."
    )
    text: str | None = Field(
        default=None,
        description="Present when format=text: this page's segment texts joined by newlines.",
    )


class TranscriptCitation(BaseModel):
    """A timestamped reference returned by youtube_analyze_transcript."""

    start_seconds: float
    timestamp_url: str
    label: str


class LLMUsage(BaseModel):
    """Token usage reported by the long-context LLM provider."""

    input_tokens: int | None = None
    output_tokens: int | None = None


class AnalyzeTranscriptResult(BaseModel):
    video_id: str
    video_url: str
    language: str
    language_code: str
    is_generated: bool
    selection: str
    transcript_provider: str
    transcript_cache_hit: bool
    llm_provider: str
    llm_model: str
    total_segments: int
    analyzed_segments: int
    transcript_characters: int
    start_seconds: float | None = None
    end_seconds: float | None = None
    answer: str
    citations: list[TranscriptCitation] = Field(default_factory=list)
    usage: LLMUsage | None = None
    retrieved_at: datetime


class HealthStatus(BaseModel):
    status: str
    app: str
    version: str
