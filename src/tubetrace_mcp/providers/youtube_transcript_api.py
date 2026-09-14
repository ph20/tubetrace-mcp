"""Transcript provider backed by the unofficial ``youtube-transcript-api`` library.

The library is synchronous and uses ``requests``. Each call runs in a bounded
thread pool with a fresh ``requests.Session`` that enforces connect/read
timeouts, so no session is shared between threads. Retries are bounded, use
exponential backoff with jitter, honour ``Retry-After`` and a total time budget,
and are attempted only for transient failures.

An optional HTTP(S) proxy (``TRANSCRIPT_PROXY_URL``) is applied to these sessions
only; the Google search client never sees it. With a proxy configured, a YouTube
block is treated as retryable: every attempt opens a fresh session and therefore a
new proxy connection, which a rotating proxy serves from a different exit IP.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import requests
from requests import exceptions as req_exc
from youtube_transcript_api import (
    AgeRestricted,
    CouldNotRetrieveTranscript,
    FailedToCreateConsentCookie,
    InvalidVideoId,
    IpBlocked,
    NoTranscriptFound,
    PoTokenRequired,
    RequestBlocked,
    TranscriptsDisabled,
    VideoUnavailable,
    VideoUnplayable,
    YouTubeDataUnparsable,
    YouTubeRequestFailed,
    YouTubeTranscriptApi,
    YouTubeTranscriptApiException,
)

from ..errors import ErrorCode, TubeTraceError
from ..retry import backoff_delay
from ..schemas import TranscriptTrack
from .base import RawSegment, RawTranscript, TrackSelector

logger = logging.getLogger(__name__)

PROVIDER_NAME = "youtube_transcript_api"

BLOCK_HINT = (
    "YouTube refused the request from this server's network (common for cloud/datacenter "
    "IP addresses). Verify that the transcript provider works from this network, or "
    "configure TRANSCRIPT_PROXY_URL; this server does not solve CAPTCHAs."
)

PROXY_BLOCK_HINT = (
    "The request went through the configured transcript proxy, so YouTube blocked the "
    "proxy's exit IP. A retry may be served from a different exit IP."
)


class TimeoutSession(requests.Session):
    """``requests.Session`` that applies connect/read timeouts to every request.

    Session-level proxies are also passed per request, because ``requests`` otherwise
    lets ``HTTP(S)_PROXY`` environment variables take precedence over ``Session.proxies``.
    """

    def __init__(
        self, connect_timeout: float, read_timeout: float, proxy_url: str | None = None
    ) -> None:
        super().__init__()
        self._timeout = (connect_timeout, read_timeout)
        if proxy_url:
            self.proxies.update(proxy_dict(proxy_url))

    def request(self, *args: Any, **kwargs: Any) -> requests.Response:
        kwargs.setdefault("timeout", self._timeout)
        if self.proxies:
            kwargs.setdefault("proxies", dict(self.proxies))
        return super().request(*args, **kwargs)


def proxy_dict(proxy_url: str) -> dict[str, str]:
    """The ``requests`` proxy mapping that routes both HTTP and HTTPS through one proxy."""
    return {"http": proxy_url, "https": proxy_url}


TranscriptApiFactory = Callable[[requests.Session], Any]
SessionFactory = Callable[[], requests.Session]


def _default_api_factory(session: requests.Session) -> Any:
    return YouTubeTranscriptApi(http_client=session)


class YouTubeTranscriptApiProvider:
    """Real provider implementation (see module docstring)."""

    name = PROVIDER_NAME

    @property
    def uses_proxy(self) -> bool:
        return self._proxy_url is not None

    def __init__(
        self,
        *,
        executor: ThreadPoolExecutor,
        max_concurrency: int,
        queue_timeout_seconds: float,
        connect_timeout_seconds: float,
        read_timeout_seconds: float,
        max_retries: int,
        retry_budget_seconds: float,
        max_segments: int,
        max_bytes: int,
        proxy_url: str | None = None,
        api_factory: TranscriptApiFactory | None = None,
        session_factory: SessionFactory | None = None,
        sleep: Callable[[float], Any] = asyncio.sleep,
        rng: Callable[[], float] = random.random,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._executor = executor
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._queue_timeout = queue_timeout_seconds
        self._max_retries = max_retries
        self._retry_budget = retry_budget_seconds
        self._max_segments = max_segments
        self._max_bytes = max_bytes
        self._proxy_url = proxy_url or None
        self._api_factory = api_factory or _default_api_factory
        self._session_factory = session_factory or (
            lambda: TimeoutSession(connect_timeout_seconds, read_timeout_seconds, self._proxy_url)
        )
        self._sleep = sleep
        self._rng = rng
        self._clock = clock

    # ------------------------------------------------------------------ public API
    async def list_transcripts(self, video_id: str) -> list[TranscriptTrack]:
        return await self._call(video_id, "list", lambda api: _list_sync(api, video_id))

    async def fetch_transcript(self, video_id: str, select: TrackSelector) -> RawTranscript:
        return await self._call(
            video_id,
            "fetch",
            lambda api: _fetch_sync(
                api, video_id, select, max_segments=self._max_segments, max_bytes=self._max_bytes
            ),
        )

    # -------------------------------------------------------------------- internals
    async def _call[T](self, video_id: str, operation: str, fn: Callable[[Any], T]) -> T:
        deadline = self._clock() + self._retry_budget
        attempt = 0
        while True:
            try:
                return await self._run_once(fn)
            except TubeTraceError as err:
                if not err.retryable or attempt >= self._max_retries:
                    raise
                delay = backoff_delay(attempt, retry_after=err.retry_after_seconds, rng=self._rng)
                if self._clock() + delay > deadline:
                    raise
                logger.info(
                    "transcript_retry",
                    extra={
                        "provider": self.name,
                        "operation": operation,
                        "video_id": video_id,
                        "attempt": attempt + 1,
                        "error_code": str(err.code),
                        "delay_seconds": round(delay, 3),
                        "via_proxy": self.uses_proxy,
                    },
                )
                await self._sleep(delay)
                attempt += 1

    async def _run_once[T](self, fn: Callable[[Any], T]) -> T:
        try:
            await asyncio.wait_for(self._semaphore.acquire(), timeout=self._queue_timeout)
        except TimeoutError:
            raise TubeTraceError(
                ErrorCode.SERVER_BUSY,
                "Too many concurrent transcript requests; try again shortly.",
                retryable=True,
                retry_after_seconds=1.0,
            ) from None
        try:
            loop = asyncio.get_running_loop()
            result: T = await loop.run_in_executor(self._executor, self._guarded, fn)
            return result
        finally:
            self._semaphore.release()

    def _guarded[T](self, fn: Callable[[Any], T]) -> T:
        """Runs in a worker thread with its own session (never shared between threads)."""
        session = self._session_factory()
        if self._proxy_url is not None:
            # Enforced here as well, so a custom session factory cannot bypass the proxy.
            session.proxies.update(proxy_dict(self._proxy_url))
        try:
            api = self._api_factory(session)
            try:
                return fn(api)
            except TubeTraceError:
                raise
            except Exception as exc:
                mapped = classify_exception(exc, proxy_configured=self.uses_proxy)
                if mapped.code == ErrorCode.UPSTREAM_ERROR and mapped.details.get("reason") in {
                    "unexpected",
                    "unparsable_response",
                }:
                    logger.error(
                        "transcript_provider_failure",
                        extra={"provider": self.name, "error_type": type(exc).__name__},
                    )
                raise mapped from exc
        finally:
            session.close()


# ------------------------------------------------------------------ sync helpers


def _track_from(transcript: Any) -> TranscriptTrack:
    return TranscriptTrack(
        language=str(transcript.language),
        language_code=str(transcript.language_code),
        is_generated=bool(transcript.is_generated),
        is_translatable=bool(getattr(transcript, "is_translatable", False)),
    )


def _list_sync(api: Any, video_id: str) -> list[TranscriptTrack]:
    transcript_list = api.list(video_id)
    return [_track_from(t) for t in transcript_list]


def _fetch_sync(
    api: Any,
    video_id: str,
    select: TrackSelector,
    *,
    max_segments: int,
    max_bytes: int,
) -> RawTranscript:
    transcript_list = api.list(video_id)
    available = list(transcript_list)
    tracks = [_track_from(t) for t in available]
    chosen = select(tracks)
    for transcript, track in zip(available, tracks, strict=True):
        if track.key == chosen.key:
            fetched = transcript.fetch()
            break
    else:  # pragma: no cover - selector must return one of the given tracks
        raise TubeTraceError(
            ErrorCode.NO_MATCHING_TRANSCRIPT, "Selected transcript track is not available."
        )

    snippets = list(fetched.snippets)
    if len(snippets) > max_segments:
        raise TubeTraceError(
            ErrorCode.TRANSCRIPT_TOO_LARGE,
            "The transcript has more segments than this server is configured to handle.",
            details={"segments": len(snippets), "max_segments": max_segments},
        )
    total_bytes = 0
    segments: list[RawSegment] = []
    for snippet in snippets:
        text = str(snippet.text)
        total_bytes += len(text.encode("utf-8"))
        if total_bytes > max_bytes:
            raise TubeTraceError(
                ErrorCode.TRANSCRIPT_TOO_LARGE,
                "The transcript text is larger than this server is configured to handle.",
                details={"max_bytes": max_bytes},
            )
        segments.append(
            RawSegment(text=text, start=float(snippet.start), duration=float(snippet.duration))
        )
    return RawTranscript(
        video_id=video_id,
        language=str(fetched.language),
        language_code=str(fetched.language_code),
        is_generated=bool(fetched.is_generated),
        segments=tuple(segments),
    )


_HTTP_STATUS_RE = re.compile(r"^(\d{3}) (?:Client|Server) Error")


def _http_status(exc: YouTubeRequestFailed) -> int | None:
    """Extract the HTTP status from the library's error text.

    ``YouTubeRequestFailed`` keeps only ``str(requests.HTTPError)`` (for example
    ``"429 Client Error: Too Many Requests for url: ..."``); the response object and
    its ``Retry-After`` header are not available.
    """
    reason = getattr(exc, "reason", None)
    if not isinstance(reason, str):
        return None
    match = _HTTP_STATUS_RE.match(reason.strip())
    return int(match.group(1)) if match else None


def classify_exception(exc: Exception, *, proxy_configured: bool = False) -> TubeTraceError:
    """Map library and network exceptions onto stable error codes.

    ``proxy_configured`` makes YouTube blocks retryable (a new attempt uses a new proxy
    connection) and adjusts the diagnostic hint.
    """
    if isinstance(exc, TranscriptsDisabled):
        return TubeTraceError(
            ErrorCode.TRANSCRIPTS_DISABLED,
            "Subtitles are disabled for this video according to the transcript provider.",
        )
    if isinstance(exc, NoTranscriptFound):
        return TubeTraceError(
            ErrorCode.NO_MATCHING_TRANSCRIPT,
            "No transcript matches the requested languages.",
        )
    if isinstance(exc, IpBlocked | RequestBlocked):
        reason = "ip_blocked" if isinstance(exc, IpBlocked) else "request_blocked"
        return TubeTraceError(
            ErrorCode.UPSTREAM_BLOCKED,
            "The transcript provider was blocked by YouTube. "
            + (PROXY_BLOCK_HINT if proxy_configured else BLOCK_HINT),
            retryable=proxy_configured,
            details={"reason": reason, "via_proxy": proxy_configured},
        )
    if isinstance(exc, PoTokenRequired):
        return TubeTraceError(
            ErrorCode.UPSTREAM_BLOCKED,
            "YouTube requires a proof-of-origin token for this request; the provider cannot "
            "fetch this transcript from this server. "
            + (PROXY_BLOCK_HINT if proxy_configured else BLOCK_HINT),
            retryable=proxy_configured,
            details={"reason": "po_token_required", "via_proxy": proxy_configured},
        )
    if isinstance(exc, AgeRestricted):
        return TubeTraceError(
            ErrorCode.VIDEO_UNAVAILABLE,
            "The video is age-restricted; transcripts cannot be retrieved without sign-in.",
            details={"reason": "age_restricted"},
        )
    if isinstance(exc, VideoUnplayable):
        details: dict[str, Any] = {"reason": "unplayable"}
        reason_text = getattr(exc, "reason", None)
        if isinstance(reason_text, str) and reason_text:
            details["youtube_reason"] = reason_text[:200]
        return TubeTraceError(
            ErrorCode.VIDEO_UNAVAILABLE, "The video is not playable.", details=details
        )
    if isinstance(exc, VideoUnavailable | InvalidVideoId):
        return TubeTraceError(
            ErrorCode.VIDEO_UNAVAILABLE,
            "The video is unavailable (removed, private, or the ID does not exist).",
            details={"reason": "unavailable"},
        )
    if isinstance(exc, YouTubeRequestFailed):
        status = _http_status(exc)
        if status == 429:
            return TubeTraceError(
                ErrorCode.UPSTREAM_RATE_LIMITED,
                "YouTube rate-limited the transcript provider.",
                retryable=True,
                details={"status": status},
                retry_after_seconds=None,
            )
        if status is not None and status >= 500:
            return TubeTraceError(
                ErrorCode.UPSTREAM_ERROR,
                "YouTube returned a server error to the transcript provider.",
                retryable=True,
                details={"status": status},
            )
        return TubeTraceError(
            ErrorCode.UPSTREAM_ERROR,
            "YouTube rejected the transcript request.",
            details={"status": status} if status is not None else {"reason": "http_error"},
        )
    if isinstance(exc, YouTubeDataUnparsable | FailedToCreateConsentCookie):
        return TubeTraceError(
            ErrorCode.UPSTREAM_ERROR,
            "The transcript provider could not parse YouTube's response. This may indicate a "
            "YouTube change or soft blocking; it does not mean the video has no subtitles.",
            details={"reason": "unparsable_response"},
        )
    if isinstance(exc, CouldNotRetrieveTranscript | YouTubeTranscriptApiException):
        return TubeTraceError(
            ErrorCode.UPSTREAM_ERROR,
            "The transcript provider failed to retrieve the transcript.",
            details={"reason": type(exc).__name__},
        )
    if isinstance(exc, req_exc.Timeout):
        return TubeTraceError(
            ErrorCode.UPSTREAM_TIMEOUT,
            "Timed out while contacting YouTube for transcripts.",
            retryable=True,
        )
    if isinstance(exc, req_exc.ProxyError):
        # Checked before ConnectionError (its base class). The exception text is not copied
        # into the result because it can contain the proxy address.
        if "407" in str(exc):
            return TubeTraceError(
                ErrorCode.UPSTREAM_ERROR,
                "The transcript proxy rejected the configured credentials "
                "(407 Proxy Authentication Required); check TRANSCRIPT_PROXY_URL.",
                details={"reason": "proxy_auth_failed"},
            )
        return TubeTraceError(
            ErrorCode.UPSTREAM_ERROR,
            "Could not reach YouTube through the configured transcript proxy.",
            retryable=True,
            details={"reason": "proxy_error"},
        )
    if isinstance(exc, req_exc.ConnectionError):
        return TubeTraceError(
            ErrorCode.UPSTREAM_ERROR,
            "Network error while contacting YouTube for transcripts.",
            retryable=True,
            details={"reason": "connection_error"},
        )
    if isinstance(exc, req_exc.RequestException):
        return TubeTraceError(
            ErrorCode.UPSTREAM_ERROR,
            "HTTP error while contacting YouTube for transcripts.",
            details={"reason": type(exc).__name__},
        )
    return TubeTraceError(
        ErrorCode.UPSTREAM_ERROR,
        "Unexpected transcript provider failure.",
        details={"reason": "unexpected"},
    )


__all__ = [
    "PROVIDER_NAME",
    "TimeoutSession",
    "YouTubeTranscriptApiProvider",
    "classify_exception",
    "proxy_dict",
]
