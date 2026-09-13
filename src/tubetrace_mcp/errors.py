"""Domain errors with stable, machine-readable codes.

Every expected failure inside TubeTrace is raised as :class:`TubeTraceError`.
The MCP tool layer converts it into an MCP tool result with ``isError=true`` and
a JSON payload ``{"error": {"code", "message", "retryable", "details"}}``.
Messages must never contain secrets (API keys, bearer tokens, full upstream
URLs with credentials).
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    """Stable error codes exposed to MCP clients."""

    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    INVALID_VIDEO_INPUT = "INVALID_VIDEO_INPUT"
    GOOGLE_API_NOT_CONFIGURED = "GOOGLE_API_NOT_CONFIGURED"
    GOOGLE_API_KEY_INVALID = "GOOGLE_API_KEY_INVALID"
    GOOGLE_QUOTA_EXCEEDED = "GOOGLE_QUOTA_EXCEEDED"
    NO_MATCHING_TRANSCRIPT = "NO_MATCHING_TRANSCRIPT"
    TRANSCRIPTS_DISABLED = "TRANSCRIPTS_DISABLED"
    VIDEO_UNAVAILABLE = "VIDEO_UNAVAILABLE"
    TRANSCRIPT_TOO_LARGE = "TRANSCRIPT_TOO_LARGE"
    RESPONSE_TOO_LARGE = "RESPONSE_TOO_LARGE"
    RATE_LIMITED = "RATE_LIMITED"
    SERVER_BUSY = "SERVER_BUSY"
    UPSTREAM_BLOCKED = "UPSTREAM_BLOCKED"
    UPSTREAM_RATE_LIMITED = "UPSTREAM_RATE_LIMITED"
    UPSTREAM_TIMEOUT = "UPSTREAM_TIMEOUT"
    UPSTREAM_ERROR = "UPSTREAM_ERROR"


class TubeTraceError(Exception):
    """An expected, client-visible failure.

    Args:
        code: Stable machine-readable error code.
        message: Safe human-readable message (no secrets).
        retryable: Whether the same call may succeed if retried later.
        details: Optional non-secret structured details.
        retry_after_seconds: Optional hint for how long to wait before retrying.
    """

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        retryable: bool = False,
        details: Mapping[str, Any] | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details: dict[str, Any] = dict(details) if details else {}
        self.retry_after_seconds = retry_after_seconds

    def to_dict(self) -> dict[str, Any]:
        """Serialize into the public error payload."""
        payload: dict[str, Any] = {
            "code": str(self.code),
            "message": self.message,
            "retryable": self.retryable,
        }
        details = dict(self.details)
        if self.retry_after_seconds is not None:
            details["retry_after_seconds"] = round(self.retry_after_seconds, 3)
        if details:
            payload["details"] = details
        return payload

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"TubeTraceError(code={self.code!s}, message={self.message!r})"
