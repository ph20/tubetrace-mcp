# AGENTS.md — TubeTrace MCP

Personal read-only MCP server (FastMCP 4, Streamable HTTP at `/mcp`, stateless) for
YouTube search (official Data API v3) and transcripts (unofficial youtube-transcript-api).

## Commands

```bash
uv sync --group dev              # install from uv.lock (Python 3.12)
uv run ruff check . && uv run ruff format --check .
uv run mypy                      # strict
uv run pytest -q                 # hermetic: no network, no secrets; live tests skip
RUN_LIVE_TESTS=1 YOUTUBE_API_KEY=... TEST_VIDEO_ID=... uv run pytest -m live tests/live
uv run tubetrace-mcp generate-token   # client token + server digest
uv run tubetrace-mcp check-config     # validate env without starting
uv run tubetrace-mcp serve            # reads .env; dev needs AUTH_DISABLED=true or MCP_TOKEN_SHA256
uv build                              # wheel + sdist
docker compose -f compose.dev.yaml up --build   # HTTP on 127.0.0.1:8000
docker compose up -d --build                    # production: Caddy 80/443 + app
```

## Layout

`src/tubetrace_mcp/`: `settings.py` (env, fail-closed auth), `schemas.py` (tool I/O models),
`errors.py` (stable codes), `video_input.py` (ID/URL allowlist parser), `cache.py`,
`ratelimit.py`, `auth.py` (SHA-256 bearer verifier), `search_client.py` (Google),
`providers/` (`TranscriptProvider` protocol + youtube-transcript-api implementation),
`services/` (selection, pagination, search/transcript services), `server.py` (factory + tools),
`cli.py`. Tests in `tests/unit`, `tests/integration` (in-memory MCP, ASGI, real HTTP e2e), `tests/live`.

## Invariants (do not break)

- MCP transport stays Streamable HTTP, stateless, path `/mcp`; no custom JSON-RPC, no SSE-first.
- Tool functions are thin wrappers: validate -> service -> `ToolResult`. Expected failures
  return `ToolResult(is_error=True)` with `{"error": {code, message, retryable, details}}`.
  Codes live in `errors.ErrorCode`; add new ones there and document them in README.
- The Google API key comes only from `YOUTUBE_API_KEY`, is sent in the `X-Goog-Api-Key`
  header (never in URLs/args), and must never appear in logs, errors or tool schemas.
- The client bearer token is never stored on the server; only `MCP_TOKEN_SHA256` digests.
  Production (`APP_ENV=production`) must fail to start without a digest; tokens are never
  read from query strings. Do not replace `Sha256TokenVerifier` with Static/Debug verifiers.
- One tool call == at most one upstream request page (plus bounded retries). No hidden
  auto-pagination, no fetching transcripts for search results, no N+1.
- Transcript provider is behind `providers.base.TranscriptProvider`; the sync library runs in a
  bounded thread pool with a fresh timeout-enforcing `requests.Session` per attempt.
  Blocking (`UPSTREAM_BLOCKED`) must never be reported as "no subtitles".
- Pagination contract: `index` = position in the full transcript, time range filters first,
  then offset/limit; size-limited pages set `next_offset` to what was actually returned;
  an empty page with the same offset is never returned (single oversized segment -> error).
- Errors are never cached; caches/rate limits are process-local.
- Tests must stay hermetic (fake provider + `httpx.MockTransport`); live tests are opt-in.
- Everything is in English: code, identifiers, docstrings, tool descriptions, README and reports.
