from __future__ import annotations

import pytest

from tubetrace_mcp.errors import ErrorCode, TubeTraceError
from tubetrace_mcp.schemas import TranscriptTrack
from tubetrace_mcp.services.selection import select_track


def track(code: str, generated: bool, language: str | None = None) -> TranscriptTrack:
    return TranscriptTrack(language=language or code, language_code=code, is_generated=generated)


TRACKS = [
    track("de", True),
    track("en-US", False),
    track("en", True),
    track("uk", False),
]


def test_language_order_beats_manual_preference() -> None:
    chosen = select_track(TRACKS, ["de", "uk"], prefer_manual=True)
    assert chosen.language_code == "de"  # generated, but first requested language wins


def test_manual_preference_within_language() -> None:
    tracks = [track("en", True), track("en", False)]
    assert select_track(tracks, ["en"], prefer_manual=True).is_generated is False
    assert select_track(tracks, ["en"], prefer_manual=False).is_generated is True


def test_falls_back_to_other_kind_within_language() -> None:
    tracks = [track("en", True)]
    assert select_track(tracks, ["en"], prefer_manual=True).is_generated is True


def test_exact_code_beats_base_language_match() -> None:
    chosen = select_track(TRACKS, ["en"], prefer_manual=True)
    assert chosen.language_code == "en"
    assert chosen.is_generated is True
    chosen = select_track(TRACKS, ["en-GB"], prefer_manual=True)
    assert chosen.language_code == "en-US"  # base-language fallback, manual preferred


def test_case_insensitive_codes() -> None:
    chosen = select_track(TRACKS, ["EN-us"], prefer_manual=True)
    assert chosen.language_code == "en-US"


def test_no_matching_language_lists_available() -> None:
    with pytest.raises(TubeTraceError) as info:
        select_track(TRACKS, ["fr", "es"], prefer_manual=True)
    assert info.value.code == ErrorCode.NO_MATCHING_TRANSCRIPT
    assert info.value.details["requested"] == ["fr", "es"]
    codes = {entry["language_code"] for entry in info.value.details["available"]}
    assert codes == {"de", "en-US", "en", "uk"}


def test_default_policy_is_deterministic() -> None:
    assert select_track(TRACKS, None, prefer_manual=True).language_code == "en-US"
    assert select_track(TRACKS, None, prefer_manual=False).language_code == "de"
    only_generated = [track("de", True), track("en", True)]
    assert select_track(only_generated, None, prefer_manual=True).language_code == "de"


def test_empty_tracks() -> None:
    with pytest.raises(TubeTraceError) as info:
        select_track([], None, prefer_manual=True)
    assert info.value.code == ErrorCode.NO_MATCHING_TRANSCRIPT
