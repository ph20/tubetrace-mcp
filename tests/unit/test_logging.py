from __future__ import annotations

import json
import logging
import sys

from tubetrace_mcp.logging_config import (
    JsonFormatter,
    SecretRedactor,
    TextFormatter,
    configure_logging,
    request_id_var,
)

SECRET = "AIzaSyVerySecretKeyValue123456789"


def test_redactor_masks_secrets_and_patterns() -> None:
    redactor = SecretRedactor([SECRET, "short"])
    assert redactor.redact(f"key={SECRET}") == "key=[REDACTED]"
    assert (
        redactor.redact("Authorization: Bearer abc.def-123") == "Authorization: Bearer [REDACTED]"
    )
    assert redactor.redact("GET /mcp?token=supersecret&x=1") == "GET /mcp?token=[REDACTED]&x=1"
    assert redactor.redact("short") == "short", "tiny values are not treated as secrets"


def test_json_formatter_includes_extras_request_id_and_redacts_exceptions() -> None:
    formatter = JsonFormatter(SecretRedactor([SECRET]))
    token = request_id_var.set("req-42")
    try:
        try:
            raise RuntimeError(f"failed with {SECRET}")
        except RuntimeError:
            record = logging.LogRecord(
                "tubetrace", logging.INFO, __file__, 1, "tool_call", None, exc_info=sys.exc_info()
            )
        record.tool = "youtube_get_transcript"
        record.latency_ms = 12.5
        record.cache_hit = True
        rendered = formatter.format(record)
    finally:
        request_id_var.reset(token)
    payload = json.loads(rendered)
    assert payload["message"] == "tool_call"
    assert payload["request_id"] == "req-42"
    assert payload["tool"] == "youtube_get_transcript"
    assert payload["latency_ms"] == 12.5
    assert payload["cache_hit"] is True
    assert SECRET not in rendered
    assert "[REDACTED]" in payload["exception"]


def test_text_formatter() -> None:
    formatter = TextFormatter(SecretRedactor([SECRET]))
    record = logging.LogRecord("t", logging.WARNING, __file__, 1, f"key {SECRET}", None, None)
    record.error_code = "UPSTREAM_ERROR"
    line = formatter.format(record)
    assert "error_code=UPSTREAM_ERROR" in line
    assert SECRET not in line


def test_configure_logging_routes_fastmcp_and_uvicorn_through_root() -> None:
    configure_logging("INFO", "json", secrets=[SECRET])
    root = logging.getLogger()
    assert len(root.handlers) == 1
    assert isinstance(root.handlers[0].formatter, JsonFormatter)
    for name in ("fastmcp", "uvicorn.access"):
        assert logging.getLogger(name).handlers == []
        assert logging.getLogger(name).propagate is True
    assert logging.getLogger("httpx").level == logging.WARNING


def test_url_credentials_are_redacted() -> None:
    redactor = SecretRedactor()
    text = "ProxyError: Unable to connect to proxy http://customer-user:pw1@pr.oxylabs.io:7777/"
    redacted = redactor.redact(text)
    assert "pw1" not in redacted
    assert "http://customer-user:[REDACTED]@pr.oxylabs.io:7777/" in redacted
