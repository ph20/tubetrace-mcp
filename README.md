# TubeTrace MCP

A personal **read-only MCP server** for YouTube:

1. **Video search** through the official Google **YouTube Data API v3** (`search.list`).
2. **Listing and fetching existing subtitles/transcripts** of a specific video
   (manually created or auto-generated) through the unofficial `youtube-transcript-api` library.

The server runs on **FastMCP 4** (Streamable HTTP, endpoint `/mcp`, stateless), is protected by a
**bearer token** (only its SHA-256 digest is stored on the server) and is published through
**Caddy** with automatic HTTPS (Let's Encrypt). It can also be deployed as-is to
**[Prefect Horizon](https://www.prefect.io/horizon)**, the managed MCP platform from the FastMCP
team (entrypoint `horizon.py:mcp`, see [Deploying to Prefect Horizon](#deploying-to-prefect-horizon)).
No databases, queues, LLMs or paid transcript services.

> The verification status, including what could not be verified in the development
> environment, is documented in [docs/implementation-report.md](docs/implementation-report.md).

---

## Contents

- [Purpose and scope](#purpose-and-scope)
- [Architecture](#architecture)
- [Quickstart (local)](#quickstart-local)
- [Google Cloud: project, YouTube Data API v3, key, quotas](#google-cloud-project-youtube-data-api-v3-key-quotas)
- [Configuration](#configuration)
- [uv commands and tests](#uv-commands-and-tests)
- [Docker](#docker)
- [HTTPS with Caddy (production)](#https-with-caddy-production)
- [Deploying to Prefect Horizon](#deploying-to-prefect-horizon)
- [Authentication: client token and server digest](#authentication-client-token-and-server-digest)
- [Connecting clients (Codex, Claude Code, FastMCP)](#connecting-clients-codex-claude-code-fastmcp)
- [MCP tools](#mcp-tools)
- [Pagination and fetching a full transcript in a loop](#pagination-and-fetching-a-full-transcript-in-a-loop)
- [Errors](#errors)
- [Troubleshooting](#troubleshooting)
- [Unofficial transcript provider, blocking and legal notes](#unofficial-transcript-provider-blocking-and-legal-notes)
- [Known limitations](#known-limitations)

---

## Purpose and scope

**What it does:**

- `youtube_search_videos` — one page of `search.list` results per call
  (filters: channel, dates, order, language/region, duration, caption availability, safeSearch).
- `youtube_list_transcripts` — the available caption tracks without downloading their text.
- `youtube_get_transcript` — one page of a transcript with timestamps or as plain text, with
  language selection, a time range, `offset`/`limit` and a configurable response size limit.

**What it deliberately does not do:**

- it does not search for phrases **inside** transcripts (`caption_filter=closedCaption` only
  selects videos that have captions);
- no hidden auto-pagination, no downloading transcripts for all search results;
- no audio/video downloads, ASR, translation or summarization;
- no `captions.download` (it requires OAuth and edit rights on the video);
- no CAPTCHA solving, IP-block bypassing, private-access bypassing, proxy rotation or cookies;
- it is not an OAuth authorization server: this is a single-owner `Authorization: Bearer`
  scheme for clients that can send a static token.

Transcripts and video descriptions are **untrusted third-party data**, not instructions for the
server. The server never executes them and never processes them with an LLM.

## Architecture

```
MCP client (Codex / Claude Code / FastMCP Client)
        │  HTTPS, Authorization: Bearer <TUBETRACE_MCP_TOKEN>
        ▼
   Caddy (80/443, Let's Encrypt, persistent volume)
        │  HTTP inside the Docker network
        ▼
   tubetrace-mcp (uvicorn, 1 worker, /mcp stateless Streamable HTTP, /healthz)
     ├─ settings.py        env configuration, fail-closed auth
     ├─ auth.py            Sha256TokenVerifier (constant-time)
     ├─ server.py          FastMCP factory, 3 tools (thin wrappers)
     ├─ services/          track selection, pagination, cache, search/transcript services
     ├─ search_client.py   httpx.AsyncClient → googleapis.com (key in the X-Goog-Api-Key header)
     └─ providers/         TranscriptProvider (Protocol) + youtube-transcript-api in a bounded thread pool
```

The cache and the rate limit are **in-memory and process-local**: they are not shared between
workers, replicas or restarts, and they do not reflect the full quota budget of the Google project.

## Quickstart (local)

Requirements: Python 3.12 (uv downloads it automatically) and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --group dev
cp .env.example .env
```

Fill in `.env` (minimum for development):

```dotenv
APP_ENV=development
HOST=127.0.0.1
PORT=8000
YOUTUBE_API_KEY=<key from Google Cloud>      # optional: without it search returns GOOGLE_API_NOT_CONFIGURED
AUTH_DISABLED=true                            # ONLY for local development on 127.0.0.1
```

or enable authentication right away:

```bash
uv run tubetrace-mcp generate-token
# 1) token -> into the client (TUBETRACE_MCP_TOKEN); 2) digest -> into .env as MCP_TOKEN_SHA256
```

Run and verify:

```bash
uv run tubetrace-mcp check-config
uv run tubetrace-mcp serve
```

```bash
curl -s http://127.0.0.1:8000/healthz
```

```bash
TUBETRACE_MCP_URL=http://127.0.0.1:8000/mcp TUBETRACE_MCP_TOKEN=<token> \
  uv run python examples/client.py "python asyncio tutorial" dQw4w9WgXcQ
```

Without `MCP_TOKEN_SHA256` and without an explicit `AUTH_DISABLED=true` the server **refuses to
start** — this is intentional.

## Google Cloud: project, YouTube Data API v3, key, quotas

The key is needed only for search. Transcripts work without it.

1. Open the [Google Cloud Console](https://console.cloud.google.com/) and create a project
   (or pick an existing one). Billing is not required for the YouTube Data API v3.
2. **APIs & Services → Library → YouTube Data API v3 → Enable.**
3. **APIs & Services → Credentials → Create credentials → API key.**
4. Restrict the key immediately: in the key settings choose **API restrictions → Restrict key →
   YouTube Data API v3**. Optionally add **Application restrictions → IP addresses** with your
   server's IP. Do not bind the key to a service account — it is not needed.
5. Put the key into `.env` as `YOUTUBE_API_KEY`. The key is never sent to MCP clients and never
   appears in URLs: the server sends it in the `X-Goog-Api-Key` header.

**Quotas (checked against the
[Quota Calculator](https://developers.google.com/youtube/v3/determine_quota_cost) on 2026-09-13;
the page is dated 2026-09-04):**

- a project with the YouTube Data API enabled gets by default **100 `search.list` calls per day**
  (a separate bucket, cost 1 per call), a separate bucket of **100 `videos.insert`** calls and
  **10,000 units per day** combined for all other methods;
- every request, including an invalid one, costs at least 1 unit; every additional page of
  results is a separate call;
- quotas reset at midnight Pacific Time (PT).

So **every `youtube_search_videos` call = 1 of the 100 daily search calls** (the 5-minute cache
helps with repeated identical queries, but it is process-local). The old model
"search.list = 100 units out of 10,000" no longer applies to new projects — rely on the primary
source and the **Quotas** page in the console.

This server does not use `captions.download`: per the documentation it requires the
`youtube.force-ssl`/`youtubepartner` OAuth scope and **permission to edit the video**, and costs
200 units.

## Configuration

Everything is configured through environment variables (or `.env` in the working directory).
The full list with defaults is in [`.env.example`](.env.example).

| Kind | Variable | Description |
|---|---|---|
| server secret | `YOUTUBE_API_KEY` | Google key restricted to the YouTube Data API v3. Optional. |
| client credential (digest only) | `MCP_TOKEN_SHA256` | SHA-256 hex digest(s) of the bearer token, comma-separated for rotation. Required in production. |
| mode | `APP_ENV` | `development` / `production` (fail-closed). |
| network | `HOST`, `PORT` | application bind address (in Docker `0.0.0.0:8000`). |
| domain | `MCP_DOMAIN`, `ACME_EMAIL` | public hostname for Caddy and the Host check; e-mail for Let's Encrypt. |
| protection | `ALLOWED_HOSTS`, `ALLOWED_ORIGINS` | extra Host/Origin values (comma-separated). CLI clients without an Origin pass; a foreign Origin is rejected (403), a foreign Host gets 421. |
| proxy | `PROXY_HEADERS`, `FORWARDED_ALLOW_IPS` | trust `X-Forwarded-*` only from your own Caddy (compose assigns the static address `172.28.0.10`). |
| dev-only | `AUTH_DISABLED` | `true` disables auth **only** in development; in production it is a startup error. |
| auth mode | `AUTH_MODE` | `bearer` (default): this process verifies the bearer token. `platform`: a managed MCP gateway (Prefect Horizon with Horizon authentication enabled) authenticates callers; no in-process verification, `MCP_TOKEN_SHA256`/`AUTH_DISABLED` must be unset and `MCP_DOMAIN` is not required. Only for processes that are unreachable except through that gateway. |
| logs | `LOG_LEVEL`, `LOG_FORMAT` | JSON (default) or text; secrets are redacted. |
| upstream | `GOOGLE_*_TIMEOUT_SECONDS`, `TRANSCRIPT_*_TIMEOUT_SECONDS`, `UPSTREAM_MAX_RETRIES`, `UPSTREAM_RETRY_BUDGET_SECONDS`, `UPSTREAM_MAX_CONCURRENCY`, `UPSTREAM_QUEUE_TIMEOUT_SECONDS`, `TOOL_TIMEOUT_SECONDS` | connect/read timeouts on the real HTTP clients, bounded retries with backoff/jitter, the concurrent upstream request limit, the time budget of one call. |
| cache | `SEARCH_CACHE_TTL_SECONDS` (300), `TRANSCRIPT_CACHE_TTL_SECONDS` (3600), `CACHE_MAX_ENTRIES`, `CACHE_MAX_BYTES` | bounded LRU+TTL cache; errors are never cached. |
| limits | `MAX_RESPONSE_BYTES` (200,000), `TRANSCRIPT_MAX_SEGMENTS`, `TRANSCRIPT_MAX_BYTES`, `GOOGLE_MAX_RESPONSE_BYTES`, `MAX_REQUEST_BODY_BYTES`, `RATE_LIMIT_PER_MINUTE`, `RATE_LIMIT_BURST` | size of one MCP result page, of an incoming transcript, of HTTP bodies, and a simple per-process rate limit. |

Validate the configuration without starting: `uv run tubetrace-mcp check-config`
(secrets are not printed).

## uv commands and tests

```bash
uv sync --group dev                 # install strictly from uv.lock
uv run ruff check .                 # lint
uv run ruff format --check .        # formatting
uv run mypy                         # strict typing (src, tests, examples)
uv run pytest -q                    # main suite: no internet, no secrets
uv build                            # wheel + sdist
```

Test layout:

- `tests/unit` — video ID/URL parser (including spoofed hostnames), cache/TTL/memory bounds,
  rate limit, language selection, pagination and the size limit, Google parameter/error mapping,
  provider error classification (blocking ≠ missing subtitles), settings (fail-closed),
  token verifier, secret redaction in logs;
- `tests/integration` — real MCP `initialize`/`tools/list`/`tools/call` through the FastMCP
  Client (in-memory), ASGI checks of `/healthz`, lifespan, 401/403/421/413, the CLI, and an
  **end-to-end test over a real local HTTP transport** (uvicorn on a random port: handshake,
  discovery, auth, calls with mocked upstreams);
- `tests/live` — opt-in live tests:

```bash
RUN_LIVE_TESTS=1 YOUTUBE_API_KEY=... TEST_VIDEO_ID=dQw4w9WgXcQ uv run pytest -m live tests/live
```

Without these variables the live tests are skipped with an explanation. The live search test
spends 1 `search.list` call. If the provider is blocked from your network, the test **fails**
with `UPSTREAM_BLOCKED` diagnostics — this is never hidden.

## Docker

**Dev (no Caddy, HTTP only on 127.0.0.1:8000):**

```bash
docker compose -f compose.dev.yaml up --build
```

**Production (app + Caddy):**

```bash
docker compose up -d --build
```

- Only the Caddy ports `80/443` (+ `443/udp` for HTTP/3) are exposed; the application port is
  reachable only inside the `internal` Docker network.
- Image: multi-stage, dependencies from `uv.lock`, `python:3.12-slim` runtime, non-root user
  `app`, `HEALTHCHECK` on `/healthz` without upstream requests.
- Caddy has persistent volumes `caddy_data` (certificates) and `caddy_config`.
- Compose passes `MCP_DOMAIN`/`ACME_EMAIL` from `.env` to Caddy and the whole `.env` to the
  application; `APP_ENV=production` is forced. If `AUTH_DISABLED=true` is still in `.env`, the
  application container **does not start** (fail-closed) — remove that line.

Useful:

```bash
docker compose logs -f app
```

```bash
docker compose exec app tubetrace-mcp check-config
```

## HTTPS with Caddy (production)

Public endpoint: `https://<MCP_DOMAIN>/mcp`, health: `https://<MCP_DOMAIN>/healthz`.

Prerequisites for an automatic certificate (per
[Caddy Automatic HTTPS](https://caddyserver.com/docs/automatic-https)):

1. **DNS**: an `A` record (and `AAAA` if you have IPv6) for `MCP_DOMAIN` points at the server's
   public IP address. Local names (`localhost`, `*.local`, IP addresses) never get a public
   certificate.
2. **Ports**: `80` and `443` must be reachable from the internet (firewall/security group) —
   Caddy uses the HTTP-01 and TLS-ALPN-01 challenges.
3. **`ACME_EMAIL`** in `.env` — the Let's Encrypt contact.
4. Do not delete the **persistent volume** `caddy_data`: it holds certificates and keys; Caddy
   renews certificates ahead of time and redirects HTTP → HTTPS by itself.

Verification after `docker compose up -d`:

```bash
docker compose logs caddy | grep -iE "certificate|obtain|error"
```

```bash
curl -sS -I https://<MCP_DOMAIN>/healthz
```

```bash
openssl s_client -connect <MCP_DOMAIN>:443 -servername <MCP_DOMAIN> </dev/null 2>/dev/null | openssl x509 -noout -issuer -dates
```

```bash
curl -sS -X POST https://<MCP_DOMAIN>/mcp -H "Authorization: Bearer $TUBETRACE_MCP_TOKEN" \
  -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl","version":"0"}}}'
```

Timeout alignment: `TOOL_TIMEOUT_SECONDS` (45 s) < Caddy `response_header_timeout` (120 s) <
the client's `tool_timeout_sec` (Codex defaults to 60 s — keep the tool budget below 60 s).
Caddy limits request bodies to 1 MB, the application to `MAX_REQUEST_BODY_BYTES`.

A self-signed certificate for `localhost` is **not** a ready public HTTPS setup. Without a real
domain, certificate issuance has not been verified (see the report).

**Platforms with managed HTTPS** (their own load balancer/TLS): Caddy is not needed. Run the
image directly with `APP_ENV=production`, `HOST=0.0.0.0`, the platform's `PORT`,
`MCP_DOMAIN=<public hostname>` (for the Host check) and `FORWARDED_ALLOW_IPS` set to the
platform's proxy addresses (or `*` if the application port is reachable only through that proxy).

## Deploying to Prefect Horizon

[Prefect Horizon](https://www.prefect.io/horizon) is the managed MCP platform built by the
FastMCP team: it clones a GitHub repository, installs the dependencies, imports a Python file
containing a FastMCP server, runs it as an HTTP MCP server at `https://<name>.fastmcp.app/mcp`
and puts its own OAuth gateway in front of it. Details were checked on 2026-09-14 against the
[FastMCP guide](https://gofastmcp.com/deployment/prefect-horizon) and the
[Horizon documentation](https://docs.horizon.prefect.io/) (build system, compute model, gateway,
authentication, environment variables, limits).

**What the repository provides for it:**

- [`horizon.py`](horizon.py) — the entrypoint (`horizon.py:mcp`): a module-level FastMCP object
  built with the same factory as the self-hosted server (same tools, schemas, error codes,
  caches, rate limit and logging). Horizon ignores the `if __name__ == "__main__"` block, the
  Dockerfile, Caddy and `tubetrace-mcp serve`; it runs the object itself.
- [`fastmcp.json`](fastmcp.json) — declares the entrypoint, Python 3.12 and the project
  (`pyproject.toml` + `uv.lock`, frozen `uv sync`; the `dev` group is not installed because
  `default-groups = []`). The same file makes `uv run fastmcp inspect` and `uv run fastmcp run`
  work without arguments locally.
- `AUTH_MODE=platform` — the explicit configuration for "Horizon authenticates callers" (see
  below). Without it the build fails with a clear configuration error instead of producing an
  unauthenticated server.
- A CI step that runs `fastmcp inspect horizon.py:mcp` with the Horizon configuration, i.e. the
  same inspection Horizon performs at build time.

**Steps:**

1. Push the repository to GitHub (public or private).
2. Sign in at [horizon.prefect.io](https://horizon.prefect.io) with GitHub, create a hosted
   server from the repository and set the **entrypoint** to `horizon.py:mcp`. Keep **Horizon
   authentication** enabled (the default): only signed-in members of your Horizon organisation,
   or Horizon API keys, can call the server.
3. Before the first build, add the **environment variables** (Settings → Environment Variables;
   they are encrypted and available at build and run time):

   | Variable | Value | Why |
   |---|---|---|
   | `APP_ENV` | `production` | fail-closed validation |
   | `AUTH_MODE` | `platform` | Horizon's gateway authenticates callers; no in-process bearer token |
   | `YOUTUBE_API_KEY` | your Google key | optional; only `youtube_search_videos` needs it |
   | `LOG_FORMAT` | `json` (default) | Horizon captures stdout/stderr as server logs; secrets are redacted |

   Do **not** set `MCP_TOKEN_SHA256`, `AUTH_DISABLED`, `MCP_DOMAIN`, `HOST` or `PORT`: the first
   two are rejected in platform mode, the rest are owned by Horizon. Variable names starting with
   `FASTMCP_CLOUD_` or `HORIZON_` are reserved by the platform.
4. Deploy. Horizon builds (dependency install → `fastmcp inspect` of the entrypoint → artifact),
   publishes `https://<name>.fastmcp.app/mcp`, redeploys on every push to `main` and builds
   preview deployments for pull requests. Test with the built-in Inspector or ChatMCP, then use
   the connection snippets Horizon shows for Claude Code, Cursor, Claude Desktop, etc. — the
   client authenticates through Horizon's OAuth, not with `TUBETRACE_MCP_TOKEN`.

Check locally what Horizon will see at build time:

```bash
APP_ENV=production AUTH_MODE=platform uv run fastmcp inspect horizon.py:mcp
```

(If your local `.env` sets `AUTH_DISABLED=true` or a digest, also pass `AUTH_DISABLED=false
MCP_TOKEN_SHA256=` — real environment variables override `.env`, and platform mode refuses an
ambiguous configuration on purpose.)

**Alternative: keep the bearer token on Horizon.** If you disable Horizon authentication for the
server (Developer/Enterprise plans), Horizon passes requests through unchanged and your server
owns authentication again: set `AUTH_MODE=bearer` (or leave it unset), `MCP_TOKEN_SHA256` and
`MCP_DOMAIN=<name>.fastmcp.app`, and clients send `Authorization: Bearer <token>` as for the
self-hosted setup. Never disable Horizon authentication while `AUTH_MODE=platform` is set — the
server would be public.

**Behavioural differences on Horizon (from the platform documentation):**

- Horizon runs the FastMCP object with its own HTTP settings: sessions are stateful and routed by
  the gateway (`mcp-session-id`, 24 h TTL; the server itself keeps no per-session state), only
  `POST /mcp` is forwarded (`GET`/`DELETE /mcp` answer 405 at the gateway), and the Caddy layer,
  the strict Host/Origin guard, `MAX_REQUEST_BODY_BYTES` and `MCP_JSON_RESPONSE` from the
  self-hosted setup do not apply. `/healthz` exists on the server but is not reachable through
  the gateway.
- Limits: 170 s per request end-to-end (`TOOL_TIMEOUT_SECONDS`, 45 s, stays well below), 6 MB
  request/response, 1024 MB memory, ephemeral filesystem; compute starts on demand, so the first
  request after idling is slower (`horizon.py` keeps import-time work small).
- The cache and rate limit remain process-local; Horizon may run more than one instance.
- Horizon runs in AWS `us-east-1` with shared egress addresses. The unofficial transcript
  provider is often blocked from cloud IP ranges: `youtube_list_transcripts` and
  `youtube_get_transcript` may return `UPSTREAM_BLOCKED` there while `youtube_search_videos`
  (official API) keeps working. This server does not bypass blocks; see
  [Unofficial transcript provider, blocking and legal notes](#unofficial-transcript-provider-blocking-and-legal-notes).
- Horizon injects `horizon-actor*` headers with the verified caller identity; the server does not
  read them (single-owner design), but they appear in Horizon's request logs.

## Authentication: client token and server digest

- `uv run tubetrace-mcp generate-token` creates a token from **32 random bytes**
  (`secrets.token_urlsafe`) and its SHA-256 digest.
- The **client** keeps the **token** (for example in the `TUBETRACE_MCP_TOKEN` variable of a
  secrets manager or a shell profile with `600` permissions) and sends
  `Authorization: Bearer <token>`.
- The **server** stores only the **digest** in `MCP_TOKEN_SHA256` and compares the digest of the
  presented token in constant time (`hmac.compare_digest`). The token is never stored or logged
  on the server.
- All MCP requests, including `initialize` and `tools/list`, are protected. A missing or wrong
  token → **HTTP 401** with `WWW-Authenticate: Bearer`. A token in the query string is **never
  accepted**.
- The client token is never forwarded to Google or YouTube.
- **Rotation**: generate a new pair, set `MCP_TOKEN_SHA256=<old>,<new>`, update the clients, then
  keep only the new digest. For a compromised token, remove its digest.
- Digest of an existing token: `TUBETRACE_MCP_TOKEN=... uv run tubetrace-mcp hash-token`
  (or `--stdin`) so the token never appears in command arguments.

This is not an OAuth authorization server: clients that require OAuth discovery/login are not
supported. For development, `AUTH_DISABLED=true` works only with `APP_ENV=development` and only
when set explicitly.

If you need OAuth for clients, put the server behind a managed MCP gateway that provides it and
set `AUTH_MODE=platform` (see [Deploying to Prefect Horizon](#deploying-to-prefect-horizon)):
the gateway authenticates callers and this process performs no token verification. The mode
is never inferred: it must be set explicitly, and setting `MCP_TOKEN_SHA256` or `AUTH_DISABLED`
together with it is a startup error, so the configuration can never be ambiguous.

## Connecting clients (Codex, Claude Code, FastMCP)

### Codex

Format checked against the [Codex MCP documentation](https://developers.openai.com/codex/mcp)
on 2026-09-13 (`~/.codex/config.toml` or a project-scoped `.codex/config.toml`). Example:
[`examples/codex-config.toml`](examples/codex-config.toml).

```toml
[mcp_servers.tubetrace]
url = "https://mcp.example.com/mcp"
bearer_token_env_var = "TUBETRACE_MCP_TOKEN"
```

The difference between the variables: `TUBETRACE_MCP_TOKEN` is the **token on the client**
(Codex injects it into `Authorization`), `MCP_TOKEN_SHA256` is the **digest on the server**. The
values differ and neither belongs in a repository. This project does not modify your real
`~/.codex/config.toml`.

### Claude Code

Syntax checked against the [Claude Code documentation](https://code.claude.com/docs/en/mcp)
on 2026-09-13:

```bash
claude mcp add --transport http tubetrace https://mcp.example.com/mcp --header "Authorization: Bearer ${TUBETRACE_MCP_TOKEN}"
```

### FastMCP Client (Python)

See [`examples/client.py`](examples/client.py):

```python
from fastmcp import Client
from fastmcp.client.auth import BearerAuth
from fastmcp.client.transports import StreamableHttpTransport

transport = StreamableHttpTransport("https://mcp.example.com/mcp", auth=BearerAuth(token))
async with Client(transport) as client:
    tools = await client.list_tools()
    page = await client.call_tool("youtube_get_transcript", {"video": "dQw4w9WgXcQ"})
    print(page.structured_content)
```

## MCP tools

All tools carry the annotations `readOnlyHint=true`, `destructiveHint=false`,
`idempotentHint=true`, `openWorldHint=true`, have typed input/output schemas and return
**structured content** (plus the same JSON in a text block for clients without structured
output support).

### `youtube_search_videos`

| Parameter | Value |
|---|---|
| `query` | non-empty string, ≤ 256 characters |
| `max_results` | 1–50, default 10 |
| `page_token` | `next_page_token` from the previous page |
| `channel_id` | `UC...` |
| `published_after`, `published_before` | RFC 3339 **with a time zone**, e.g. `2024-01-01T00:00:00Z` |
| `order` | `relevance` (default), `date`, `viewCount`, `rating`, `title` |
| `relevance_language`, `region_code` | `uk`, `UA` |
| `video_duration` | `any`, `short`, `medium`, `long` |
| `caption_filter` | `any`, `closedCaption`, `none` — filters by caption **availability** |
| `safe_search` | `moderate` (default), `strict`, `none` |

One call = one request `GET https://www.googleapis.com/youtube/v3/search?part=snippet&type=video&q=...`.

```json
{"query": "fastmcp streamable http", "max_results": 5, "order": "date", "caption_filter": "closedCaption"}
```

Result: `items[]` (`video_id`, `video_url`, `title`, `description`, `channel_id`,
`channel_title`, `published_at`, `thumbnail_url`, `live_broadcast_content`),
`next_page_token`, `prev_page_token`, `results_per_page`, `total_results_estimate` +
`total_results_note` (Google returns an **estimate**, not a guaranteed count), `region_code`,
`retrieved_at`, `provider`, `cache_hit`. HTML entities in titles/descriptions are decoded;
invalid dates in the response do not break the page (`published_at: null`).

### `youtube_list_transcripts`

```json
{"video": "https://youtu.be/dQw4w9WgXcQ"}
```

Accepts an 11-character ID or a URL from the allowlisted hosts `youtube.com`,
`www/m/music.youtube.com`, `youtu.be`, `youtube-nocookie.com` in the forms `watch?v=`,
`youtu.be/`, `shorts/`, `embed/`, `live/`, `v/`. The hostname is parsed with the standard URL
parser: `youtube.com.evil.example`, userinfo (`user@`), non-standard ports and schemes are
rejected (`INVALID_VIDEO_INPUT`). The given URL is never fetched and its redirects are never
followed — only the ID is extracted.

Returns `tracks[]` (`language`, `language_code`, `is_generated`, `is_translatable`),
`default_selection_policy`, `retrieved_at`, `cache_hit`. Track text is not downloaded.

### `youtube_get_transcript`

| Parameter | Value |
|---|---|
| `video` | ID or URL |
| `languages` | codes in priority order, e.g. `["uk", "en"]` (≤ 10) |
| `prefer_manual` | `true` (default): a manual track wins over an auto-generated one **within a language** |
| `start_seconds`, `end_seconds` | time range (seconds) |
| `offset` | ≥ 0 (default 0) — index within the matched segments |
| `limit` | 1–500 (default 100) |
| `format` | `segments` (default) or `text` |

**Track selection algorithm** (deterministic, documented in `default_selection_policy`):

1. If `languages` is given: the list order outranks `prefer_manual`. For each code, tracks with
   an exact code match (case-insensitive) are considered first, then tracks with the same base
   language (`en` ↔ `en-US`). Inside a tier, `prefer_manual` chooses between the manual and the
   auto-generated track (falling back to the other kind if the preferred one is missing). If none
   of the languages is available — `NO_MATCHING_TRANSCRIPT` with the list of available languages.
2. If `languages` is omitted: the preferred kind (manual if `prefer_manual=true`) and the
   **first track in the provider's listing order** are used. This is **not necessarily the
   video's original language** — the provider does not expose that information. The language
   actually used is returned in `language_code`; `selection` is `requested_language` or
   `default_policy`.
3. No translation is performed; the language is never switched silently.

**Segment and page semantics:**

- `index` is the position of the segment in the **full** transcript (0…`total_segments-1`) and
  never changes with filters.
- A time range keeps segments whose interval `[start, start+duration)` **intersects**
  `[start_seconds, end_seconds)`; zero-length segments are kept when their start lies in the
  range. Timings are not altered and text is never cut "at a phrase boundary".
- `offset`/`limit` are then applied to the matched segments (`matched_segments`).
- `format=segments`: every segment has `index`, `text`, `start_seconds`, `duration_seconds`,
  `end_seconds`, `timestamp_url`. `format=text`: `text` is only this page's text with lines
  separated by `\n`; segments are not duplicated.
- The `MAX_RESPONSE_BYTES` limit applies to the structured payload of the page. If a page is
  shortened, `truncated_by_size_limit=true` and `next_offset` reflects the segments **actually
  returned**. An empty page with the same `next_offset` is impossible; if a single segment does
  not fit on its own, the explicit error `RESPONSE_TOO_LARGE` is returned.
- Response: `video_id`, `video_url`, `language`, `language_code`, `is_generated`, `selection`,
  `provider`, `retrieved_at`, `cache_hit`, `format`, `total_segments`, `matched_segments`,
  `offset`, `returned_segments`, `next_offset`, `has_more`, `truncated_by_size_limit`,
  `start_seconds`, `end_seconds`, `segments` | `text`.

Examples:

```json
{"video": "dQw4w9WgXcQ", "languages": ["uk", "en"], "limit": 50}
```

```json
{"video": "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "start_seconds": 60, "end_seconds": 120, "format": "text"}
```

## Pagination and fetching a full transcript in a loop

The full transcript is downloaded from upstream **once** and cached (1 hour by default); the
following pages are served from the cache (`cache_hit=true`). The cache is an optimisation: if it
is gone, the page simply re-downloads the transcript.

```python
offset, parts = 0, []
while True:
    page = await client.call_tool(
        "youtube_get_transcript",
        {"video": video, "offset": offset, "limit": 500, "format": "text"},
        raise_on_error=False,
    )
    data = page.structured_content
    if page.is_error:
        raise RuntimeError(data["error"])
    parts.append(data["text"])
    if not data["has_more"]:
        break
    offset = data["next_offset"]
full_text = "\n".join(parts)
```

For search: pass `next_page_token` as `page_token` until it becomes `null`. Every page is a
separate `search.list` call and a separate quota unit.

## Errors

Expected failures are returned as an **MCP tool error** (`isError: true`) with the payload:

```json
{"error": {"code": "NO_MATCHING_TRANSCRIPT", "message": "...", "retryable": false, "details": {"requested": ["fr"], "available": [{"language_code": "en", "language": "English", "is_generated": false}]}}}
```

| Code | Meaning | retryable |
|---|---|---|
| `INVALID_ARGUMENT` | invalid parameters (e.g. a date without a time zone, `end_seconds ≤ start_seconds`) | no |
| `INVALID_VIDEO_INPUT` | neither an ID nor a URL from the allowlisted hosts | no |
| `GOOGLE_API_NOT_CONFIGURED` | no `YOUTUBE_API_KEY`; transcripts keep working | no |
| `GOOGLE_API_KEY_INVALID` | key invalid/forbidden, API not enabled | no |
| `GOOGLE_QUOTA_EXCEEDED` | the project's daily quota is exhausted | no |
| `NO_MATCHING_TRANSCRIPT` | no track in the requested language (see `details.available`) | no |
| `TRANSCRIPTS_DISABLED` | subtitles are disabled for the video | no |
| `VIDEO_UNAVAILABLE` | video unavailable/private/age-restricted (`details.reason`) | no |
| `TRANSCRIPT_TOO_LARGE` | transcript exceeds `TRANSCRIPT_MAX_SEGMENTS`/`TRANSCRIPT_MAX_BYTES` | no |
| `RESPONSE_TOO_LARGE` | a single segment does not fit into `MAX_RESPONSE_BYTES` | no |
| `RATE_LIMITED` | local per-process limit (`details.retry_after_seconds`) | yes |
| `SERVER_BUSY` | the concurrent upstream request limit is exhausted | yes |
| `UPSTREAM_BLOCKED` | YouTube blocked the provider (IP/request block, PO token) — **not** "no subtitles" | no |
| `UPSTREAM_RATE_LIMITED` | 429 from Google/YouTube | yes |
| `UPSTREAM_TIMEOUT` | upstream timeout or the tool time budget | yes |
| `UPSTREAM_ERROR` | other upstream/network/parsing failure (`details.reason`) | depends |

JSON-schema validation errors of the arguments (for example `max_results: 51`) are also returned
by FastMCP with `isError: true`, but with the pydantic validation text instead of a code. Protocol
and auth errors follow the MCP/HTTP rules (401/403/421/413, JSON-RPC error).

Logs are structured (JSON): `request_id`, `tool`, `latency_ms`, `provider`, `cache_hit`,
`error_code`, `status`. `Authorization`, the API key, full transcripts and `.env` are never logged;
redaction is also applied to exception text.

## Troubleshooting

| Symptom | What to check |
|---|---|
| Server does not start: `Authentication is not configured` | set `MCP_TOKEN_SHA256` or (dev only) `AUTH_DISABLED=true` |
| `AUTH_DISABLED=true is not allowed when APP_ENV=production` | remove `AUTH_DISABLED` from `.env` |
| `MCP_TOKEN_SHA256 is ignored when AUTH_MODE=platform` / `AUTH_DISABLED has no effect when AUTH_MODE=platform` | platform mode must be unambiguous: remove the digest / `AUTH_DISABLED`, or switch to `AUTH_MODE=bearer` |
| Horizon build fails at the inspect step with `MCP_TOKEN_SHA256 is required` | set `AUTH_MODE=platform` (Horizon authentication enabled) or a digest (Horizon authentication disabled) in the Horizon environment variables, then rebuild |
| `MCP_DOMAIN (or ALLOWED_HOSTS) must be set in production` | set `MCP_DOMAIN` |
| 401 | the client token does not match the digest; verify with `hash-token`; a token in the query string never works |
| 403 `Forbidden Origin` | a browser client with a foreign Origin; add it to `ALLOWED_ORIGINS` |
| 421 `Misdirected Request` | Host does not match `MCP_DOMAIN`/`ALLOWED_HOSTS` (e.g. you connect by IP) |
| 413 | request body larger than `MAX_REQUEST_BODY_BYTES` / 1 MB in Caddy |
| `GOOGLE_API_NOT_CONFIGURED` | `YOUTUBE_API_KEY` did not reach the container (`docker compose exec app tubetrace-mcp check-config`) |
| `GOOGLE_API_KEY_INVALID` (`accessNotConfigured`) | the API is not enabled in the project or the key is restricted to another API/IP |
| `GOOGLE_QUOTA_EXCEEDED` | the 100 daily `search.list` calls are used up; wait for the reset (PT) or request a quota increase |
| `UPSTREAM_BLOCKED` | YouTube blocks the server's IP (common for cloud/datacenter); check the provider from this network — the server does not bypass blocks |
| `UPSTREAM_ERROR` with `unparsable_response` | YouTube changed something or soft-blocks; update `youtube-transcript-api` |
| Caddy does not obtain a certificate | DNS A/AAAA → this server? ports 80/443 open? `docker compose logs caddy`; Let's Encrypt rate limits |
| The client "hangs" on a long call | align `TOOL_TIMEOUT_SECONDS` < Caddy `response_header_timeout` < the client timeout |

## Unofficial transcript provider, blocking and legal notes

- `youtube-transcript-api` uses an **undocumented part of YouTube**: it is an unofficial way of
  retrieving **already existing** subtitles with no availability guarantee. The library's
  availability does not imply Google's approval.
- YouTube frequently **blocks cloud provider IPs** (`UPSTREAM_BLOCKED`, `IpBlocked`/`RequestBlocked`,
  PO token requirement). The server returns a diagnostic error and does **not** bypass blocks, use
  cookies/login, rotate proxies or retry endlessly. In practice this means transcripts may be
  unavailable on some VPS hosts while search through the official API keeps working.
- Some videos have no subtitles (`TRANSCRIPTS_DISABLED`) or only automatic ones.
- Before using this, review the [YouTube Terms of Service](https://www.youtube.com/t/terms), the
  [YouTube API Services Terms](https://developers.google.com/youtube/terms/api-services-terms-of-service)
  and the rights to reuse subtitle text: a transcript is the video author's content.

## Known limitations

- One worker, an in-memory cache and rate limit — no shared state between processes/replicas
  (including several Horizon instances).
- `AUTH_MODE=platform` trusts the network path: it is only safe when the process cannot be reached
  except through the authenticating gateway. The server does not verify the gateway's identity
  headers.
- `total_results_estimate` is Google's estimate; the real pagination depth is smaller.
- The provider's track order does not guarantee the "original" language.
- The on-the-wire response is roughly twice `MAX_RESPONSE_BYTES`, because the structured content
  is duplicated in a text block for clients without structured output support.
- The FastMCP Client in its new negotiation mode (`mode="auto"`, not the standard `initialize`)
  gets "Method not found" for `ping`; standard clients (`initialize` handshake, such as Codex) ping
  normally — this is covered by a test.
- Request bodies are limited (1 MB in Caddy, `MAX_REQUEST_BODY_BYTES` in the application), the
  number of `languages` is capped at 10 and `query` at 256 characters.

License: MIT.
