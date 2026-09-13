"""End-to-end over a real local HTTP transport (uvicorn on a random loopback port).

Covers the Streamable HTTP handshake, discovery, bearer auth (valid, missing,
wrong, query-string), host/origin protection and tool calls with mocked upstreams
through the real FastMCP client.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator

import httpx
import pytest
import uvicorn
from fastmcp import Client
from fastmcp.client.auth import BearerAuth
from fastmcp.client.transports import StreamableHttpTransport

from tests.conftest import (
    VIDEO_ID,
    FakeProvider,
    GoogleMock,
    decode_error,
    make_search_client,
    make_settings,
)
from tubetrace_mcp.auth import generate_token
from tubetrace_mcp.server import create_app

pytestmark = pytest.mark.e2e


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class RunningServer:
    def __init__(self, url: str, token: str, provider: FakeProvider, google: GoogleMock) -> None:
        self.url = url
        self.token = token
        self.provider = provider
        self.google = google


@pytest.fixture(scope="module")
def running_server() -> Iterator[RunningServer]:
    token, digest = generate_token()
    google = GoogleMock()
    http = httpx.AsyncClient(transport=httpx.MockTransport(google.handler))
    provider = FakeProvider()
    settings = make_settings(
        app_env="production",
        auth_disabled=False,
        mcp_token_sha256=digest,
        mcp_domain="mcp.example.com",
        log_format="text",
    )
    app = create_app(settings, transcript_provider=provider, search_client=make_search_client(http))
    port = free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while not server.started:
        if time.time() > deadline:
            raise RuntimeError("uvicorn did not start")
        time.sleep(0.05)
    try:
        yield RunningServer(f"http://127.0.0.1:{port}", token, provider, google)
    finally:
        server.should_exit = True
        thread.join(timeout=10)


INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "t", "version": "0"},
    },
}
HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def test_health_is_public(running_server: RunningServer) -> None:
    response = httpx.get(f"{running_server.url}/healthz", timeout=5)
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_auth_failures_over_real_http(running_server: RunningServer) -> None:
    base = running_server.url
    missing = httpx.post(f"{base}/mcp", json=INIT, headers=HEADERS, timeout=5)
    assert missing.status_code == 401
    assert "Bearer" in missing.headers.get("www-authenticate", "")
    wrong = httpx.post(
        f"{base}/mcp", json=INIT, headers={**HEADERS, "Authorization": "Bearer wrong"}, timeout=5
    )
    assert wrong.status_code == 401
    query = httpx.post(
        f"{base}/mcp?token={running_server.token}", json=INIT, headers=HEADERS, timeout=5
    )
    assert query.status_code == 401
    foreign_origin = httpx.post(
        f"{base}/mcp",
        json=INIT,
        headers={
            **HEADERS,
            "Authorization": f"Bearer {running_server.token}",
            "Origin": "https://evil.example",
        },
        timeout=5,
    )
    assert foreign_origin.status_code == 403
    bad_host = httpx.post(
        f"{base}/mcp",
        json=INIT,
        headers={
            **HEADERS,
            "Authorization": f"Bearer {running_server.token}",
            "Host": "evil.example",
        },
        timeout=5,
    )
    assert bad_host.status_code == 421
    good_host = httpx.post(
        f"{base}/mcp",
        json=INIT,
        headers={
            **HEADERS,
            "Authorization": f"Bearer {running_server.token}",
            "Host": "mcp.example.com",
        },
        timeout=5,
    )
    assert good_host.status_code == 200


async def test_fastmcp_client_handshake_discovery_and_calls(running_server: RunningServer) -> None:
    transport = StreamableHttpTransport(
        f"{running_server.url}/mcp", auth=BearerAuth(running_server.token)
    )
    async with Client(transport) as client:
        tools = {t.name for t in await client.list_tools()}
        assert tools == {
            "youtube_search_videos",
            "youtube_list_transcripts",
            "youtube_get_transcript",
        }

        listing = await client.call_tool(
            "youtube_list_transcripts", {"video": f"https://youtu.be/{VIDEO_ID}"}
        )
        assert listing.structured_content is not None
        assert listing.structured_content["provider"] == "fake_provider"

        page = await client.call_tool("youtube_get_transcript", {"video": VIDEO_ID, "limit": 2})
        assert page.structured_content is not None
        assert page.structured_content["returned_segments"] == 2
        next_page = await client.call_tool(
            "youtube_get_transcript", {"video": VIDEO_ID, "limit": 2, "offset": 2}
        )
        assert next_page.structured_content is not None
        assert next_page.structured_content["cache_hit"] is True

        search = await client.call_tool("youtube_search_videos", {"query": "mcp servers"})
        assert search.structured_content is not None
        assert len(search.structured_content["items"]) == 2
        sent = running_server.google.requests[-1]
        assert "X-Goog-Api-Key" in sent.headers
        assert running_server.token not in str(sent.url)
        assert running_server.token not in sent.headers.get("Authorization", "")

        failure = await client.call_tool(
            "youtube_get_transcript", {"video": "https://evil.example/w"}, raise_on_error=False
        )
        assert failure.is_error is True
        assert decode_error(failure.content[0].text)["code"] == "INVALID_VIDEO_INPUT"


async def test_fastmcp_client_with_wrong_token_fails(running_server: RunningServer) -> None:
    transport = StreamableHttpTransport(
        f"{running_server.url}/mcp", auth=BearerAuth("not-the-token")
    )
    with pytest.raises(Exception) as info:
        async with Client(transport) as client:
            await client.list_tools()
    message = str(info.value).lower()
    assert "401" in message or "unauthorized" in message or "error response" in message
