"""One audit line per MCP request: who called what, how it ended and how long it took.

This module is shared verbatim by rabotaua-mcp and tubetrace-mcp; keep both copies
identical.

Example (``LOG_FORMAT=text``, wrapped here)::

    2026-09-26T19:18:23.518Z INFO    audit mcp_request method=tools/call
      tool=search_vacancies status=ok latency_ms=412.7 user=agrynchuk@gmail.com
      actor=user role=admin client=ClaudeCode ua=Claude-User arguments=keywords,city_id
      request_id=4bf92f3577b34da6a3ce929d0e0e4736

Where the fields come from (verified on a live Prefect Horizon deployment):

* ``user``, ``actor``, ``role``: headers the Horizon gateway injects after it has
  authenticated the caller: ``horizon-actor-email`` (only actors that have an email),
  ``horizon-actor`` (the id, also for service accounts), ``horizon-actor-type`` and
  ``horizon-user-role``. The gateway removes client-supplied ``horizon-*`` headers, so
  these can be trusted. Without the gateway the verified access token is used, if any.
* ``client``, ``ua``: ``x-anthropic-client`` or the MCP ``clientInfo``, and the
  ``User-Agent``; reported by the client, informational only. Behind Horizon the MCP
  ``clientInfo`` belongs to the gateway's own client (``mcp 0.1.0``) and is ignored.
* ``ip``: not available behind Horizon (uvicorn only sees the Lambda Web Adapter on
  127.0.0.1 and the gateway sends no ``X-Forwarded-For``). Logged only when uvicorn
  sees a real peer: a direct connection or a reverse proxy it trusts.
* ``request_id``: the W3C ``traceparent`` trace id when present (Horizon's gateway,
  AWS X-Ray and Lambda share it), otherwise random. Every other line logged while the
  request runs carries the same ``request_id``.

Tool argument *names* are logged; values only with ``log_arguments=True``
(``LOG_TOOL_ARGUMENTS=true``). Horizon Request Logs already keep full payloads.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import re
import time
import uuid
from collections.abc import Mapping
from contextvars import ContextVar
from typing import Any

import fastmcp
from fastmcp.server.dependencies import get_access_token, get_http_request
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from starlette.requests import Request

from .logging_config import request_id_var

logger = logging.getLogger("audit")

_MAX_VALUE = 120
_MAX_ARGUMENTS = 300
_TRUE = frozenset({"1", "true", "yes", "on"})
_TRACEPARENT = re.compile(r"^[0-9a-f]{2}-([0-9a-f]{32})-[0-9a-f]{16}-[0-9a-f]{2}$")
_DEPLOYMENT = re.compile(r"/deployments/([0-9a-f-]{36})")
_LOOPBACK = ("127.", "::1", "localhost")

# Attributes every LogRecord has; an ``extra`` key with one of these names raises KeyError.
_RECORD_ATTRS = frozenset(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}

_extra: ContextVar[dict[str, Any] | None] = ContextVar("mcp_audit_extra", default=None)


def annotate(**fields: Any) -> None:
    """Add fields to the audit line of the request being handled (no-op outside one)."""
    extra = _extra.get()
    if extra is not None:
        extra.update(fields)


def behind_horizon() -> bool:
    """True inside a Prefect Horizon deployment (the platform sets ``FASTMCP_CLOUD_URL``)."""
    return bool(os.environ.get("FASTMCP_CLOUD_URL"))


def runtime_fields() -> dict[str, Any]:
    """Process facts for the ``server_started`` line (logged once per cold start)."""
    fields: dict[str, Any] = {
        "fastmcp": fastmcp.__version__,
        "python": platform.python_version(),
        "pid": os.getpid(),
    }
    if behind_horizon():
        deployment = _DEPLOYMENT.search(os.environ.get("AWS_LAMBDA_LOG_GROUP_NAME", ""))
        memory = os.environ.get("AWS_LAMBDA_FUNCTION_MEMORY_SIZE", "")
        fields.update(
            platform="horizon",
            url=os.environ.get("FASTMCP_CLOUD_URL"),
            deployment=deployment.group(1) if deployment else None,
            memory_mb=int(memory) if memory.isdigit() else None,
        )
    return fields


class AuditMiddleware(Middleware):
    """Log one ``mcp_request`` line per MCP request (see the module docstring)."""

    def __init__(self, *, log_arguments: bool | None = None) -> None:
        if log_arguments is None:
            log_arguments = os.environ.get("LOG_TOOL_ARGUMENTS", "").strip().lower() in _TRUE
        self._log_arguments = log_arguments
        self._behind_horizon = behind_horizon()
        self._cold_start = True

    async def on_request(
        self, context: MiddlewareContext[Any], call_next: CallNext[Any, Any]
    ) -> Any:
        request = _http_request()
        headers: Mapping[str, str] = request.headers if request is not None else {}
        method = context.method or "unknown"
        fields: dict[str, Any] = {"method": method}
        if method == "tools/call":
            fields["tool"] = getattr(context.message, "name", None)
        cold_start, self._cold_start = self._cold_start, False
        extra: dict[str, Any] = {}
        request_id = _trace_id(headers.get("traceparent")) or uuid.uuid4().hex
        request_id_token = request_id_var.set(request_id)
        extra_token = _extra.set(extra)
        status, error = "error", None
        started = time.perf_counter()
        try:
            result = await call_next(context)
            error = _result_error(result)
            status = "error" if error else "ok"
            return result
        except Exception as exc:
            error = _exception_name(exc)
            raise
        except BaseException:
            status = "cancelled"
            raise
        finally:
            fields.update(status=status, error=error)
            fields["latency_ms"] = round((time.perf_counter() - started) * 1000, 1)
            try:
                self._log(context, request, headers, fields, extra, cold_start, request_id)
            except Exception:  # the audit line must never change the response
                logger.exception("mcp_request_not_logged")
            finally:
                _extra.reset(extra_token)
                request_id_var.reset(request_id_token)

    def _log(
        self,
        context: MiddlewareContext[Any],
        request: Request | None,
        headers: Mapping[str, str],
        fields: dict[str, Any],
        extra: dict[str, Any],
        cold_start: bool,
        request_id: str,
    ) -> None:
        fields.update(self._caller(context, request, headers))
        if fields["method"] == "tools/call":
            fields["arguments"] = self._arguments(getattr(context.message, "arguments", None))
        fields.update(extra)
        if cold_start:
            fields["cold_start"] = True
        fields["request_id"] = request_id
        level = logging.INFO
        if fields["status"] != "ok":
            level = logging.WARNING
        elif fields["method"] == "ping":
            level = logging.DEBUG
        logger.log(level, "mcp_request", extra=_record_safe(fields))

    def _caller(
        self,
        context: MiddlewareContext[Any],
        request: Request | None,
        headers: Mapping[str, str],
    ) -> dict[str, Any]:
        user_agent = _clean(headers.get("user-agent"))
        client = _clean(headers.get("x-anthropic-client"))
        if client is None and not self._behind_horizon:
            client = _client_info(context)
        if client is None and user_agent:
            client = user_agent.split("/", 1)[0].split(" ", 1)[0]
        fields: dict[str, Any] = {
            "user": _clean(
                headers.get("horizon-actor-email")
                or headers.get("horizon-user-email")
                or headers.get("horizon-actor")
            )
            or _token_user()
            or "anonymous",
            "actor": _clean(headers.get("horizon-actor-type")),
            "role": _clean(headers.get("horizon-user-role")),
            "client": client,
            "ua": user_agent,
        }
        peer = request.client.host if request is not None and request.client else None
        if peer and not self._behind_horizon and not peer.startswith(_LOOPBACK):
            fields["ip"] = peer
        return fields

    def _arguments(self, arguments: Any) -> str | None:
        if not isinstance(arguments, Mapping) or not arguments:
            return None
        if not self._log_arguments:
            return ",".join(str(name) for name in arguments)
        text = json.dumps(arguments, ensure_ascii=False, default=str, separators=(",", ":"))
        return text if len(text) <= _MAX_ARGUMENTS else text[: _MAX_ARGUMENTS - 1] + "…"


def _record_safe(fields: dict[str, Any]) -> dict[str, Any]:
    """Rename keys that would clash with LogRecord attributes (``annotate(name=...)``)."""
    return {f"{key}_" if key in _RECORD_ATTRS else key: value for key, value in fields.items()}


def _http_request() -> Request | None:
    try:
        return get_http_request()
    except RuntimeError:  # stdio or in-memory transport: no HTTP request
        return None


def _clean(value: str | None) -> str | None:
    """Printable, bounded header value (headers are client-controlled)."""
    if not value:
        return None
    text = "".join(ch for ch in value if ch.isprintable()).strip()
    return text[:_MAX_VALUE] or None


def _trace_id(traceparent: str | None) -> str | None:
    match = _TRACEPARENT.match(traceparent.strip().lower()) if traceparent else None
    return match.group(1) if match else None


def _token_user() -> str | None:
    try:
        token = get_access_token()
    except Exception:  # no auth context for this request
        return None
    if token is None:
        return None
    claims = getattr(token, "claims", None) or {}
    for key in ("email", "preferred_username", "sub"):
        if claims.get(key):
            return _clean(str(claims[key]))
    return _clean(token.client_id)


def _client_info(context: MiddlewareContext[Any]) -> str | None:
    info: Any = None
    try:
        session = context.fastmcp_context.session if context.fastmcp_context else None
        info = getattr(getattr(session, "client_params", None), "client_info", None)
    except Exception:  # best effort: no session yet, or no request context
        info = None
    if info is None:  # an initialize request carries it in its own params
        info = getattr(getattr(context.message, "params", None), "client_info", None)
    name = getattr(info, "name", None)
    if not name:
        return None
    version = getattr(info, "version", None)
    return _clean(f"{name}/{version}" if version else str(name))


def _result_error(result: Any) -> str | None:
    if not getattr(result, "is_error", False):
        return None
    content = getattr(result, "structured_content", None)
    if isinstance(content, dict):
        error = content.get("error")
        if isinstance(error, dict) and error.get("code"):
            return str(error["code"])
    return "tool_error"


def _exception_name(exc: BaseException) -> str:
    cause = exc.__cause__ or exc
    status = getattr(getattr(cause, "response", None), "status_code", None)
    name = type(cause).__name__
    return f"{name}({status})" if isinstance(status, int) else name
