"""Process-wide logging: one line format for everything the server writes.

This module is shared verbatim by rabotaua-mcp and tubetrace-mcp; keep both copies
identical.

``configure_logging`` installs a single stderr handler on the root logger and routes
the uvicorn, FastMCP and MCP SDK loggers through it, so every line has the same shape:
``text`` (``<time> <LEVEL> <logger> <message> key=value ...``, the default) or ``json``
(one object per line). Secret redaction runs on the fully rendered line, including
exception text. The Prefect Horizon "Logs" view shows the raw stdout/stderr of the
server process, so this is exactly what appears there.

Prefect Horizon starts a server with ``fastmcp run <entrypoint> --transport http``.
That command imports the entrypoint first and only afterwards creates
``uvicorn.Config``, which applies uvicorn's default ``LOGGING_CONFIG``: it re-installs
uvicorn's own handlers, sets ``propagate=False`` and re-enables ``uvicorn.access``.
Configuring uvicorn's loggers at import time is therefore not enough, so
``configure_logging`` also rewrites the ``loggers`` section of that default in place.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import re
import sys
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any, Literal

import fastmcp

LogFormat = Literal["text", "json"]
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "mcp_request_id", default=None
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
    # user:password@ credentials embedded in URLs (for example a proxy URL)
    re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://[^\s:/@]+:)[^\s/@]+(@)"),
)

# A text value is written bare when it cannot be confused with the key=value layout.
_BARE_VALUE = re.compile(r"^[^\s\"=]+$")
# Raised to WARNING. The MCP SDK's streamable_http logs only "Terminating session: None"
# at INFO (on Horizon once per cold start); everything else it logs is an error.
_NOISY_LOGGERS = (
    "httpx",
    "httpx2",
    "httpcore",
    "httpcore2",
    "urllib3",
    "hpack",
    "mcp.server.streamable_http",
)
_ROUTED_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access", "fastmcp", "FastMCP")


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
        text = _GENERIC_PATTERNS[3].sub(r"\1[REDACTED]\2", text)
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


def _timestamp(record: logging.LogRecord) -> str:
    moment = datetime.fromtimestamp(record.created, tz=UTC)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _fields(record: logging.LogRecord) -> dict[str, Any]:
    """``extra=`` fields of a record (``None`` values dropped), ``request_id`` last."""
    fields: dict[str, Any] = {
        key: value
        for key, value in record.__dict__.items()
        if key not in _STANDARD_ATTRS and not key.startswith("_") and value is not None
    }
    request_id = fields.pop("request_id", None) or request_id_var.get()
    if request_id:
        fields["request_id"] = request_id
    return fields


def _text_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, dict | list | tuple):
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    else:
        text = str(value)
    return text if _BARE_VALUE.match(text) else json.dumps(text, ensure_ascii=False)


class JsonFormatter(RedactingFormatter):
    """One JSON object per line: ``time``, ``level``, ``logger``, ``message``, fields."""

    def render(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "time": _timestamp(record),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        payload.update(_fields(record))
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


class TextFormatter(RedactingFormatter):
    """``<time> <LEVEL> <logger> <message> key=value ...``; tracebacks follow on new lines."""

    def render(self, record: logging.LogRecord) -> str:
        parts = [_timestamp(record), f"{record.levelname:<7}", record.name, record.getMessage()]
        parts.extend(f"{key}={_text_value(value)}" for key, value in _fields(record).items())
        line = " ".join(parts)
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


class DropLoopbackAccessLog(logging.Filter):
    """Drop uvicorn access lines whose peer is a loopback address.

    Behind Prefect Horizon every request reaches uvicorn from the AWS Lambda Web
    Adapter on 127.0.0.1 (including its ``GET /`` readiness probe), so these lines carry
    no caller information; the per-request audit line replaces them. Lines for real
    peers (a self-hosted server behind a reverse proxy trusted by uvicorn) are kept.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and args and isinstance(args[0], str):
            return not args[0].startswith(("127.", "::1"))
        return True


def _route_uvicorn_default_config_through_root() -> None:
    """Make uvicorn's *default* dictConfig keep its loggers routed through root.

    ``uvicorn.Config`` applies ``uvicorn.config.LOGGING_CONFIG`` when it is created,
    which under ``fastmcp run`` (Prefect Horizon) happens after the entrypoint module
    ran. Logger filters survive that dictConfig; handlers and ``propagate`` do not.
    """
    try:
        from uvicorn.config import LOGGING_CONFIG
    except ImportError:  # pragma: no cover - uvicorn is a FastMCP dependency
        return
    LOGGING_CONFIG["loggers"] = {
        "uvicorn": {"handlers": [], "level": "INFO", "propagate": True},
        "uvicorn.error": {"level": "INFO", "propagate": True},
        "uvicorn.access": {"handlers": [], "level": "INFO", "propagate": True},
    }


def configure_logging(
    level: str = "INFO",
    log_format: LogFormat = "text",
    *,
    secrets: Iterable[str] = (),
) -> None:
    """Configure the root logger once; safe to call repeatedly."""
    for secret in secrets:
        _redactor.add_secret(secret)
    level = level.upper()
    formatter: RedactingFormatter = (
        JsonFormatter(_redactor) if log_format == "json" else TextFormatter(_redactor)
    )
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(formatter)
    # The handler level also caps loggers whose level uvicorn sets explicitly.
    handler.setLevel(level)
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    for noisy in _NOISY_LOGGERS:
        logging.getLogger(noisy).setLevel(logging.WARNING)
    # uvicorn and FastMCP loggers propagate to root so they share the redacting formatter
    # (FastMCP otherwise installs its own Rich handler that bypasses redaction).
    for name in _ROUTED_LOGGERS:
        other = logging.getLogger(name)
        other.handlers[:] = []
        other.propagate = True
        other.setLevel(logging.NOTSET)
        other.disabled = False
    access = logging.getLogger("uvicorn.access")
    for existing in [f for f in access.filters if isinstance(f, DropLoopbackAccessLog)]:
        access.removeFilter(existing)
    if level != "DEBUG":
        access.addFilter(DropLoopbackAccessLog())
    _route_uvicorn_default_config_through_root()
    # FastMCP must not re-install its Rich handler later (``fastmcp run --log-level`` or
    # ``deployment.log_level`` in fastmcp.json), and its startup banner is a Rich panel
    # that breaks the line format and makes a PyPI update check on every cold start.
    fastmcp.settings.log_enabled = False
    fastmcp.settings.show_server_banner = False


def configure_logging_from_env(*, secrets: Iterable[str] = ()) -> None:
    """``configure_logging`` from ``LOG_LEVEL`` (default INFO) and ``LOG_FORMAT`` (default text).

    Invalid values raise ``ValueError``, which fails the Horizon build step
    (``fastmcp inspect``) instead of silently falling back.
    """
    level = os.environ.get("LOG_LEVEL", "").strip().upper() or "INFO"
    log_format = os.environ.get("LOG_FORMAT", "").strip().lower() or "text"
    if level not in LOG_LEVELS:
        raise ValueError(f"LOG_LEVEL must be one of {', '.join(LOG_LEVELS)}, got {level!r}")
    if log_format not in ("text", "json"):
        raise ValueError(f"LOG_FORMAT must be 'text' or 'json', got {log_format!r}")
    configure_logging(level, "json" if log_format == "json" else "text", secrets=secrets)
