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
APP_ENV=production AUTH_MODE=platform uv run fastmcp inspect horizon.py:mcp   # Prefect Horizon view
docker compose -f compose.dev.yaml up --build   # HTTP on 127.0.0.1:8000
docker compose up -d --build                    # production: Caddy 80/443 + app
```

## Layout

`src/tubetrace_mcp/`: `settings.py` (env, fail-closed auth), `schemas.py` (tool I/O models),
`errors.py` (stable codes), `video_input.py` (ID/URL allowlist parser), `cache.py`,
`ratelimit.py`, `auth.py` (SHA-256 bearer verifier), `search_client.py` (Google),
`providers/` (`TranscriptProvider` protocol + youtube-transcript-api implementation),
`services/` (selection, pagination, search/transcript services), `server.py` (factory + tools),
`cli.py`. Root `horizon.py` (+ `fastmcp.json`) is the Prefect Horizon entrypoint (`horizon.py:mcp`):
a module-level FastMCP object built from env, absolute imports only, no transport code.
Tests in `tests/unit`, `tests/integration` (in-memory MCP, ASGI, real HTTP e2e, Horizon entrypoint),
`tests/live`.

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
  The only exception is the explicit `AUTH_MODE=platform` (managed gateway such as Prefect
  Horizon authenticates callers): no in-process verifier, and `MCP_TOKEN_SHA256` /
  `AUTH_DISABLED` must then be unset (an error otherwise). Never make `platform` the default
  and never infer it from the environment.
- Horizon builds a Docker image from the repo root (`COPY . /app`), so `.dockerignore` must never
  exclude `horizon.py`, `fastmcp.json`, `pyproject.toml`, `uv.lock`, `README.md` or `src/`.
- `horizon.py` must stay importable with only env vars: no network, no `.env` dependency, no
  relative imports (Horizon loads it as a standalone file), and it must expose `mcp` at module
  level. Horizon runs the FastMCP object itself (stateful sessions handled by its gateway,
  170 s request timeout, 6 MB payloads); `create_app` options (Caddy, Host guard, body limit)
  do not apply there.
- One tool call == at most one upstream request page (plus bounded retries). No hidden
  auto-pagination, no fetching transcripts for search results, no N+1.
- Transcript provider is behind `providers.base.TranscriptProvider`; the sync library runs in a
  bounded thread pool with a fresh timeout-enforcing `requests.Session` per attempt.
  Blocking (`UPSTREAM_BLOCKED`) must never be reported as "no subtitles".
- `TRANSCRIPT_PROXY_URL` (optional, secret) applies ONLY to the transcript provider's
  `requests` sessions (passed per request so env proxies cannot override it). Never route the
  Google search client through it; never log or print it (only `host:port`). With a proxy,
  `UPSTREAM_BLOCKED` is retryable within the normal retry bounds; without one it is not.
- Pagination contract: `index` = position in the full transcript, time range filters first,
  then offset/limit; size-limited pages set `next_offset` to what was actually returned;
  an empty page with the same offset is never returned (single oversized segment -> error).
- Errors are never cached; caches/rate limits are process-local.
- Tests must stay hermetic (fake provider + `httpx.MockTransport`); live tests are opt-in.
- Everything is in English: code, identifiers, docstrings, tool descriptions, README and reports.
