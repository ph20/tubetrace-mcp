from __future__ import annotations

import json
import logging
import re
import sys

import fastmcp
import pytest
import uvicorn
from fastmcp.utilities.logging import configure_logging as fastmcp_configure_logging

from tubetrace_mcp.logging_config import (
    DropLoopbackAccessLog,
    DropUvicornLifecycleLog,
    JsonFormatter,
    SecretRedactor,
    TextFormatter,
    configure_logging,
    configure_logging_from_env,
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
    assert logging.getLogger("mcp.server.streamable_http").level == logging.WARNING
    assert logging.getLogger("mcp.server.streamable_http_manager").level == logging.WARNING


def test_url_credentials_are_redacted() -> None:
    redactor = SecretRedactor()
    text = "ProxyError: Unable to connect to proxy http://customer-user:pw1@pr.oxylabs.io:7777/"
    redacted = redactor.redact(text)
    assert "pw1" not in redacted
    assert "http://customer-user:[REDACTED]@pr.oxylabs.io:7777/" in redacted


def test_uvicorn_config_created_after_configure_keeps_routing() -> None:
    """``fastmcp run`` (Prefect Horizon) creates uvicorn.Config after the entrypoint ran."""
    configure_logging("INFO", "text")
    uvicorn.Config(app=lambda *_: None, log_level="info")  # applies uvicorn's LOGGING_CONFIG
    fastmcp_configure_logging("INFO")  # what `fastmcp run --log-level` does
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "fastmcp"):
        logger = logging.getLogger(name)
        assert logger.handlers == [], name
        assert logger.propagate is True, name
        assert logger.disabled is False, name
    assert fastmcp.settings.show_server_banner is False
    access_filters = logging.getLogger("uvicorn.access").filters
    assert [type(f) for f in access_filters] == [DropLoopbackAccessLog]
    error_filters = logging.getLogger("uvicorn.error").filters
    assert [type(f) for f in error_filters] == [DropUvicornLifecycleLog]


def test_loopback_access_lines_are_dropped_real_peers_kept() -> None:
    def access(peer: str) -> logging.LogRecord:
        return logging.LogRecord(
            "uvicorn.access",
            logging.INFO,
            __file__,
            1,
            '%s - "%s %s HTTP/%s" %d',
            (peer, "POST", "/mcp", "1.1", 200),
            None,
        )

    drop = DropLoopbackAccessLog()
    assert drop.filter(access("127.0.0.1:44008")) is False
    assert drop.filter(access("::1:44008")) is False
    assert drop.filter(access("203.0.113.7:51234")) is True


def test_text_formatter_layout_quotes_values_and_skips_none() -> None:
    formatter = TextFormatter(SecretRedactor())
    record = logging.LogRecord("audit", logging.INFO, __file__, 1, "mcp_request", None, None)
    record.user = "agrynchuk@gmail.com"
    record.ua = "Mozilla/5.0 (X11; Linux)"
    record.ip = None
    record.cold_start = True
    record.request_id = "4bf92f3577b34da6a3ce929d0e0e4736"
    line = formatter.format(record)
    assert re.fullmatch(
        r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z INFO    audit mcp_request "
        r'user=agrynchuk@gmail\.com ua="Mozilla/5\.0 \(X11; Linux\)" cold_start=true '
        r"request_id=4bf92f3577b34da6a3ce929d0e0e4736",
        line,
    ), line


def test_configure_logging_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOG_FORMAT", "JSON")
    monkeypatch.setenv("LOG_LEVEL", "warning")
    configure_logging_from_env()
    root = logging.getLogger()
    assert isinstance(root.handlers[0].formatter, JsonFormatter)
    assert root.level == logging.WARNING
    monkeypatch.setenv("LOG_FORMAT", "yaml")
    with pytest.raises(ValueError, match="LOG_FORMAT"):
        configure_logging_from_env()


def test_uvicorn_lifecycle_info_is_hidden_but_its_warnings_are_not(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """uvicorn logs start/stop at INFO on "uvicorn.error", which reads like an error."""
    configure_logging("INFO", "text")
    uvicorn.Config(app=lambda *_: None, log_level="info")  # what `fastmcp run` does later
    server_log = logging.getLogger("uvicorn.error")
    server_log.info("Application startup complete.")
    server_log.warning("Invalid HTTP request received.")
    err = capsys.readouterr().err
    assert "Application startup complete." not in err
    assert "WARNING uvicorn.error Invalid HTTP request received." in err

    configure_logging("DEBUG", "text")
    logging.getLogger("uvicorn.error").info("Application startup complete.")
    assert "Application startup complete." in capsys.readouterr().err, "DEBUG shows everything"
