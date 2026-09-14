# TubeTrace MCP implementation report

Date: 2026-09-13. Development environment: macOS (Darwin 24.6), uv 0.12.10, Python 3.12.14
(managed by uv), no Docker daemon, no Caddy binary, network access available.

Dependency versions (from `uv.lock`): fastmcp 4.0.3, mcp 2.2.0, youtube-transcript-api 1.2.4,
httpx 0.28.1, pydantic 2.13.5, pydantic-settings 2.15.0, uvicorn 0.52.4, starlette 1.6.0,
requests 2.34.2; dev: pytest 9.1.1, pytest-asyncio 1.4.0, ruff 0.16.7, mypy 2.3.1.

## 1. What was implemented

- The `tubetrace_mcp` package (src layout, `uv_build`, console script `tubetrace-mcp`):
  `settings.py` (pydantic-settings, fail-closed auth), `schemas.py` (typed input/output),
  `errors.py` (stable codes), `video_input.py` (ID/URL allowlist parser), `cache.py`
  (bounded LRU+TTL), `ratelimit.py` (token bucket), `retry.py`, `auth.py`
  (`Sha256TokenVerifier` on top of `fastmcp.server.auth.TokenVerifier`, constant-time),
  `search_client.py` (httpx → `search.list`, key in the `X-Goog-Api-Key` header),
  `providers/base.py` (Protocol) + `providers/youtube_transcript_api.py` (bounded thread pool,
  `TimeoutSession` with connect/read timeouts, a fresh session per attempt, retries with
  backoff/jitter, error classification), `services/` (track selection, pagination with a size
  limit, search cache and a two-level transcript cache), `server.py` (FastMCP factory + 3 tools +
  `/healthz`), `middleware.py` (request body limit), `cli.py` (`serve`, `generate-token`,
  `hash-token`, `check-config`), `asgi.py`, `logging_config.py` (JSON logs, secret redaction).
- Tools: `youtube_search_videos`, `youtube_list_transcripts`, `youtube_get_transcript` with
  read-only/open-world annotations, output schemas and structured content; domain failures are
  returned as `isError=true` with `{"error": {code, message, retryable, details}}`.
- Transport: Streamable HTTP, `/mcp`, stateless, JSON responses (`MCP_JSON_RESPONSE=true`),
  host/origin protection in strict mode, Caddy as the TLS terminator.
- Infrastructure: `Dockerfile` (multi-stage, non-root), `compose.yaml` (app + Caddy, only 80/443
  exposed, a static Caddy address for proxy-header trust, persistent volumes),
  `compose.dev.yaml`, `Caddyfile`, `.env.example`, `.dockerignore`, `.gitignore`,
  GitHub Actions CI without secrets, `examples/client.py`, `examples/codex-config.toml`,
  `README.md`, `AGENTS.md`, `LICENSE`.
- Tests: unit/integration modules plus live; e2e over a real uvicorn on loopback.

## 2. Checks that were actually run

| Check | Result |
|---|---|
| `uv lock` / `uv lock --check` / `uv sync --group dev` | OK, 92 packages, lockfile up to date |
| `uv run ruff check .` | OK (All checks passed) |
| `uv run ruff format --check .` | OK (48 files) |
| `uv run mypy` (strict, src + tests + examples) | OK (46 files, 0 errors) |
| `uv run pytest` | 153 passed, 2 skipped, 0 failed (the 2 skipped are the live tests without options, as intended); 155 tests including live |
| `uv build` | OK: wheel + sdist |
| `docker compose -f compose.yaml config -q` (with test MCP_DOMAIN/ACME_EMAIL) | OK |
| `docker compose -f compose.dev.yaml config -q` | OK |
| Live tests `RUN_LIVE_TESTS=1 … pytest -m live` (a real `search.list` with the key + a real transcript listing/fetch of `dQw4w9WgXcQ` through the provider) | 2 passed |
| Local smoke: `tubetrace-mcp serve` with `MCP_TOKEN_SHA256` on 127.0.0.1:8765 | `/healthz` 200 without auth; `/mcp` without a token → 401 + `WWW-Authenticate: Bearer`; wrong token → 401; foreign `Origin` → 403; `initialize` with a valid token → 200 (`protocolVersion 2025-06-18`) |
| `examples/client.py` against the running server (FastMCP Client, bearer) | tools/list = 3 tools; a real search for "python asyncio tutorial" → 3 results; a real track listing and transcript of `dQw4w9WgXcQ` |
| Secret redaction | the client token and the Google key are absent from the server logs (verified with grep) |
| Google Cloud | in the owner's Google Cloud project the YouTube Data API v3 was enabled and an API key named "tubetrace-mcp" was created, restricted to this API only; the value was written to the local `.env` (chmod 600, git-ignored) |

Quota spent today: 2 `search.list` calls out of the 100 daily ones (live test + smoke).

