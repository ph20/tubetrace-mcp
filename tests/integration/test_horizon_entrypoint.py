"""The Prefect Horizon entrypoint (``horizon.py:mcp``) must be importable and inspectable.

Horizon imports the entrypoint file at build time, takes the module-level ``mcp`` object,
runs ``fastmcp inspect`` on it and later serves it over HTTP itself. These tests load the
file the same way the FastMCP CLI does (``FileSystemSource``), with the environment Horizon
users are told to configure, and check that the result is the real TubeTrace server with no
in-process bearer verification.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client, FastMCP
from fastmcp.utilities.inspect import inspect_fastmcp
from fastmcp.utilities.mcp_server_config.v1.sources.filesystem import FileSystemSource
from pydantic import ValidationError

REPO_ROOT = Path(__file__).resolve().parents[2]
ENTRYPOINT = REPO_ROOT / "horizon.py"
TOOL_NAMES = {"youtube_search_videos", "youtube_list_transcripts", "youtube_get_transcript"}


@pytest.fixture
def horizon_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The documented Horizon configuration; ``.env`` from the checkout is not read."""
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("AUTH_MODE", "platform")
    monkeypatch.setenv("AUTH_DISABLED", "false")
    monkeypatch.setenv("MCP_TOKEN_SHA256", "")
    monkeypatch.chdir(REPO_ROOT / "tests")
    sys.modules.pop("server_module", None)
    yield
    sys.modules.pop("server_module", None)


async def load_entrypoint() -> FastMCP[Any]:
    server = await FileSystemSource(path=f"{ENTRYPOINT}:mcp").load_server()
    assert isinstance(server, FastMCP)
    return server


@pytest.mark.usefixtures("horizon_env")
async def test_entrypoint_loads_like_the_fastmcp_cli_and_inspects() -> None:
    server = await load_entrypoint()
    assert server.name == "tubetrace-mcp"
    assert server.auth is None, "AUTH_MODE=platform must not attach an in-process verifier"
    info = await inspect_fastmcp(server)
    assert {tool.name for tool in info.tools} == TOOL_NAMES
    assert info.name == "tubetrace-mcp"


@pytest.mark.usefixtures("horizon_env")
async def test_entrypoint_serves_tools_in_memory() -> None:
    server = await load_entrypoint()
    async with Client(server) as client:
        tools = {tool.name for tool in await client.list_tools()}
        assert tools == TOOL_NAMES
        # No YOUTUBE_API_KEY in this environment: the search tool must answer with the stable
        # error code instead of failing the import or the call.
        result = await client.call_tool(
            "youtube_search_videos", {"query": "python"}, raise_on_error=False
        )
        assert result.is_error is True
        assert result.structured_content is not None
        assert result.structured_content["error"]["code"] == "GOOGLE_API_NOT_CONFIGURED"


async def test_entrypoint_fails_closed_without_explicit_auth_choice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without AUTH_MODE=platform (or a digest) the Horizon build must fail, not run open."""
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("AUTH_DISABLED", "false")
    monkeypatch.setenv("MCP_TOKEN_SHA256", "")
    monkeypatch.delenv("AUTH_MODE", raising=False)
    monkeypatch.chdir(REPO_ROOT / "tests")
    sys.modules.pop("server_module", None)
    with pytest.raises(ValidationError, match="AUTH_MODE=platform"):
        await FileSystemSource(path=f"{ENTRYPOINT}:mcp").load_server()
    sys.modules.pop("server_module", None)


def test_fastmcp_json_points_at_the_entrypoint() -> None:
    config = json.loads((REPO_ROOT / "fastmcp.json").read_text())
    assert config["source"] == {"path": "horizon.py", "entrypoint": "mcp"}
    assert config["environment"]["project"] == "."
    assert config["deployment"]["transport"] == "http"
    assert config["deployment"]["path"] == "/mcp"
