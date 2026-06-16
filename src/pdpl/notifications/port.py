"""The Notifier port (ADR-0008 §3) — abstract delivery contract.

The worker depends on this interface, never on a concrete vendor, so the
reliability machinery (backoff, retry, dead-lettering, idempotency) is
written once around the port and reused by every implementation. Session A
ships the contract only; the first concrete implementation (an HMAC-signed
webhook) and the worker land in Session B.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable
from uuid import UUID


@dataclass(frozen=True)
class OutboxAlert:
    """One alert as read from an `outbox` row — the unit a Notifier sends.

    `idempotency_key` is carried into the delivery (e.g. an `Idempotency-Key`
    header) so a receiver can dedupe an at-least-once re-delivery (ADR-0008
    §5).
    """

    id: UUID
    topic: str
    idempotency_key: str
    payload: dict[str, Any]
    attempts: int


class NotifierError(Exception):
    """Delivery failed. The worker owns the transient-vs-permanent decision
    via attempts / max_attempts and backoff — a Notifier just signals that a
    send did not succeed by raising (this, or any other exception)."""


@runtime_checkable
class Notifier(Protocol):
    """Delivers an alert to a destination. An implementation must raise on
    failure (not swallow it), and should include `alert.idempotency_key` in
    the delivery so the receiver can dedupe a re-delivery."""

    async def send(self, alert: OutboxAlert) -> None: ...
