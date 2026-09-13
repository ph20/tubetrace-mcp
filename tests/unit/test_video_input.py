from __future__ import annotations

import pytest

from tubetrace_mcp.errors import ErrorCode, TubeTraceError
from tubetrace_mcp.video_input import canonical_video_url, parse_video_input, timestamp_url

VID = "dQw4w9WgXcQ"


@pytest.mark.parametrize(
    "value",
    [
        VID,
        f"  {VID}  ",
        f"https://www.youtube.com/watch?v={VID}",
        f"http://youtube.com/watch?v={VID}&t=10s",
        f"https://m.youtube.com/watch?v={VID}",
        f"https://music.youtube.com/watch?v={VID}&list=PLxyz",
        f"https://youtu.be/{VID}",
        f"https://youtu.be/{VID}?si=abc123",
        f"https://www.youtube.com/shorts/{VID}",
        f"https://www.youtube.com/embed/{VID}?autoplay=1",
        f"https://www.youtube.com/live/{VID}",
        f"https://www.youtube.com/v/{VID}",
        f"https://www.youtube-nocookie.com/embed/{VID}",
        f"www.youtube.com/watch?v={VID}",
        f"youtu.be/{VID}",
        f"https://WWW.YOUTUBE.COM/watch?v={VID}",
    ],
)
def test_accepts_valid_ids_and_urls(value: str) -> None:
    assert parse_video_input(value) == VID


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        (f"https://youtube.com.evil.example/watch?v={VID}", "host_not_allowed"),
        (f"https://evil.example/watch?v={VID}", "host_not_allowed"),
        (f"https://notyoutube.com/watch?v={VID}", "host_not_allowed"),
        (f"https://user:pw@www.youtube.com/watch?v={VID}", "userinfo_not_allowed"),
        (f"https://www.youtube.com@evil.example/watch?v={VID}", "userinfo_not_allowed"),
        (f"https://www.youtube.com:8443/watch?v={VID}", "explicit_port_not_allowed"),
        (f"ftp://www.youtube.com/watch?v={VID}", "unsupported_scheme"),
        ("javascript:alert(1)", "unsupported_scheme"),
        ("https://www.youtube.com/watch?v=short", "video_id_not_found"),
        ("https://www.youtube.com/playlist?list=PLabc", "video_id_not_found"),
        ("https://www.youtube.com/@somechannel", "video_id_not_found"),
        ("https://www.youtube.com/watch", "video_id_not_found"),
        ("", "empty"),
        ("   ", "empty"),
        ("x" * 2049, "too_long"),
        ("dQw4w9WgXc", "invalid_video_id"),
        ("dQw4w9WgXcQ!", "invalid_video_id"),
        ("dQw4w9WgXcQQ", "invalid_video_id"),
        ("mailto:someone@example.com", "unsupported_scheme"),
    ],
)
def test_rejects_invalid_input(value: str, reason: str) -> None:
    with pytest.raises(TubeTraceError) as info:
        parse_video_input(value)
    assert info.value.code == ErrorCode.INVALID_VIDEO_INPUT
    assert info.value.details["reason"] == reason


def test_helpers() -> None:
    assert canonical_video_url(VID) == f"https://www.youtube.com/watch?v={VID}"
    assert timestamp_url(VID, 65.9) == f"https://www.youtube.com/watch?v={VID}&t=65s"
    assert timestamp_url(VID, -3) == f"https://www.youtube.com/watch?v={VID}&t=0s"