Sources checked on 2026-09-13: FastMCP (`deployment/http`, `servers/auth/token-verification`,
`clients/client`, `servers/tools`) + introspection of the installed 4.0.3 package; the
`youtube-transcript-api` README + introspection of 1.2.4; Google `search.list`, the Quota
Calculator (page dated 2026-09-04: 100 `search.list`/day, 10,000 units for the other methods),
`captions.download` (OAuth, video edit rights, 200 units); Caddy Automatic HTTPS; the Codex MCP
documentation (`bearer_token_env_var`, `url`, `startup_timeout_sec`, `tool_timeout_sec`); the
Claude Code documentation (`claude mcp add --transport http … --header`).

## 3. What was NOT verified and why

| Item | Status | Reason / how to verify |
|---|---|---|
| `docker build` of the image | not run | no Docker daemon in the development environment. Run `docker build -t tubetrace-mcp .` or rely on CI. |
| `docker compose up` (app + Caddy) | not run | same |
| `caddy validate` of the Caddyfile | not run | no Caddy binary and no daemon; the step exists in CI (`caddy:2 caddy validate`) |
| Let's Encrypt certificate issuance/renewal, public HTTPS | not run | no real domain and no public server. Requires DNS A/AAAA, open 80/443, `MCP_DOMAIN`, `ACME_EMAIL`. |
| Provider behaviour from a cloud/datacenter IP (YouTube blocking) | not run | verified only from a residential macOS network where the provider works. On a VPS `UPSTREAM_BLOCKED` is possible; the live test reports it as a failure. |
| Connecting a real Codex / Claude Code | not run | the configuration format was checked against the documentation and the server works with the FastMCP Client; the real clients were not launched. |
| GitHub Actions | not run locally | the workflow runs automatically after a push to GitHub; see the Actions tab of the repository. |
| Managed-HTTPS platform (without Caddy) | not run | described in README (PORT, MCP_DOMAIN, FORWARDED_ALLOW_IPS), not deployed. |

## 4. External prerequisites for production

1. A domain with A/AAAA DNS records pointing at the server; open ports 80/443.
2. A Google key (already created and stored in `.env`) restricted to the YouTube Data API v3.
3. A "client token / server digest" pair (`uv run tubetrace-mcp generate-token`).
4. A production `.env`: `APP_ENV=production`, `MCP_TOKEN_SHA256`, `MCP_DOMAIN`, `ACME_EMAIL`,
   `YOUTUBE_API_KEY`; no `AUTH_DISABLED`.
5. A host with Docker Engine + Compose v2.

## 5. Decisions on ambiguities

- MCP POST responses are `application/json` (`MCP_JSON_RESPONSE=true`) rather than SSE: the tools
  do not stream progress, JSON is more robust behind proxies, and clients must accept both.
- The Google key is sent in the `X-Goog-Api-Key` header (supported by Google APIs, confirmed by
  the live test), so request URLs are safe to log.
- Tool errors are returned as `ToolResult(is_error=True)` with structured content and the same
  JSON in a text block; argument JSON-schema errors stay as FastMCP's standard messages (also
  `isError=true`).
- The `MAX_RESPONSE_BYTES` limit counts the structured payload; on the wire the response is
  roughly ×2 because of the duplicated text block (documented).
