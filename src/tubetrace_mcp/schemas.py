"""Pydantic models for tool inputs and structured tool outputs."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_QUERY_LENGTH = 256
MAX_VIDEO_INPUT_LENGTH = 2048
MAX_PAGE_TOKEN_LENGTH = 512
MAX_LANGUAGES = 10

PROVIDER_YOUTUBE_DATA_API = "youtube_data_api_v3"
TOTAL_RESULTS_NOTE = (
    "Google reports totalResults as an approximation; it does not guarantee the exact "
    "number of results that can actually be retrieved through pagination."
)
LANGUAGE_CODE_PATTERN = r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*$"


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
        default=None, max_length=MAX_PAGE_TOKEN_LENGTH, pattern=r"^[A-Za-z0-9_=\-]+$"
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
        if value is None:
            return None
        import re

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

    @model_validator(mode="after")
    def _check_time_range(self) -> GetTranscriptRequest:
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


class HealthStatus(BaseModel):
    status: str
    app: str
    version: str
