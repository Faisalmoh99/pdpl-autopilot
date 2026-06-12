"""Structured logging — JSON output, contextvars-aware. See ADR-0004 §1."""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog

_SECRET_KEY_FRAGMENTS = ("password", "secret", "token", "api_key")


def _drop_secret_keys(_logger: Any, _method: str, event_dict: dict) -> dict:
    """Defense-in-depth: refuse to emit any key whose name looks secret-ish.

    Secrets live in pydantic SecretStr by primary design (ADR-0004 §4); this
    processor catches the case where one slipped through into a log call.
    """
    for key in list(event_dict.keys()):
        lowered = key.lower()
        if any(fragment in lowered for fragment in _SECRET_KEY_FRAGMENTS):
            event_dict[key] = "***redacted***"
    return event_dict


def configure_logging(level: str = "INFO") -> None:
    """Wire structlog + stdlib logging to emit a single JSON shape.

    Idempotent: safe to call multiple times (test fixtures rely on this).
    """
    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)

    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        timestamper,
        _drop_secret_keys,
    ]

    structlog.configure(
        processors=shared_processors
        + [
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping().get(level.upper(), logging.INFO)
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )

    # Route stdlib logging (SQLAlchemy, uvicorn, asyncpg, FastAPI) through the
    # same JSON formatter so output is a single uniform stream.
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(),
        ],
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())

    # Tame uvicorn's noisy access log — we generate our own request log line
    # in the correlation-ID middleware.
    for noisy in ("uvicorn.access",):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)  # type: ignore[return-value]
