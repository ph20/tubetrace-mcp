"""Deterministic transcript track selection.

Rules (documented for users in README):

* When ``languages`` is given, the order of the list is the priority order and
  outranks ``prefer_manual``. For each requested code, tracks with the exact code
  (case-insensitive) are considered first, then tracks sharing the base language
  (``en`` matches ``en-US`` and vice versa). Inside one tier ``prefer_manual``
  chooses between manually created and auto-generated tracks, falling back to the
  other kind when the preferred kind is missing.
* When ``languages`` is omitted, the preferred kind (manual unless
  ``prefer_manual=false``) is used and, inside it, the first track in the
  provider's listing order. This is **not** necessarily the video's original
  language; the provider does not expose that information.
"""

from __future__ import annotations

from collections.abc import Sequence

from ..errors import ErrorCode, TubeTraceError
from ..schemas import TranscriptTrack

DEFAULT_SELECTION_POLICY = (
    "Without 'languages': prefer manually created tracks over auto-generated ones "
    "(unless prefer_manual=false) and take the first track in the provider's listing "
    "order; this is not necessarily the video's original language. With 'languages': "
    "list order first (exact code, then same base language), prefer_manual within a language."
)


def _prefer(candidates: Sequence[TranscriptTrack], prefer_manual: bool) -> TranscriptTrack | None:
    if not candidates:
        return None
    preferred = [t for t in candidates if t.is_generated != prefer_manual]
    return preferred[0] if preferred else candidates[0]


def _base(code: str) -> str:
    return code.lower().split("-", 1)[0]


def available_summary(tracks: Sequence[TranscriptTrack]) -> list[dict[str, object]]:
    return [
        {"language_code": t.language_code, "language": t.language, "is_generated": t.is_generated}
        for t in tracks
    ]


def select_track(
    tracks: Sequence[TranscriptTrack],
    languages: Sequence[str] | None,
    prefer_manual: bool,
) -> TranscriptTrack:
    """Pick one track; raise ``NO_MATCHING_TRANSCRIPT`` with the available languages."""
    if not tracks:
        raise TubeTraceError(
            ErrorCode.NO_MATCHING_TRANSCRIPT,
            "No caption tracks are available for this video.",
            details={"available": []},
        )
    if languages:
        for requested in languages:
            code = requested.lower()
            exact = [t for t in tracks if t.language_code.lower() == code]
            chosen = _prefer(exact, prefer_manual)
            if chosen is not None:
                return chosen
            related = [
                t
                for t in tracks
                if _base(t.language_code) == _base(code) and t.language_code.lower() != code
            ]
            chosen = _prefer(related, prefer_manual)
            if chosen is not None:
                return chosen
        raise TubeTraceError(
            ErrorCode.NO_MATCHING_TRANSCRIPT,
            "No transcript is available in the requested languages: " + ", ".join(languages) + ".",
            details={"requested": list(languages), "available": available_summary(tracks)},
        )
    chosen = _prefer(tracks, prefer_manual)
    assert chosen is not None  # tracks is non-empty
    return chosen
