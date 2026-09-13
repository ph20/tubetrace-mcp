from __future__ import annotations

import pytest
from pydantic import ValidationError

from tests.conftest import make_settings

DIGEST = "a" * 64


def test_production_requires_digest() -> None:
    with pytest.raises(ValidationError, match="MCP_TOKEN_SHA256 is required"):
        make_settings(app_env="production", auth_disabled=False, mcp_domain="mcp.example.com")


def test_production_rejects_auth_disabled() -> None:
    with pytest.raises(ValidationError, match="not allowed"):
        make_settings(
            app_env="production",
            auth_disabled=True,
            mcp_token_sha256=DIGEST,
            mcp_domain="x.example",
        )


def test_production_requires_domain_or_allowed_hosts() -> None:
    with pytest.raises(ValidationError, match="MCP_DOMAIN"):
        make_settings(app_env="production", auth_disabled=False, mcp_token_sha256=DIGEST)
    settings = make_settings(
        app_env="production",
        auth_disabled=False,
        mcp_token_sha256=DIGEST,
        allowed_hosts="mcp.example.com",
    )
    assert settings.auth_enabled is True
    assert settings.effective_allowed_hosts == ["mcp.example.com"]


def test_production_valid() -> None:
    settings = make_settings(
        app_env="production",
        auth_disabled=False,
        mcp_token_sha256=DIGEST.upper() + ", " + "b" * 64,
        mcp_domain="MCP.Example.com",
        allowed_hosts="internal.example, mcp.example.com",
    )
    assert settings.token_digests == ["a" * 64, "b" * 64]
    assert settings.mcp_domain == "mcp.example.com"
    assert settings.effective_allowed_hosts == ["internal.example", "mcp.example.com"]


def test_development_requires_explicit_choice() -> None:
    with pytest.raises(ValidationError, match="Authentication is not configured"):
        make_settings(auth_disabled=False)
    dev = make_settings(auth_disabled=True)
    assert dev.auth_enabled is False
    with_token = make_settings(auth_disabled=False, mcp_token_sha256=DIGEST)
    assert with_token.auth_enabled is True


def test_invalid_digest_rejected() -> None:
    with pytest.raises(ValidationError, match="64-character hex"):
        make_settings(auth_disabled=False, mcp_token_sha256="not-a-digest")


def test_blank_secret_means_not_configured() -> None:
    settings = make_settings(youtube_api_key="   ")
    assert settings.youtube_api_key is None
    assert settings.google_configured is False
    configured = make_settings(youtube_api_key="AIzaSyExample")
    assert configured.google_configured is True
    assert "AIzaSyExample" not in repr(configured)
    assert configured.redaction_secrets() == ["AIzaSyExample"]


def test_domain_validation_and_env_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError, match="bare hostname"):
        make_settings(mcp_domain="https://mcp.example.com/")
    monkeypatch.setenv("ALLOWED_ORIGINS", "https://app.example, https://other.example")
    monkeypatch.setenv("MCP_TOKEN_SHA256", DIGEST)
    monkeypatch.setenv("AUTH_DISABLED", "false")
    settings = make_settings(auth_disabled=False)
    assert settings.allowed_origins == ["https://app.example", "https://other.example"]
    assert settings.token_digests == [DIGEST]
