"""Application configuration loaded from environment variables (and ``.env``).

Three kinds of values are distinguished:

* **server secrets** - ``YOUTUBE_API_KEY`` (never sent to MCP clients, never logged);
* **client credential material** - ``MCP_TOKEN_SHA256`` is the SHA-256 digest of the
  bearer token that MCP clients present; the token itself never lives on the server;
* **non-secret parameters** - hostnames, ports, limits, timeouts and cache settings.

Validation is fail-closed: an invalid or missing auth configuration stops startup
instead of silently allowing anonymous access.
"""

from __future__ import annotations

import re
from functools import cached_property
from typing import Annotated, Any, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

_HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")

AppEnv = Literal["development", "production"]


def _split_csv(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, list | tuple | set):
        return [str(item).strip() for item in value if str(item).strip()]
    raise ValueError("expected a comma-separated string or a list")


class Settings(BaseSettings):
    """Runtime configuration. Field names map to upper-case environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- deployment -------------------------------------------------------------
    app_env: AppEnv = "development"
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    mcp_domain: str | None = Field(
        default=None, description="Public hostname served by Caddy (e.g. mcp.example.com)."
    )
    acme_email: str | None = Field(
        default=None, description="Contact e-mail for ACME/Let's Encrypt (used by Caddy only)."
    )
    allowed_hosts: Annotated[list[str], NoDecode] = Field(
        default_factory=list,
        description="Extra Host header values accepted besides MCP_DOMAIN and loopback names.",
    )
    allowed_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=list,
        description="Browser Origins allowed to call the server (CLI clients send no Origin).",
    )
    proxy_headers: bool = Field(
        default=True, description="Honour X-Forwarded-* headers from trusted proxies."
    )
    forwarded_allow_ips: str = Field(
        default="127.0.0.1",
        description="Proxy addresses/networks trusted for X-Forwarded-* (uvicorn syntax).",
    )
    mcp_json_response: bool = Field(
        default=True,
        description="Answer MCP POST requests with application/json instead of SSE streams.",
    )
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_format: Literal["json", "text"] = "json"

    # --- secrets & auth ---------------------------------------------------------
    youtube_api_key: SecretStr | None = Field(
        default=None, description="Google API key restricted to YouTube Data API v3."
    )
    mcp_token_sha256: str | None = Field(
        default=None,
        description="SHA-256 hex digest(s) of accepted bearer tokens (comma-separated).",
    )
    auth_disabled: bool = Field(
        default=False,
        description="Development only: explicitly allow anonymous access on loopback.",
    )

    # --- upstream timeouts, retries, concurrency -------------------------------------
    google_api_base_url: str = "https://www.googleapis.com/youtube/v3"
    google_connect_timeout_seconds: float = Field(default=5.0, gt=0, le=60)
    google_read_timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    transcript_connect_timeout_seconds: float = Field(default=5.0, gt=0, le=60)
    transcript_read_timeout_seconds: float = Field(default=15.0, gt=0, le=120)
    upstream_max_retries: int = Field(default=2, ge=0, le=5)
    upstream_retry_budget_seconds: float = Field(default=20.0, gt=0, le=120)
    upstream_max_concurrency: int = Field(default=4, ge=1, le=64)
    upstream_queue_timeout_seconds: float = Field(default=10.0, ge=0, le=120)
    tool_timeout_seconds: float = Field(default=45.0, gt=0, le=600)

    # --- cache --------------------------------------------------------------------
    search_cache_ttl_seconds: float = Field(default=300.0, ge=0)
    transcript_cache_ttl_seconds: float = Field(default=3600.0, ge=0)
    cache_max_entries: int = Field(default=512, ge=0)
    cache_max_bytes: int = Field(default=64 * 1024 * 1024, ge=0)

    # --- limits -------------------------------------------------------------------
    max_response_bytes: int = Field(
        default=200_000,
        ge=4096,
        description="Maximum size of the structured payload of one tool result.",
    )
    transcript_max_segments: int = Field(default=50_000, ge=1)
    transcript_max_bytes: int = Field(default=8 * 1024 * 1024, ge=1024)
    google_max_response_bytes: int = Field(default=2 * 1024 * 1024, ge=1024)
    max_request_body_bytes: int = Field(default=1024 * 1024, ge=1024)
    rate_limit_per_minute: int = Field(default=120, ge=1)
    rate_limit_burst: int = Field(default=30, ge=1)

    # --- validators ---------------------------------------------------------------
    @field_validator(
        "youtube_api_key", "mcp_token_sha256", "mcp_domain", "acme_email", mode="before"
    )
    @classmethod
    def _blank_to_none(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("allowed_hosts", "allowed_origins", mode="before")
    @classmethod
    def _csv(cls, value: Any) -> list[str]:
        return _split_csv(value)

    @field_validator("mcp_domain")
    @classmethod
    def _domain(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip().lower()
        if "/" in value or ":" in value or " " in value:
            raise ValueError("MCP_DOMAIN must be a bare hostname such as mcp.example.com")
        return value

    @model_validator(mode="after")
    def _validate_auth(self) -> Settings:
        digests = self.token_digests
        if self.app_env == "production":
            if self.auth_disabled:
                raise ValueError(
                    "AUTH_DISABLED=true is not allowed when APP_ENV=production; "
                    "configure MCP_TOKEN_SHA256 instead."
                )
            if not digests:
                raise ValueError(
                    "MCP_TOKEN_SHA256 is required when APP_ENV=production. Generate it with "
                    "'tubetrace-mcp generate-token'."
                )
            if not self.mcp_domain and not self.allowed_hosts:
                raise ValueError(
                    "MCP_DOMAIN (or ALLOWED_HOSTS) must be set in production so the Host "
                    "header of your public hostname is accepted."
                )
        elif not digests and not self.auth_disabled:
            raise ValueError(
                "Authentication is not configured. Set MCP_TOKEN_SHA256 (see "
                "'tubetrace-mcp generate-token') or, for local development only, "
                "set AUTH_DISABLED=true explicitly."
            )
        return self

    # --- derived ------------------------------------------------------------------
    @cached_property
    def token_digests(self) -> list[str]:
        if not self.mcp_token_sha256:
            return []
        digests = _split_csv(self.mcp_token_sha256)
        for digest in digests:
            if not _HEX64.match(digest):
                raise ValueError(
                    "MCP_TOKEN_SHA256 must contain 64-character hex SHA-256 digest(s), "
                    "comma-separated"
                )
        return [d.lower() for d in digests]

    @property
    def auth_enabled(self) -> bool:
        if self.app_env == "production":
            return True
        return not self.auth_disabled

    @property
    def effective_allowed_hosts(self) -> list[str]:
        hosts = list(self.allowed_hosts)
        if self.mcp_domain and self.mcp_domain not in hosts:
            hosts.append(self.mcp_domain)
        return hosts

    @property
    def google_configured(self) -> bool:
        return self.youtube_api_key is not None and bool(
            self.youtube_api_key.get_secret_value().strip()
        )

    def redaction_secrets(self) -> list[str]:
        secrets: list[str] = []
        if self.youtube_api_key is not None:
            secrets.append(self.youtube_api_key.get_secret_value())
        return secrets
