"""Thin metrics abstraction — emits structlog events for now. See ADR-0004 §3.

Deliberately under-built: no registry, no label validation, no Prometheus
client. Call sites are the durable part. When the upgrade trigger fires
(first real tenant, ~100 req/min, or a real incident), the bodies of
`counter()` and `histogram()` change to talk to `prometheus_client` — no
call site changes.
"""

from __future__ import annotations

from pdpl.observability.logging import get_logger

_log = get_logger("pdpl.metrics")


def counter(name: str, value: int = 1, **labels: str) -> None:
    _log.info(
        "metric",
        metric_kind="counter",
        metric_name=name,
        value=value,
        **labels,
    )


def histogram(name: str, value: float, **labels: str) -> None:
    _log.info(
        "metric",
        metric_kind="histogram",
        metric_name=name,
        value=value,
        **labels,
    )
