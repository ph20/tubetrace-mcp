"""Prefect Horizon entrypoint: ``horizon.py:mcp``.

Prefect Horizon (the managed MCP platform from the FastMCP team) imports this file,
takes the module-level ``mcp`` object and runs it as an HTTP MCP server itself. It
does not use ``tubetrace-mcp serve``, ``asgi.py``, the Dockerfile or Caddy, so this
module only builds the FastMCP server; the transport, port, sessions, TLS and caller
authentication are owned by Horizon.

Configuration comes from the environment variables set in the Horizon dashboard
(see README, "Deploying to Prefect Horizon"). The minimum is::

    APP_ENV=production
    AUTH_MODE=platform      # Horizon authentication is enabled; no in-process tokens
    YOUTUBE_API_KEY=...     # optional, only needed for youtube_search_videos

Keep ``AUTH_MODE=bearer`` (with ``MCP_TOKEN_SHA256`` and ``MCP_DOMAIN``) only if Horizon
authentication is disabled for the server and clients send their own bearer token.

Import-time work is deliberately small: settings validation, logging setup and the
service wiring. No network request is made until a tool is called. Horizon runs a
``fastmcp inspect`` step at build time, so a misconfiguration fails the build with a
clear message instead of producing an unauthenticated or half-configured server.

Verify locally what Horizon will see::

    AUTH_MODE=platform APP_ENV=production uv run fastmcp inspect horizon.py:mcp
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

try:  # the package is installed by Horizon from uv.lock / pyproject.toml
    import tubetrace_mcp  # noqa: F401
except ModuleNotFoundError:  # pragma: no cover - raw checkout without installation
    sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from fastmcp import FastMCP

from tubetrace_mcp.logging_config import configure_logging
from tubetrace_mcp.server import create_server
from tubetrace_mcp.settings import Settings


def build_server() -> FastMCP[Any]:
    """Build the FastMCP server from environment variables (no upstream calls)."""
    settings = Settings()
    configure_logging(settings.log_level, settings.log_format, secrets=settings.redaction_secrets())
    return create_server(settings)


mcp: FastMCP[Any] = build_server()

if __name__ == "__main__":  # pragma: no cover - ignored by Horizon; local convenience only
    mcp.run(transport="http", host="127.0.0.1", port=8000, path="/mcp")
