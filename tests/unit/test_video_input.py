from __future__ import annotations

import pytest

from tubetrace_mcp.errors import ErrorCode, TubeTraceError
from tubetrace_mcp.video_input import (
    ChannelTarget,
    ParsedVideoInput,
    canonical_channel_url,
    canonical_playlist_url,
    canonical_video_url,
    parse_channel_input,
    parse_iso8601_duration,
    parse_playlist_input,
    parse_video_input,
    parse_video_with_playlist,
    timestamp_url,
)

VID = "dQw4w9WgXcQ"
CID = "UCuAXFkgsw1L7xaCfnd5JJOw"
PID = "PL1234567890abcdef"


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


def test_parse_video_with_playlist() -> None:
    assert parse_video_with_playlist(VID) == ParsedVideoInput(video_id=VID, playlist_id=None)
    assert parse_video_with_playlist(
        f"https://www.youtube.com/watch?v={VID}&list={PID}&index=3"
    ) == ParsedVideoInput(video_id=VID, playlist_id=PID)
    # Non-playlist 'WL' (Watch Later) is ignored rather than failing video parsing
    assert parse_video_with_playlist(
        f"https://www.youtube.com/watch?v={VID}&list=WL"
    ) == ParsedVideoInput(video_id=VID, playlist_id=None)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (CID, ChannelTarget(channel_id=CID)),
        (f"  {CID}  ", ChannelTarget(channel_id=CID)),
        (f"https://www.youtube.com/channel/{CID}", ChannelTarget(channel_id=CID)),
        (f"https://www.youtube.com/channel/{CID}/videos", ChannelTarget(channel_id=CID)),
        ("@PyConUS", ChannelTarget(handle="@PyConUS")),
        ("https://www.youtube.com/@PyConUS", ChannelTarget(handle="@PyConUS")),
        ("https://www.youtube.com/@PyConUS/playlists", ChannelTarget(handle="@PyConUS")),
        ("www.youtube.com/@PyConUS", ChannelTarget(handle="@PyConUS")),
    ],
)
def test_parse_channel_input_valid(value: str, expected: ChannelTarget) -> None:
    assert parse_channel_input(value) == expected


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        ("", "empty"),
        ("   ", "empty"),
        ("UCshort", "invalid_channel_identifier"),
        ("@ab", "invalid_channel_identifier"),
        ("https://evil.example/@PyConUS", "host_not_allowed"),
        (f"https://youtu.be/{VID}", "channel_not_found_in_url"),
        (f"https://www.youtube.com/watch?v={VID}", "channel_not_found_in_url"),
        ("https://www.youtube.com/channel/UCbad", "invalid_channel_id"),
        ("javascript:alert(1)", "unsupported_scheme"),
    ],
)
def test_parse_channel_input_invalid(value: str, reason: str) -> None:
    with pytest.raises(TubeTraceError) as info:
        parse_channel_input(value)
    assert info.value.code == ErrorCode.INVALID_CHANNEL_INPUT
    assert info.value.details["reason"] == reason


@pytest.mark.parametrize(
    "value",
    [
        PID,
        f"  {PID}  ",
        f"https://www.youtube.com/playlist?list={PID}",
        f"https://www.youtube.com/watch?v={VID}&list={PID}",
        f"www.youtube.com/playlist?list={PID}",
    ],
)
def test_parse_playlist_input_valid(value: str) -> None:
    assert parse_playlist_input(value) == PID


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        ("", "empty"),
        ("PLshort", "invalid_playlist_id"),
        (f"https://www.youtube.com/watch?v={VID}", "playlist_id_not_found"),
        ("https://www.youtube.com/playlist?list=WL", "playlist_id_not_found"),
        (f"https://evil.example/playlist?list={PID}", "host_not_allowed"),
    ],
)
def test_parse_playlist_input_invalid(value: str, reason: str) -> None:
    with pytest.raises(TubeTraceError) as info:
        parse_playlist_input(value)
    assert info.value.code == ErrorCode.INVALID_PLAYLIST_INPUT
    assert info.value.details["reason"] == reason


def test_helpers_and_iso8601_duration() -> None:
    assert canonical_video_url(VID) == f"https://www.youtube.com/watch?v={VID}"
    assert canonical_channel_url(CID) == f"https://www.youtube.com/channel/{CID}"
    assert canonical_playlist_url(PID) == f"https://www.youtube.com/playlist?list={PID}"
    assert timestamp_url(VID, 65.9) == f"https://www.youtube.com/watch?v={VID}&t=65s"
    assert timestamp_url(VID, -3) == f"https://www.youtube.com/watch?v={VID}&t=0s"
    assert parse_iso8601_duration("PT3M33S") == 213
    assert parse_iso8601_duration("PT1H2M3S") == 3723
    assert parse_iso8601_duration("P1DT2H") == 93600
    assert parse_iso8601_duration("P0D") == 0
    assert parse_iso8601_duration(None) is None
    assert parse_iso8601_duration("invalid") is None
    assert parse_iso8601_duration("PT") is None
