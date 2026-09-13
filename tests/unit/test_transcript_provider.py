from __future__ import annotations

from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import Any, ClassVar

import pytest
import requests
from requests import exceptions as req_exc
from youtube_transcript_api import (
    AgeRestricted,
    IpBlocked,
    PoTokenRequired,
    RequestBlocked,
    TranscriptsDisabled,
    VideoUnavailable,
    VideoUnplayable,
    YouTubeDataUnparsable,
    YouTubeRequestFailed,
)

from tests.conftest import NoSleep
from tubetrace_mcp.errors import ErrorCode, TubeTraceError
from tubetrace_mcp.providers.youtube_transcript_api import (
    TimeoutSession,
    YouTubeTranscriptApiProvider,
    classify_exception,
)
from tubetrace_mcp.schemas import TranscriptTrack

VID = "dQw4w9WgXcQ"


class FakeTranscript:
    def __init__(
        self, code: str, generated: bool, snippets: list[Any], *, fail: Exception | None = None
    ) -> None:
        self.video_id = VID
        self.language = f"Lang {code}"
        self.language_code = code
        self.is_generated = generated
        self.is_translatable = not generated
        self._snippets = snippets
        self._fail = fail
        self.fetch_calls = 0

    def fetch(self) -> Any:
        self.fetch_calls += 1
        if self._fail is not None:
            raise self._fail
        return type(
            "Fetched",
            (),
            {
                "snippets": self._snippets,
                "language": self.language,
                "language_code": self.language_code,
                "is_generated": self.is_generated,
            },
        )()


class Snippet:
    def __init__(self, text: str, start: float, duration: float) -> None:
        self.text = text
        self.start = start
        self.duration = duration


class FakeApi:
    """Mimics YouTubeTranscriptApi.list(); records the session it was built with."""

    instances: ClassVar[list[FakeApi]] = []

    def __init__(
        self,
        session: requests.Session,
        *,
        transcripts: list[FakeTranscript] | None = None,
        errors: list[Exception] | None = None,
    ) -> None:
        self.session = session
        self.transcripts = transcripts or []
        self.errors = errors or []
        FakeApi.instances.append(self)

    def list(self, video_id: str) -> list[FakeTranscript]:
        if self.errors:
            raise self.errors.pop(0)
        return list(self.transcripts)


class SessionSpy(TimeoutSession):
    closed_count = 0

    def close(self) -> None:
        SessionSpy.closed_count += 1
        super().close()


@pytest.fixture
def executor() -> Any:
    pool = ThreadPoolExecutor(max_workers=2)
    yield pool
    pool.shutdown(wait=True)


def make_provider(
    executor: ThreadPoolExecutor, api_factory: Any, **kwargs: Any
) -> YouTubeTranscriptApiProvider:
    defaults: dict[str, Any] = {
        "max_concurrency": 2,
        "queue_timeout_seconds": 5,
        "connect_timeout_seconds": 1,
        "read_timeout_seconds": 2,
        "max_retries": 2,
        "retry_budget_seconds": 30,
        "max_segments": 1000,
        "max_bytes": 100_000,
        "sleep": NoSleep(),
        "rng": lambda: 0.0,
        "session_factory": lambda: SessionSpy(1, 2),
    }
    defaults.update(kwargs)
    return YouTubeTranscriptApiProvider(executor=executor, api_factory=api_factory, **defaults)


def shared_errors(errors: list[Any], transcripts: list[FakeTranscript] | None = None) -> Any:
    state = {"errors": errors}

    def factory(session: requests.Session) -> FakeApi:
        api = FakeApi(session, transcripts=transcripts)
        api.errors = state["errors"]
        return api

    return factory


