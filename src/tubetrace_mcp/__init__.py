"""TubeTrace MCP: a personal read-only MCP server for YouTube search and transcripts."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("tubetrace-mcp")
except PackageNotFoundError:  # pragma: no cover - only when running from a raw checkout
    __version__ = "0.0.0"

__all__ = ["__version__"]
