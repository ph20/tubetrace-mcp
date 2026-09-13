"""Example: talk to TubeTrace MCP with the real FastMCP client over Streamable HTTP.

Usage::

    export TUBETRACE_MCP_URL="https://mcp.example.com/mcp"  # default http://127.0.0.1:8000/mcp
    export TUBETRACE_MCP_TOKEN="<client bearer token>"  # omit only for AUTH_DISABLED dev
    uv run python examples/client.py "python asyncio tutorial" dQw4w9WgXcQ

The first argument is a search query (optional, needs YOUTUBE_API_KEY on the
server); the second is a video ID or URL whose transcript is fetched page by page.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any

from fastmcp import Client
from fastmcp.client.auth import BearerAuth
from fastmcp.client.transports import StreamableHttpTransport


def show(title: str, payload: Any) -> None:
    print(f"\n=== {title} ===")
    print(json.dumps(payload, ensure_ascii=False, indent=2)[:2000])


async def main(query: str | None, video: str | None) -> int:
    url = os.environ.get("TUBETRACE_MCP_URL", "http://127.0.0.1:8000/mcp")
    token = os.environ.get("TUBETRACE_MCP_TOKEN")
    transport = StreamableHttpTransport(url, auth=BearerAuth(token) if token else None)

    async with Client(transport) as client:
        tools = await client.list_tools()
        print("Connected to", url)
        print("Tools:", ", ".join(tool.name for tool in tools))

        if query:
            result = await client.call_tool(
                "youtube_search_videos", {"query": query, "max_results": 3}, raise_on_error=False
            )
            show("youtube_search_videos", result.structured_content)

        if video:
            listing = await client.call_tool(
                "youtube_list_transcripts", {"video": video}, raise_on_error=False
            )
            show("youtube_list_transcripts", listing.structured_content)
            if listing.is_error:
                return 1

            # Fetch the whole transcript page by page; the server may shorten pages to
            # respect its response size limit, so always continue from next_offset.
            offset = 0
            texts: list[str] = []
            language = None
            while True:
                page = await client.call_tool(
                    "youtube_get_transcript",
                    {"video": video, "offset": offset, "limit": 200, "format": "text"},
                    raise_on_error=False,
                )
                data = page.structured_content or {}
                if page.is_error:
                    show("youtube_get_transcript error", data)
                    return 1
                language = data.get("language_code")
                texts.append(data.get("text") or "")
                if not data.get("has_more"):
                    break
                offset = int(data["next_offset"])
            full_text = "\n".join(texts)
            print(f"\nTranscript language: {language}; characters: {len(full_text)}")
            print(full_text[:500])
    return 0


if __name__ == "__main__":
    args = sys.argv[1:]
    query_arg = args[0] if args else None
    video_arg = args[1] if len(args) > 1 else None
    raise SystemExit(asyncio.run(main(query_arg, video_arg)))
