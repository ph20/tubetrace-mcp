"""Structured (JSON) logging with secret redaction.

Redaction runs on the fully formatted record (message, extras and exception
text) so that API keys, bearer tokens and credential-bearing query strings never
reach log output even when they appear inside third-party exception messages.
"""

from __future__ import annotations

import contextvars
import json
import logging
import re
import sys
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any, Literal

request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "tubetrace_request_id", default=None
)

_STANDARD_ATTRS = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "message",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "thread",
        "threadName",
        "taskName",
        "color_message",
    }
)

_GENERIC_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"AIza[0-9A-Za-z_\-]{20,}"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9\-._~+/]+=*"),
    re.compile(r"(?i)([?&](?:key|token|access_token|api_key|apikey)=)[^&\s\"']+"),
)


class SecretRedactor:
    """Replace known secret values and credential-like patterns with ``[REDACTED]``."""

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        self._secrets: list[str] = sorted(
            {s for s in secrets if s and len(s) >= 8}, key=len, reverse=True
        )

    def add_secret(self, value: str | None) -> None:
        if value and len(value) >= 8 and value not in self._secrets:
            self._secrets.append(value)
            self._secrets.sort(key=len, reverse=True)

    def redact(self, text: str) -> str:
        for secret in self._secrets:
            if secret in text:
                text = text.replace(secret, "[REDACTED]")
        text = _GENERIC_PATTERNS[0].sub("[REDACTED]", text)
        text = _GENERIC_PATTERNS[1].sub(r"\1[REDACTED]", text)
        text = _GENERIC_PATTERNS[2].sub(r"\1[REDACTED]", text)
        return text


_redactor = SecretRedactor()


def get_redactor() -> SecretRedactor:
    return _redactor


class RedactingFormatter(logging.Formatter):
    """Base formatter applying redaction to the final rendered line."""

    def __init__(self, redactor: SecretRedactor) -> None:
        super().__init__()
        self._redactor = redactor

    def format(self, record: logging.LogRecord) -> str:
        rendered = self.render(record)
        return self._redactor.redact(rendered)

    def render(self, record: logging.LogRecord) -> str:  # pragma: no cover - abstract
        raise NotImplementedError


def _extra_fields(record: logging.LogRecord) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for key, value in record.__dict__.items():
        if key in _STANDARD_ATTRS or key.startswith("_"):
            continue
        fields[key] = value
    return fields


class JsonFormatter(RedactingFormatter):
    def render(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "time": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        request_id = getattr(record, "request_id", None) or request_id_var.get()
        if request_id:
            payload["request_id"] = request_id
        payload.update(_extra_fields(record))
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


class TextFormatter(RedactingFormatter):
    def render(self, record: logging.LogRecord) -> str:
        extras = _extra_fields(record)
        request_id = extras.pop("request_id", None) or request_id_var.get()
        parts = [
            datetime.fromtimestamp(record.created, tz=UTC).strftime("%H:%M:%S"),
            record.levelname,
            record.name,
            record.getMessage(),
        ]
        if request_id:
            parts.append(f"request_id={request_id}")
        parts.extend(f"{key}={value}" for key, value in extras.items())
        line = " ".join(str(p) for p in parts)
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


def configure_logging(
    level: str = "INFO",
    log_format: Literal["json", "text"] = "json",
    *,
    secrets: Iterable[str] = (),
) -> None:
    """Configure the root logger once; safe to call repeatedly."""
    for secret in secrets:
        _redactor.add_secret(secret)
    formatter: RedactingFormatter = (
        JsonFormatter(_redactor) if log_format == "json" else TextFormatter(_redactor)
    )
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    for noisy in ("httpx", "httpx2", "httpcore", "urllib3", "hpack"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    # uvicorn and FastMCP loggers propagate to root so they share the redacting formatter
    # (FastMCP otherwise installs its own rich handler that bypasses redaction).
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "fastmcp", "FastMCP"):
        other = logging.getLogger(name)
        other.handlers[:] = []
        other.propagate = True
        other.setLevel(logging.NOTSET)
