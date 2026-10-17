"""Parsing and validation of YouTube video, channel, and playlist identifiers and URLs.

Only a valid YouTube identifier or a URL whose hostname is on an explicit allowlist
is accepted. The URL is parsed with the standard library parser, never fetched,
and redirects are never followed: only the identifier is extracted.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import SplitResult, parse_qs, urlsplit

from .errors import ErrorCode, TubeTraceError
from .schemas import MAX_VIDEO_INPUT_LENGTH

VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
CHANNEL_ID_RE = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
CHANNEL_HANDLE_RE = re.compile(r"^@[A-Za-z0-9_.\-]{3,30}$")
PLAYLIST_ID_RE = re.compile(r"^(?:PL|UU|FL|LL|RD|UL|OLAK5uy_)[A-Za-z0-9_-]{6,64}$")
_ISO8601_DURATION_RE = re.compile(
    r"^P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?$"
)
_SCHEME_PREFIX_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*:")

ALLOWED_YOUTUBE_HOSTS: frozenset[str] = frozenset(
    {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
        "youtu.be",
        "www.youtu.be",
        "youtube-nocookie.com",
        "www.youtube-nocookie.com",
    }
)

_PATH_PREFIXES = ("/shorts/", "/embed/", "/live/", "/v/")


@dataclass(frozen=True, slots=True)
class ParsedVideoInput:
    """Extracted video ID and optional playlist ID from a video input."""

    video_id: str
    playlist_id: str | None = None


@dataclass(frozen=True, slots=True)
class ChannelTarget:
    """Extracted channel identifier (either a UC... channel_id or an @handle)."""

    channel_id: str | None = None
    handle: str | None = None

    @property
    def cache_token(self) -> str:
        if self.channel_id is not None:
            return f"id:{self.channel_id}"
        assert self.handle is not None
        return f"handle:{self.handle.lower()}"


def _invalid_video(reason: str) -> TubeTraceError:
    return TubeTraceError(
        ErrorCode.INVALID_VIDEO_INPUT,
        "Expected an 11-character YouTube video ID or a YouTube video URL "
        "(watch?v=, youtu.be/, shorts/, embed/, live/).",
        details={"reason": reason},
    )


def _invalid_channel(reason: str) -> TubeTraceError:
    return TubeTraceError(
        ErrorCode.INVALID_CHANNEL_INPUT,
        "Expected a YouTube channel ID (UC...), handle (@name), or YouTube channel URL "
        "(/channel/UC... or /@name).",
        details={"reason": reason},
    )


def _invalid_playlist(reason: str) -> TubeTraceError:
    return TubeTraceError(
        ErrorCode.INVALID_PLAYLIST_INPUT,
        "Expected a YouTube playlist ID (PL..., UU..., OLAK5uy_...) or a YouTube URL "
        "with a list=... parameter.",
        details={"reason": reason},
    )


def canonical_video_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def canonical_channel_url(channel_id: str) -> str:
    return f"https://www.youtube.com/channel/{channel_id}"


def canonical_playlist_url(playlist_id: str) -> str:
    return f"https://www.youtube.com/playlist?list={playlist_id}"


def timestamp_url(video_id: str, start_seconds: float) -> str:
    return f"https://www.youtube.com/watch?v={video_id}&t={int(max(start_seconds, 0))}s"


def parse_iso8601_duration(value: str | None) -> int | None:
    """Parse an ISO-8601 duration such as ``PT3M33S`` into total seconds."""
    if not isinstance(value, str) or not value:
        return None
    match = _ISO8601_DURATION_RE.match(value.strip())
    if not match or value.strip() in {"P", "PT"}:
        return None
    days = int(match.group("days") or 0)
    hours = int(match.group("hours") or 0)
    minutes = int(match.group("minutes") or 0)
    seconds = int(match.group("seconds") or 0)
    return ((days * 24 + hours) * 60 + minutes) * 60 + seconds


def _split_youtube_url(
    candidate: str, invalid: Callable[[str], TubeTraceError]
) -> tuple[SplitResult, str]:
    try:
        parts = urlsplit(candidate)
    except ValueError:
        raise invalid("unparseable_url") from None

    if parts.scheme.lower() not in {"http", "https"}:
        raise invalid("unsupported_scheme")
    if parts.username is not None or parts.password is not None:
        raise invalid("userinfo_not_allowed")
    try:
        port = parts.port
    except ValueError:
        raise invalid("invalid_port") from None
    if port is not None:
        raise invalid("explicit_port_not_allowed")
    hostname = (parts.hostname or "").lower()
    if hostname not in ALLOWED_YOUTUBE_HOSTS:
        raise invalid("host_not_allowed")
    return parts, hostname


def parse_video_with_playlist(value: str) -> ParsedVideoInput:
    """Return the video ID and optional playlist ID for a raw ID or allowlisted YouTube URL.

    Raises:
        TubeTraceError: with code ``INVALID_VIDEO_INPUT`` for invalid video inputs.
    """
    if not isinstance(value, str):
        raise _invalid_video("not_a_string")
    candidate = value.strip()
    if not candidate:
        raise _invalid_video("empty")
    if len(candidate) > MAX_VIDEO_INPUT_LENGTH:
        raise _invalid_video("too_long")
    if VIDEO_ID_RE.match(candidate):
        return ParsedVideoInput(video_id=candidate)

    if "://" not in candidate:
        if _SCHEME_PREFIX_RE.match(candidate):
            # e.g. "javascript:..." or "mailto:..." - never treated as a host.
            raise _invalid_video("unsupported_scheme")
        if "/" not in candidate and "." not in candidate:
            # Looks like an attempt at a bare video ID, but it is not 11 valid characters.
            raise _invalid_video("invalid_video_id")
        # Allow scheme-less forms such as "youtu.be/ID" or "www.youtube.com/watch?v=ID".
        candidate = "https://" + candidate

    parts, hostname = _split_youtube_url(candidate, _invalid_video)
    path = parts.path or "/"
    query = parse_qs(parts.query, keep_blank_values=False)

    video_id: str | None = None
    if hostname in {"youtu.be", "www.youtu.be"}:
        video_id = path.strip("/").split("/", 1)[0] if path.strip("/") else None
    elif path == "/watch" or path.startswith("/watch/"):
        values = query.get("v")
        video_id = values[0] if values else None
    else:
        for prefix in _PATH_PREFIXES:
            if path.startswith(prefix):
                remainder = path[len(prefix) :]
                video_id = remainder.split("/", 1)[0] if remainder else None
                break
        else:
            # Fallback: some share links carry the ID in the ``v`` query parameter.
            values = query.get("v")
            video_id = values[0] if values else None

    if not video_id or not VIDEO_ID_RE.match(video_id):
        raise _invalid_video("video_id_not_found")

    playlist_id: str | None = None
    list_values = query.get("list")
    if list_values and PLAYLIST_ID_RE.match(list_values[0]):
        playlist_id = list_values[0]
    return ParsedVideoInput(video_id=video_id, playlist_id=playlist_id)


def parse_video_input(value: str) -> str:
    """Return the video ID for a raw ID or an allowlisted YouTube URL.

    Raises:
        TubeTraceError: with code ``INVALID_VIDEO_INPUT`` for anything else.
    """
    return parse_video_with_playlist(value).video_id


def parse_channel_input(value: str) -> ChannelTarget:
    """Return a :class:`ChannelTarget` for a bare channel ID, ``@handle``, or channel URL.

    Raises:
        TubeTraceError: with code ``INVALID_CHANNEL_INPUT`` for anything else.
    """
    if not isinstance(value, str):
        raise _invalid_channel("not_a_string")
    candidate = value.strip()
    if not candidate:
        raise _invalid_channel("empty")
    if len(candidate) > MAX_VIDEO_INPUT_LENGTH:
        raise _invalid_channel("too_long")
    if CHANNEL_ID_RE.match(candidate):
        return ChannelTarget(channel_id=candidate)
    if CHANNEL_HANDLE_RE.match(candidate):
        return ChannelTarget(handle=candidate)

    if "://" not in candidate:
        if _SCHEME_PREFIX_RE.match(candidate):
            raise _invalid_channel("unsupported_scheme")
        if "/" not in candidate and "." not in candidate:
            raise _invalid_channel("invalid_channel_identifier")
        candidate = "https://" + candidate

    parts, hostname = _split_youtube_url(candidate, _invalid_channel)
    if hostname in {"youtu.be", "www.youtu.be"}:
        raise _invalid_channel("channel_not_found_in_url")

    segments = [seg for seg in (parts.path or "").split("/") if seg]
    if not segments:
        raise _invalid_channel("channel_not_found_in_url")

    first = segments[0]
    if first == "channel" and len(segments) >= 2:
        channel_id = segments[1]
        if CHANNEL_ID_RE.match(channel_id):
            return ChannelTarget(channel_id=channel_id)
        raise _invalid_channel("invalid_channel_id")

    if first.startswith("@"):
        if CHANNEL_HANDLE_RE.match(first):
            return ChannelTarget(handle=first)
        raise _invalid_channel("invalid_channel_handle")

    raise _invalid_channel("channel_not_found_in_url")


def parse_playlist_input(value: str) -> str:
    """Return the playlist ID for a raw playlist ID or an allowlisted YouTube URL with ``list=``.

    Raises:
        TubeTraceError: with code ``INVALID_PLAYLIST_INPUT`` for anything else.
    """
    if not isinstance(value, str):
        raise _invalid_playlist("not_a_string")
    candidate = value.strip()
    if not candidate:
        raise _invalid_playlist("empty")
    if len(candidate) > MAX_VIDEO_INPUT_LENGTH:
        raise _invalid_playlist("too_long")
    if PLAYLIST_ID_RE.match(candidate):
        return candidate

    if "://" not in candidate:
        if _SCHEME_PREFIX_RE.match(candidate):
            raise _invalid_playlist("unsupported_scheme")
        if "/" not in candidate and "." not in candidate:
            raise _invalid_playlist("invalid_playlist_id")
        candidate = "https://" + candidate

    parts, _ = _split_youtube_url(candidate, _invalid_playlist)
    query = parse_qs(parts.query, keep_blank_values=False)
    list_values = query.get("list")
    if not list_values or not PLAYLIST_ID_RE.match(list_values[0]):
        raise _invalid_playlist("playlist_id_not_found")
    return list_values[0]
