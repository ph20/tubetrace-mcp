"""Parsing and validation of YouTube video identifiers and URLs.

Only an 11-character video ID or a URL whose hostname is on an explicit allowlist
is accepted. The URL is parsed with the standard library parser, never fetched,
and redirects are never followed: only the ID is extracted.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit

from .errors import ErrorCode, TubeTraceError
from .schemas import MAX_VIDEO_INPUT_LENGTH

VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
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


def _invalid(reason: str) -> TubeTraceError:
    return TubeTraceError(
        ErrorCode.INVALID_VIDEO_INPUT,
        "Expected an 11-character YouTube video ID or a YouTube video URL "
        "(watch?v=, youtu.be/, shorts/, embed/, live/).",
        details={"reason": reason},
    )


def canonical_video_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def timestamp_url(video_id: str, start_seconds: float) -> str:
    return f"https://www.youtube.com/watch?v={video_id}&t={int(max(start_seconds, 0))}s"


def parse_video_input(value: str) -> str:
    """Return the video ID for a raw ID or an allowlisted YouTube URL.

    Raises:
        TubeTraceError: with code ``INVALID_VIDEO_INPUT`` for anything else.
    """
    if not isinstance(value, str):
        raise _invalid("not_a_string")
    candidate = value.strip()
    if not candidate:
        raise _invalid("empty")
    if len(candidate) > MAX_VIDEO_INPUT_LENGTH:
        raise _invalid("too_long")
    if VIDEO_ID_RE.match(candidate):
        return candidate

    if "://" not in candidate:
        if _SCHEME_PREFIX_RE.match(candidate):
            # e.g. "javascript:..." or "mailto:..." - never treated as a host.
            raise _invalid("unsupported_scheme")
        if "/" not in candidate and "." not in candidate:
            # Looks like an attempt at a bare video ID, but it is not 11 valid characters.
            raise _invalid("invalid_video_id")
        # Allow scheme-less forms such as "youtu.be/ID" or "www.youtube.com/watch?v=ID".
        candidate = "https://" + candidate

    try:
        parts = urlsplit(candidate)
    except ValueError:
        raise _invalid("unparseable_url") from None

    if parts.scheme.lower() not in {"http", "https"}:
        raise _invalid("unsupported_scheme")
    if parts.username is not None or parts.password is not None:
        raise _invalid("userinfo_not_allowed")
    try:
        port = parts.port
    except ValueError:
        raise _invalid("invalid_port") from None
    if port is not None:
        raise _invalid("explicit_port_not_allowed")
    hostname = (parts.hostname or "").lower()
    if hostname not in ALLOWED_YOUTUBE_HOSTS:
        raise _invalid("host_not_allowed")

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
        raise _invalid("video_id_not_found")
    return video_id
