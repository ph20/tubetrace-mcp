"""In-process ASGI checks: health endpoint, lifespan, auth and host/origin guards."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from starlette.testclient import TestClient

from tests.conftest import VIDEO_ID, FakeProvider, GoogleMock, make_search_client, make_settings
from tubetrace_mcp.auth import generate_token
from tubetrace_mcp.server import create_app

INIT_BODY: dict[str, Any] = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def test_healthz_without_auth_and_without_upstream_calls(
    google_mock: GoogleMock, mock_http: httpx.AsyncClient
) -> None:
    provider = FakeProvider()
    _token, digest = generate_token()
    settings = make_settings(auth_disabled=False, mcp_token_sha256=digest)
    app = create_app(
        settings, transcript_provider=provider, search_client=make_search_client(mock_http)
    )
    with TestClient(app) as client:
        response = client.get("/healthz")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok" and body["app"] == "tubetrace-mcp"
        assert "key" not in json.dumps(body).lower()
        assert provider.list_calls == provider.fetch_calls == 0
        assert google_mock.requests == []
    state = app.state.tubetrace
    assert state.http_client is None, "no default HTTP client is created when one is injected"


def test_lifespan_closes_owned_http_client(fake_provider: FakeProvider) -> None:
    settings = make_settings()
    app = create_app(settings, transcript_provider=fake_provider)
    state = app.state.tubetrace
    assert state.http_client is not None
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        assert state.http_client.is_closed is False
    assert state.http_client.is_closed is True


def test_mcp_requires_bearer_token(
    fake_provider: FakeProvider, mock_http: httpx.AsyncClient
) -> None:
    token, digest = generate_token()
    settings = make_settings(auth_disabled=False, mcp_token_sha256=digest)
    app = create_app(
        settings, transcript_provider=fake_provider, search_client=make_search_client(mock_http)
    )
    with TestClient(app) as client:
        missing = client.post("/mcp", json=INIT_BODY, headers=MCP_HEADERS)
        assert missing.status_code == 401
        assert missing.headers["www-authenticate"].startswith("Bearer")
        wrong = client.post(
            "/mcp", json=INIT_BODY, headers={**MCP_HEADERS, "Authorization": "Bearer nope"}
        )
        assert wrong.status_code == 401
        query = client.post(f"/mcp?access_token={token}", json=INIT_BODY, headers=MCP_HEADERS)
        assert query.status_code == 401, "tokens in the query string are never accepted"
        ok = client.post(
            "/mcp", json=INIT_BODY, headers={**MCP_HEADERS, "Authorization": f"Bearer {token}"}
        )
        assert ok.status_code == 200
        assert ok.json()["result"]["serverInfo"]["name"] == "tubetrace-mcp"


def test_host_and_origin_protection(
    fake_provider: FakeProvider, mock_http: httpx.AsyncClient
) -> None:
    token, digest = generate_token()
    settings = make_settings(
        auth_disabled=False,
        mcp_token_sha256=digest,
        mcp_domain="mcp.example.com",
        allowed_origins="https://app.example.com",
    )
    app = create_app(
        settings, transcript_provider=fake_provider, search_client=make_search_client(mock_http)
    )
    auth = {**MCP_HEADERS, "Authorization": f"Bearer {token}", "Host": "mcp.example.com"}
    with TestClient(app) as client:
        assert client.post("/mcp", json=INIT_BODY, headers=auth).status_code == 200
        foreign = client.post(
            "/mcp", json=INIT_BODY, headers={**auth, "Origin": "https://evil.example"}
        )
        assert foreign.status_code == 403
        allowed = client.post(
            "/mcp", json=INIT_BODY, headers={**auth, "Origin": "https://app.example.com"}
        )
        assert allowed.status_code == 200
        same = client.post(
            "/mcp", json=INIT_BODY, headers={**auth, "Origin": "http://mcp.example.com"}
        )
        assert same.status_code == 200
        bad_host = client.post("/mcp", json=INIT_BODY, headers={**auth, "Host": "evil.example"})
        assert bad_host.status_code == 421
        assert client.get("/healthz", headers={"Host": "evil.example"}).status_code == 421
        assert client.get("/healthz", headers={"Host": "localhost:8000"}).status_code == 200


def test_request_body_limit(fake_provider: FakeProvider, mock_http: httpx.AsyncClient) -> None:
    settings = make_settings(max_request_body_bytes=2048)
    app = create_app(
        settings, transcript_provider=fake_provider, search_client=make_search_client(mock_http)
    )
    with TestClient(app) as client:
        huge = {**INIT_BODY, "params": {**INIT_BODY["params"], "padding": "x" * 5000}}
        response = client.post("/mcp", json=huge, headers=MCP_HEADERS)
        assert response.status_code == 413


def test_tools_call_over_http_returns_structured_result(
    fake_provider: FakeProvider, mock_http: httpx.AsyncClient
) -> None:
    settings = make_settings()
    app = create_app(
        settings, transcript_provider=fake_provider, search_client=make_search_client(mock_http)
    )
    call = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": "youtube_get_transcript", "arguments": {"video": VIDEO_ID, "limit": 1}},
    }
    with TestClient(app) as client:
        response = client.post("/mcp", json=call, headers=MCP_HEADERS)
        assert response.status_code == 200
        result = response.json()["result"]
        assert result.get("isError") in (None, False)
        assert result["structuredContent"]["video_id"] == VIDEO_ID
        assert result["structuredContent"]["returned_segments"] == 1


@pytest.mark.parametrize("path", ["/", "/mcp/", "/sse", "/messages"])
def test_other_paths_are_not_served(fake_provider: FakeProvider, path: str) -> None:
    app = create_app(make_settings(), transcript_provider=fake_provider)
    with TestClient(app) as client:
        assert client.get(path).status_code in (404, 405)
