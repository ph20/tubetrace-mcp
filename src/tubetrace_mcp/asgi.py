"""ASGI entrypoint: ``uvicorn --factory tubetrace_mcp.asgi:application``."""

from __future__ import annotations

from starlette.applications import Starlette

from .logging_config import configure_logging
from .server import create_app
from .settings import Settings


def application() -> Starlette:
    settings = Settings()
    configure_logging(settings.log_level, settings.log_format, secrets=settings.redaction_secrets())
    return create_app(settings)