- `format=text` joins segments with `\n` (like the library's `TextFormatter`).
- Track selection policy without `languages`: preferred kind → the first track in the provider's
  order; explicitly documented as not being the "original language".
- `YouTubeRequestFailed` in 1.2.4 keeps only the error text, so the HTTP status is parsed from the
  string (`"429 Client Error…"`) and `Retry-After` is unavailable for the provider — backoff is used.
- Host validation: FastMCP `HostOriginGuardMiddleware` in strict mode + `MCP_DOMAIN`; in Docker
  the healthcheck uses `127.0.0.1` (allowed by default).

## 6. Library observations (relevant for maintenance)

- FastMCP 4.0.3 / MCP SDK 2.2.0: the client in `mode="auto"` performs the new negotiation
  (`server/discover`), in which `ping` returns "Method not found" and `initialize_result` is None;
  in the standard mode (`mode="legacy"`, the usual `initialize`) `ping` works. Covered by a test.
- `Tool.outputSchema` is deprecated → use `output_schema` (SDK v2 snake_case).
- `youtube-transcript-api` 1.2.4: `YouTubeTranscriptApi(http_client=...)` is not thread-safe —
  the provider creates a new `requests.Session` per attempt; cookie auth is disabled in the library.
- FastMCP installs its own RichHandler on the `fastmcp` logger; `configure_logging` reroutes it to
  the root JSON formatter with redaction.

## 6a. Prefect Horizon compatibility (added 2026-09-14)

- Added `horizon.py` (entrypoint `horizon.py:mcp`, module-level FastMCP object from the shared
  factory, absolute imports, no transport code), `fastmcp.json` (entrypoint, Python 3.12,
  project install from `uv.lock`), `AUTH_MODE=bearer|platform` in `settings.py` (platform mode:
  gateway-managed auth, no in-process verifier, digest/`AUTH_DISABLED` rejected, `MCP_DOMAIN`
  not required), `default-groups = []` in `pyproject.toml` so the frozen `uv sync` on Horizon
  installs only runtime dependencies, a CI step running `fastmcp inspect horizon.py:mcp`, tests
  (`tests/integration/test_horizon_entrypoint.py`, settings and CLI cases) and a README section.
- Verified locally: `AUTH_MODE=platform APP_ENV=production uv run fastmcp inspect horizon.py:mcp`
  reports the server with 3 tools (the same inspection Horizon runs at build time); the test
  suite loads the entrypoint through FastMCP's `FileSystemSource` exactly like the CLI does.
- Verified on Horizon (2026-09-14, server `tubetrace-mcp-hgy78r`, deployment of `cfbecb3`):
  the first build (`3d54a55`) failed because `.dockerignore` excluded `horizon.py` from
  Horizon's `COPY . /app` (fixed in `cfbecb3`); the second build detected `fastmcp.json`,
  Python 3.12 and `uv.lock`, ran `uv sync --frozen --no-dev` and `fastmcp inspect`, and went
  Live. Server logs: `auth_delegated_to_platform`, `server_started` with `auth_mode=platform`,
  `google_configured=true`, FastMCP started the server as `transport 'http' (stateless)` on
  port 8080. Environment variables in the dashboard: `APP_ENV`, `AUTH_MODE`, `YOUTUBE_API_KEY`
  (encrypted). Horizon authentication enabled: an unauthenticated `POST /mcp` from outside
  returns 401 with `WWW-Authenticate: Bearer` and OAuth protected-resource metadata; `/healthz`
  is also behind the gateway (401). Playground: `youtube_list_transcripts` (6 tracks for
  `dQw4w9WgXcQ`), `youtube_get_transcript` (61 segments, manual English track,
  `has_more=false`), `youtube_search_videos` (real `search.list`, 1 quota call) all succeeded
  from Horizon's AWS egress, i.e. the provider was not blocked that day; a spoofed hostname
  returned `INVALID_VIDEO_INPUT` (`host_not_allowed`) as an `isError` tool result. Traffic
  logs show 9 gateway requests, 0 errors, with the caller identity; server logs contain no key
  or token material.
- Not verified: whether Horizon forwards the client `Authorization` header when Horizon
  authentication is disabled (the README describes that variant as documented by Horizon, not
  as tested).
- Sources: gofastmcp.com/deployment/prefect-horizon; docs.horizon.prefect.io (quickstart,
  platform/build-system, platform/compute-model, gateway, platform/authentication,
  environment-variables, limits, platform/networking); introspection of FastMCP 4.0.3
  (`fastmcp.cli.run`, `FileSystemSource`, `run_http_async`).

## 6b. Transcript proxy (added 2026-09-14)

- `TRANSCRIPT_PROXY_URL` (`http(s)://user:password@host:port`, `SecretStr`) is applied only to the
  `youtube-transcript-api` sessions. `TimeoutSession` also passes the proxies per request because
  `requests` otherwise lets `HTTP(S)_PROXY` environment variables override `Session.proxies`. The
  httpx search client is untouched.
- `youtube-transcript-api`'s `ProxyConfig` is not used: `retries_when_blocked` would retry on the
  same session/connection and mount its own urllib3 retry adapters. Instead a block through a proxy
  is marked retryable and goes through the provider's own bounded retry loop with a fresh session.
- Validation errors never echo the URL; the URL and its password are added to log redaction, and a
  generic `scheme://user:password@` redaction pattern covers short passwords.
- Verified with unit tests (fake sessions/adapters, no network) and live on 2026-09-14 through
  Oxylabs Mobile Proxies (`pr.oxylabs.io:7777`, rotating, `-cc-US`), from a residential Ukrainian
  network:
  - the provider's session exited through a US mobile carrier IP while the search `httpx` client
    exited directly from the local IP (proxy isolation);
  - `list` + `fetch` succeeded through the proxy for two videos (about 4-6 s each);
  - a full `tubetrace-mcp serve` run with `examples/client.py` returned search results, the track
    list and a transcript page; neither the password nor the proxy username appeared in the server
    log or client output;
  - a wrong password returned `UPSTREAM_ERROR` / `proxy_auth_failed` (not retried) through MCP while
    search kept working.
  Not verified: the retry-on-block path against a real YouTube block (no block occurred).

## 7. Files

Key files: `pyproject.toml`, `uv.lock`, `src/tubetrace_mcp/**`, `tests/**`, `horizon.py`,
`fastmcp.json`, `Dockerfile`,
`compose.yaml`, `compose.dev.yaml`, `Caddyfile`, `.env.example`, `.github/workflows/ci.yml`,
`examples/client.py`, `examples/codex-config.toml`, `README.md`, `AGENTS.md`, this report.
The local `.env` contains the real key and must **not** end up in the repository (it is in
`.gitignore`). Repository: https://github.com/ph20/tubetrace-mcp (branch `main`).