async def test_list_and_fetch_success(executor: ThreadPoolExecutor) -> None:
    manual = FakeTranscript("en", False, [Snippet("Hello", 0.0, 1.5), Snippet("World", 1.5, 2.0)])
    generated = FakeTranscript("en", True, [Snippet("hello world", 0.0, 3.5)])
    provider = make_provider(executor, shared_errors([], [generated, manual]))
    tracks = await provider.list_transcripts(VID)
    assert [t.key for t in tracks] == [("en", True), ("en", False)]
    assert tracks[1].is_translatable is True

    transcript = await provider.fetch_transcript(
        VID, lambda ts: next(t for t in ts if not t.is_generated)
    )
    assert transcript.language_code == "en"
    assert transcript.is_generated is False
    assert [s.text for s in transcript.segments] == ["Hello", "World"]
    assert transcript.segments[1].start == 1.5
    assert manual.fetch_calls == 1
    assert generated.fetch_calls == 0, "only the selected track is downloaded"


async def test_fresh_session_per_attempt_and_closed(executor: ThreadPoolExecutor) -> None:
    FakeApi.instances.clear()
    SessionSpy.closed_count = 0
    timeouts: list[Any] = [req_exc.ReadTimeout("slow"), req_exc.ReadTimeout("slow")]
    provider = make_provider(
        executor,
        shared_errors(
            timeouts,
            [FakeTranscript("en", False, [])],
        ),
    )
    tracks = await provider.list_transcripts(VID)
    assert len(tracks) == 1
    sessions = [api.session for api in FakeApi.instances]
    assert len(sessions) == 3
    assert len(set(map(id, sessions))) == 3
    assert SessionSpy.closed_count == 3


async def test_timeout_retries_are_bounded(executor: ThreadPoolExecutor) -> None:
    errors: list[Any] = [req_exc.ReadTimeout("slow")] * 10
    provider = make_provider(executor, shared_errors(errors), max_retries=2)
    with pytest.raises(TubeTraceError) as info:
        await provider.list_transcripts(VID)
    assert info.value.code == ErrorCode.UPSTREAM_TIMEOUT
    assert info.value.retryable is True
    assert len(errors) == 7  # 1 initial + 2 retries consumed


@pytest.mark.parametrize(
    ("exc", "code", "reason"),
    [
        (TranscriptsDisabled(VID), ErrorCode.TRANSCRIPTS_DISABLED, None),
        (VideoUnavailable(VID), ErrorCode.VIDEO_UNAVAILABLE, "unavailable"),
        (AgeRestricted(VID), ErrorCode.VIDEO_UNAVAILABLE, "age_restricted"),
        (
            VideoUnplayable(VID, "This video is private", []),
            ErrorCode.VIDEO_UNAVAILABLE,
            "unplayable",
        ),
        (IpBlocked(VID), ErrorCode.UPSTREAM_BLOCKED, "ip_blocked"),
        (RequestBlocked(VID), ErrorCode.UPSTREAM_BLOCKED, "request_blocked"),
        (PoTokenRequired(VID), ErrorCode.UPSTREAM_BLOCKED, "po_token_required"),
        (YouTubeDataUnparsable(VID), ErrorCode.UPSTREAM_ERROR, "unparsable_response"),
    ],
)
async def test_library_exceptions_are_classified_and_not_retried(
    executor: ThreadPoolExecutor, exc: Exception, code: ErrorCode, reason: str | None
) -> None:
    errors = [exc, exc, exc]
    provider = make_provider(executor, shared_errors(errors))
    with pytest.raises(TubeTraceError) as info:
        await provider.fetch_transcript(VID, lambda ts: ts[0])
    assert info.value.code == code
    assert info.value.retryable is False
    if reason:
        assert info.value.details["reason"] == reason
    assert len(errors) == 2, "non-retryable errors must not be retried"


def test_blocking_is_not_reported_as_missing_subtitles() -> None:
    err = classify_exception(IpBlocked(VID))
    assert err.code == ErrorCode.UPSTREAM_BLOCKED
    assert "blocked" in err.message.lower()
    assert "not bypass" in err.message
    unparsable = classify_exception(YouTubeDataUnparsable(VID))
    assert unparsable.code == ErrorCode.UPSTREAM_ERROR
    assert "does not mean the video has no subtitles" in unparsable.message


def http_failure(status: int) -> YouTubeRequestFailed:
    kind = "Server" if status >= 500 else "Client"
    message = f"{status} {kind} Error: Something for url: https://www.youtube.com/watch?v={VID}"
    return YouTubeRequestFailed(VID, requests.HTTPError(message))


