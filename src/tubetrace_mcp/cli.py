"""Command-line interface: serve, generate-token, hash-token, check-config."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Sequence

from pydantic import ValidationError

from . import __version__
from .auth import generate_token, sha256_hex
from .settings import Settings

logger = logging.getLogger(__name__)


def _load_settings() -> Settings:
    return Settings()


def cmd_serve(_: argparse.Namespace) -> int:
    import uvicorn

    from .logging_config import configure_logging
    from .server import create_app

    try:
        settings = _load_settings()
    except ValidationError as exc:
        print(f"Configuration error:\n{exc}", file=sys.stderr)
        return 2
    configure_logging(settings.log_level, settings.log_format, secrets=settings.redaction_secrets())
    app = create_app(settings)
    logger.info(
        "server_starting",
        extra={
            "version": __version__,
            "app_env": settings.app_env,
            "host": settings.host,
            "port": settings.port,
            "auth_mode": settings.auth_mode,
            "auth_enabled": settings.auth_enabled,
            "google_configured": settings.google_configured,
            "transcript_proxy": settings.transcript_proxy_endpoint,
            "allowed_hosts": settings.effective_allowed_hosts,
        },
    )
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        proxy_headers=settings.proxy_headers,
        forwarded_allow_ips=settings.forwarded_allow_ips,
        log_config=None,
        access_log=True,
        server_header=False,
        timeout_keep_alive=15,
        timeout_graceful_shutdown=10,
    )
    return 0


def cmd_generate_token(args: argparse.Namespace) -> int:
    token, digest = generate_token()
    if args.digest_only:
        print(digest)
        return 0
    print("Generated a new TubeTrace MCP bearer token.\n")
    print("1) CLIENT SIDE - store this token in your MCP client (e.g. TUBETRACE_MCP_TOKEN):")
    print(f"   {token}\n")
    print("2) SERVER SIDE - put only the SHA-256 digest into the server environment:")
    print(f"   MCP_TOKEN_SHA256={digest}\n")
    print(
        "Keep the token in a secrets manager or your shell profile with restricted "
        "permissions; never commit it. To rotate: generate a new pair, set MCP_TOKEN_SHA256 "
        "to 'old_digest,new_digest', update clients, then remove the old digest."
    )
    return 0


def cmd_hash_token(args: argparse.Namespace) -> int:
    token = sys.stdin.readline().rstrip("\r\n") if args.stdin else os.environ.get(args.env_var)
    if not token:
        print(
            f"No token found. Export {args.env_var} or pass --stdin and pipe the token.",
            file=sys.stderr,
        )
        return 2
    print(sha256_hex(token))
    return 0


def cmd_check_config(_: argparse.Namespace) -> int:
    try:
        settings = _load_settings()
    except ValidationError as exc:
        print(f"Configuration error:\n{exc}", file=sys.stderr)
        return 2
    print(f"tubetrace-mcp {__version__}")
    print(f"APP_ENV            : {settings.app_env}")
    print(f"listen             : {settings.host}:{settings.port} (path /mcp, stateless HTTP)")
    print(f"auth               : {settings.auth_summary}")
    print(f"token digests      : {len(settings.token_digests)} configured")
    print(
        f"google search      : {'configured' if settings.google_configured else 'NOT configured'}"
    )
    proxy = settings.transcript_proxy_endpoint
    print(
        "transcript proxy   : "
        + (f"{proxy} (transcripts only)" if proxy else "not configured (direct connection)")
    )
    print(f"MCP_DOMAIN         : {settings.mcp_domain or '-'}")
    print(
        f"allowed hosts      : {', '.join(settings.effective_allowed_hosts) or '(loopback only)'}"
    )
    print(f"allowed origins    : {', '.join(settings.allowed_origins) or '(same-origin only)'}")
    print(f"json response      : {settings.mcp_json_response}")
    print(f"log                : {settings.log_level} / {settings.log_format}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tubetrace-mcp",
        description="Personal read-only MCP server for YouTube search and transcripts.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="Run the MCP server (default).")
    serve.set_defaults(func=cmd_serve)

    gen = sub.add_parser(
        "generate-token", help="Generate a client bearer token and its SHA-256 digest."
    )
    gen.add_argument("--digest-only", action="store_true", help="Print only the digest.")
    gen.set_defaults(func=cmd_generate_token)

    hsh = sub.add_parser("hash-token", help="Compute the SHA-256 digest of an existing token.")
    hsh.add_argument(
        "--env-var",
        default="TUBETRACE_MCP_TOKEN",
        help="Environment variable holding the token (default: TUBETRACE_MCP_TOKEN).",
    )
    hsh.add_argument("--stdin", action="store_true", help="Read the token from stdin instead.")
    hsh.set_defaults(func=cmd_hash_token)

    chk = sub.add_parser("check-config", help="Validate configuration and print a summary.")
    chk.set_defaults(func=cmd_check_config)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        args.func = cmd_serve
    result: int = args.func(args)
    return result


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
