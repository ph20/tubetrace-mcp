"""The per-request audit line: caller identity from the Prefect Horizon gateway headers.

Requests go over a real local HTTP transport so the middleware sees real headers, the
way it does behind the Horizon gateway (values below were observed on a live
deployment).
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from collections.abc import Iterator
from typing import Any

import httpx
import mcp_types
import pytest
import uvicorn
from fastmcp import Client, FastMCP
from fastmcp.client.transports import StreamableHttpTransport

from tests.conftest import VIDEO_ID, FakeProvider, GoogleMock, make_search_client, make_settings
from tubetrace_mcp.audit import AuditMiddleware, annotate, runtime_fields
from tubetrace_mcp.server import create_app, create_server

TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
GATEWAY_HEADERS = {
    "horizon-actor": "b45d13d8-4979-4240-8f5c-07ccdb6b51eb",
    "horizon-actor-email": "agrynchuk@gmail.com",
    "horizon-actor-type": "user",
    "horizon-user-role": "admin",
    "x-anthropic-client": "ClaudeCode",
    "user-agent": "Claude-User",
    "traceparent": f"00-{TRACE_ID}-00f067aa0ba902b7-01",
}


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def serve(google_mock: GoogleMock, mock_http: httpx.AsyncClient) -> Iterator[Any]:
    servers: list[tuple[uvicorn.Server, threading.Thread]] = []

    def start() -> str:
        app = create_app(
            make_settings(),
            transcript_provider=FakeProvider(),
            search_client=make_search_client(mock_http),
        )
        port = free_port()
        server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        )
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        deadline = time.time() + 15
        while not server.started:
            assert time.time() < deadline, "uvicorn did not start"
            time.sleep(0.05)
        servers.append((server, thread))
        return f"http://127.0.0.1:{port}/mcp"

    yield start
    for server, thread in servers:
        server.should_exit = True
        thread.join(timeout=10)


def audit_records(caplog: pytest.LogCaptureFixture, method: str) -> list[logging.LogRecord]:
    return [
        r
        for r in caplog.records
        if r.name == "audit" and r.getMessage() == "mcp_request" and r.__dict__["method"] == method
    ]


async def test_horizon_user_identity_and_tool_fields(
    serve: Any, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FASTMCP_CLOUD_URL", "https://tubetrace-abc123.fastmcp.app")
    caplog.set_level(logging.DEBUG, logger="audit")
    url = serve()
    async with Client(StreamableHttpTransport(url, headers=GATEWAY_HEADERS)) as client:
        await client.call_tool("youtube_list_transcripts", {"video": VIDEO_ID})
        await client.call_tool(
            "youtube_list_transcripts", {"video": "not a video"}, raise_on_error=False
        )

    ok, failed = audit_records(caplog, "tools/call")
    assert ok.levelno == logging.DEBUG, "on Horizon successes are left to Traffic Logs"
    expected = {
        "tool": "youtube_list_transcripts",
        "status": "ok",
        "user": "agrynchuk@gmail.com",
        "actor": "user",
        "role": "admin",
        "client": "ClaudeCode",
        "ua": "Claude-User",
        "arguments": "video",
        "request_id": TRACE_ID,
        "cache_hit": False,
    }
    assert ok.__dict__.items() >= expected.items()
    assert isinstance(ok.__dict__["latency_ms"], float)
    assert "ip" not in ok.__dict__, "the client IP is never known behind Horizon"
    assert failed.levelno == logging.WARNING
    assert failed.__dict__["status"] == "error"
    assert failed.__dict__["error"] == "INVALID_VIDEO_INPUT"
    assert failed.__dict__["retryable"] is False


async def test_service_account_has_no_email_and_client_info_is_ignored_behind_horizon(
    serve: Any, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FASTMCP_CLOUD_URL", "https://tubetrace-abc123.fastmcp.app")
    caplog.set_level(logging.DEBUG, logger="audit")
    url = serve()
    headers = {
        "horizon-actor": "sa_7f3c",
        "horizon-actor-type": "service_account",
        "user-agent": "python-httpx/0.28.1",
    }
    async with Client(StreamableHttpTransport(url, headers=headers)) as client:
        await client.list_tools()

    (record,) = audit_records(caplog, "tools/list")
    assert record.__dict__["user"] == "sa_7f3c"
    assert record.__dict__["actor"] == "service_account"
    assert record.__dict__["client"] == "python-httpx", "the gateway's clientInfo is ignored"


async def test_self_hosted_uses_the_mcp_client_info(
    serve: Any, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("FASTMCP_CLOUD_URL", raising=False)
    caplog.set_level(logging.INFO, logger="audit")
    url = serve()
    info = mcp_types.Implementation(name="claude-code", version="2.1.3")
    async with Client(StreamableHttpTransport(url), client_info=info) as client:
        await client.list_tools()

    (record,) = audit_records(caplog, "tools/list")
    assert record.__dict__["user"] == "anonymous"
    assert record.__dict__["client"] == "claude-code/2.1.3"
    assert "ip" not in record.__dict__, "loopback peers are not logged"


async def test_in_memory_transport_is_logged_without_http(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="audit")
    server = create_server(make_settings(), transcript_provider=FakeProvider())
    async with Client(server) as client:
        await client.list_tools()
    (record,) = audit_records(caplog, "tools/list")
    assert record.__dict__["user"] == "anonymous"
    assert record.__dict__["status"] == "ok"
    assert len(record.__dict__["request_id"]) == 32


async def test_annotate_with_reserved_names_never_breaks_the_call(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="audit")
    server: FastMCP[Any] = FastMCP("t")
    server.add_middleware(AuditMiddleware())

    @server.tool
    def echo(text: str) -> str:
        annotate(name="clash", msg="clash", size=len(text))
        return text

    async with Client(server) as client:
        result = await client.call_tool("echo", {"text": "hi"})
    assert result.data == "hi"
    (record,) = audit_records(caplog, "tools/call")
    assert record.__dict__["name_"] == "clash"
    assert record.__dict__["size"] == 2
    assert record.getMessage() == "mcp_request"


def test_runtime_fields_on_horizon(monkeypatch: pytest.MonkeyPatch) -> None:
    """Values as observed in a live Horizon deployment (Lambda environment)."""
    monkeypatch.setenv("FASTMCP_CLOUD_URL", "https://pyprobe-mcp-hgy78r.fastmcp.app")
    monkeypatch.setenv(
        "AWS_LAMBDA_LOG_GROUP_NAME",
        "/aws/lambda/mcp-prd-projects/640464c1-89a0-4876-80e7-9144216fc202/targets/"
        "af48d0c1-9bf9-49c2-bce1-2d948b6f9705/deployments/105b67f6-c481-4021-b248-a2733a19d5f5",
    )
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_MEMORY_SIZE", "1024")
    fields = runtime_fields()
    assert fields["platform"] == "horizon"
    assert fields["url"] == "https://pyprobe-mcp-hgy78r.fastmcp.app"
    assert fields["deployment"] == "105b67f6-c481-4021-b248-a2733a19d5f5"
    assert fields["memory_mb"] == 1024
    monkeypatch.delenv("FASTMCP_CLOUD_URL")
    assert "platform" not in runtime_fields()


async def test_log_requests_all_logs_successes_at_info_on_horizon(
    serve: Any, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FASTMCP_CLOUD_URL", "https://tubetrace-abc123.fastmcp.app")
    monkeypatch.setenv("LOG_REQUESTS", "all")
    caplog.set_level(logging.INFO, logger="audit")
    url = serve()
    async with Client(StreamableHttpTransport(url, headers=GATEWAY_HEADERS)) as client:
        await client.list_tools()
    (record,) = audit_records(caplog, "tools/list")
    assert record.levelno == logging.INFO


def test_log_requests_rejects_unknown_modes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOG_REQUESTS", "some")
    with pytest.raises(ValueError, match="LOG_REQUESTS"):
        AuditMiddleware()