async def test_http_429_maps_to_rate_limit_and_is_retried(executor: ThreadPoolExecutor) -> None:
    sleep = NoSleep()
    errors: list[Any] = [http_failure(429)]
    provider = make_provider(
        executor, shared_errors(errors, [FakeTranscript("uk", True, [])]), sleep=sleep
    )
    tracks = await provider.list_transcripts(VID)
    assert tracks[0].language_code == "uk"
    assert len(sleep.calls) == 1 and sleep.calls[0] > 0


async def test_http_5xx_retryable_then_error(executor: ThreadPoolExecutor) -> None:
    errors: list[Any] = [http_failure(503), http_failure(503), http_failure(503), http_failure(503)]
    provider = make_provider(executor, shared_errors(errors), max_retries=1)
    with pytest.raises(TubeTraceError) as info:
        await provider.list_transcripts(VID)
    assert info.value.code == ErrorCode.UPSTREAM_ERROR
    assert info.value.retryable is True
    assert info.value.details["status"] == 503
    assert len(errors) == 2


def test_http_4xx_not_retryable() -> None:
    err = classify_exception(http_failure(404))
    assert err.code == ErrorCode.UPSTREAM_ERROR
    assert err.retryable is False


def test_generic_request_and_unexpected_exceptions() -> None:
    assert classify_exception(req_exc.ConnectionError("x")).retryable is True
    assert classify_exception(req_exc.ConnectTimeout("x")).code == ErrorCode.UPSTREAM_TIMEOUT
    assert classify_exception(req_exc.InvalidURL("x")).retryable is False
    unexpected = classify_exception(RuntimeError("boom"))
    assert unexpected.code == ErrorCode.UPSTREAM_ERROR
    assert unexpected.details["reason"] == "unexpected"
    assert "boom" not in unexpected.message


async def test_transcript_size_limits(executor: ThreadPoolExecutor) -> None:
    many = FakeTranscript("en", False, [Snippet("x", i, 1.0) for i in range(11)])
    provider = make_provider(executor, shared_errors([], [many]), max_segments=10)
    with pytest.raises(TubeTraceError) as info:
        await provider.fetch_transcript(VID, lambda ts: ts[0])
    assert info.value.code == ErrorCode.TRANSCRIPT_TOO_LARGE

    big = FakeTranscript("en", False, [Snippet("y" * 600, 0.0, 1.0), Snippet("y" * 600, 1.0, 1.0)])
    provider = make_provider(executor, shared_errors([], [big]), max_bytes=1000)
    with pytest.raises(TubeTraceError) as info:
        await provider.fetch_transcript(VID, lambda ts: ts[0])
    assert info.value.code == ErrorCode.TRANSCRIPT_TOO_LARGE


async def test_selector_errors_propagate_unchanged(executor: ThreadPoolExecutor) -> None:
    provider = make_provider(executor, shared_errors([], [FakeTranscript("en", False, [])]))

    def select(tracks: Sequence[TranscriptTrack]) -> TranscriptTrack:
        raise TubeTraceError(ErrorCode.NO_MATCHING_TRANSCRIPT, "nope")

    with pytest.raises(TubeTraceError) as info:
        await provider.fetch_transcript(VID, select)
    assert info.value.code == ErrorCode.NO_MATCHING_TRANSCRIPT


def test_timeout_session_applies_timeouts() -> None:
    captured: dict[str, Any] = {}

    class Adapter(requests.adapters.BaseAdapter):
        def send(
            self,
            request: Any,
            stream: bool = False,
            timeout: Any = None,
            verify: Any = True,
            cert: Any = None,
            proxies: Any = None,
        ) -> requests.Response:
            captured["timeout"] = timeout
            response = requests.Response()
            response.status_code = 200
            response._content = b"{}"
            response.request = request
            return response

        def close(self) -> None:
            pass

    session = TimeoutSession(1.5, 7.0)
    session.mount("https://", Adapter())
    session.get("https://example.invalid/")
    assert captured["timeout"] == (1.5, 7.0)
    session.get("https://example.invalid/", timeout=3)
    assert captured["timeout"] == 3
