from __future__ import annotations

import hashlib
import io
from pathlib import Path

import pytest

from tubetrace_mcp.cli import main


def test_generate_token_prints_pair(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["generate-token"]) == 0
    out = capsys.readouterr().out
    lines = [line.strip() for line in out.splitlines() if line.strip()]
    token = next(line for line in lines if len(line) >= 43 and " " not in line and "=" not in line)
    digest_line = next(line for line in lines if line.startswith("MCP_TOKEN_SHA256="))
    digest = digest_line.split("=", 1)[1]
    assert hashlib.sha256(token.encode()).hexdigest() == digest
    assert "rotate" in out.lower()


def test_generate_token_digest_only(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["generate-token", "--digest-only"]) == 0
    out = capsys.readouterr().out.strip()
    assert len(out) == 64


def test_hash_token_from_env_and_stdin(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("TUBETRACE_MCP_TOKEN", "my-token")
    assert main(["hash-token"]) == 0
    assert capsys.readouterr().out.strip() == hashlib.sha256(b"my-token").hexdigest()
    monkeypatch.delenv("TUBETRACE_MCP_TOKEN")
    monkeypatch.setattr("sys.stdin", io.StringIO("other\n"))
    assert main(["hash-token", "--stdin"]) == 0
    assert capsys.readouterr().out.strip() == hashlib.sha256(b"other").hexdigest()
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert main(["hash-token", "--stdin"]) == 2


def test_check_config(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)  # no .env file here
    monkeypatch.setenv("APP_ENV", "production")
    assert main(["check-config"]) == 2
    assert "MCP_TOKEN_SHA256" in capsys.readouterr().err
    monkeypatch.setenv("MCP_TOKEN_SHA256", "c" * 64)
    monkeypatch.setenv("MCP_DOMAIN", "mcp.example.com")
    monkeypatch.setenv("YOUTUBE_API_KEY", "AIzaSyFakeKeyForTests1234567890")
    assert main(["check-config"]) == 0
    out = capsys.readouterr().out
    assert "auth               : enabled" in out
    assert "google search      : configured" in out
    assert "AIzaSy" not in out
