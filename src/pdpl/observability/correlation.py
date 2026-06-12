"""Correlation-ID middleware + contextvar plumbing. See ADR-0004 §2.

A single UUID v7 threads through:
  incoming X-Request-ID header (or generated)
    -> structlog contextvars (every log line in this request)
    -> the audit_log writer (audit_log.correlation_id column)
    -> outgoing X-Request-ID response header.
"""

from __future__ import annotations

import time
from contextvars import ContextVar
from typing import Awaitable, Callable
from uuid import UUID

import structlog
import uuid6
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from pdpl.observability.logging import get_logger

correlation_id_var: ContextVar[UUID | None] = ContextVar(
    "correlation_id", default=None
)

_HEADER = "X-Request-ID"
_log = get_logger("pdpl.http")


def current_correlation_id() -> UUID | None:
    """Read the correlation ID set by the middleware for this request.

    Used by the audit_log writer to stamp every event with the request that
    produced it. Returns None outside an HTTP request context (e.g. tests
    calling the writer directly with their own ID).
    """
    return correlation_id_var.get()


def _parse_incoming(raw: str | None) -> UUID | None:
    if not raw:
        return None
    try:
        return UUID(raw)
    except (ValueError, AttributeError):
        return None


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        incoming = _parse_incoming(request.headers.get(_HEADER))
        correlation_id = incoming or uuid6.uuid7()
        token = correlation_id_var.set(correlation_id)
        structlog.contextvars.bind_contextvars(
            correlation_id=str(correlation_id),
            http_method=request.method,
            http_path=request.url.path,
        )

        started = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            response.headers[_HEADER] = str(correlation_id)
            return response
        finally:
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            _log.info(
                "http.request",
                status=status_code,
                duration_ms=round(elapsed_ms, 2),
            )
            structlog.contextvars.clear_contextvars()
            correlation_id_var.reset(token)
