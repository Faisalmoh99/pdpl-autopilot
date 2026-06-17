"""WebhookNotifier — the first concrete Notifier (ADR-0008 §3).

POSTs the alert payload as JSON to a configured URL, HMAC-SHA256 signed, with
a single overall wall-clock send deadline. It raises the typed errors from
the port so the worker (Session B) can route the outcome:

  - timeout / connection error / HTTP 5xx / HTTP 429  -> TransientNotifierError
  - HTTP 4xx (other than 429)                         -> PermanentNotifierError
  - any status it cannot classify (1xx/3xx)           -> NotifierError (base),
    which propagates to the worker's default branch.

Reliability (retry, backoff, dead-lettering) lives in the worker AROUND this
port, not here. This class owns exactly one thing: a single signed send, with
a bounded deadline and an honest typed failure.

The signing secret is a SecretStr and is NEVER logged — at most a short
fingerprint of it is, so misconfiguration is diagnosable without leaking it.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from typing import Final

import httpx
from pydantic import SecretStr

from pdpl.notifications.port import (
    NotifierError,
    OutboxAlert,
    PermanentNotifierError,
    TransientNotifierError,
)
from pdpl.observability.logging import get_logger

_log = get_logger("pdpl.notifications.webhook")

_SIGNATURE_HEADER: Final = "X-PDPL-Signature"
_TIMESTAMP_HEADER: Final = "X-PDPL-Timestamp"
_IDEMPOTENCY_HEADER: Final = "Idempotency-Key"


def _fingerprint(secret: str) -> str:
    """A short, non-reversible fingerprint of the signing secret — safe to log
    so a key mismatch is diagnosable without ever revealing the secret."""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:8]


class WebhookNotifier:
    """Delivers an alert as a signed JSON webhook POST. Satisfies the
    `Notifier` port. Fails fast at construction if URL/secret are missing."""

    def __init__(
        self,
        *,
        url: str | None,
        secret: SecretStr | None,
        timeout_seconds: float = 5.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not url:
            raise ValueError(
                "WebhookNotifier requires a non-empty url "
                "(set ALERT_WEBHOOK_URL)"
            )
        if secret is None or not secret.get_secret_value():
            raise ValueError(
                "WebhookNotifier requires a non-empty signing secret "
                "(set ALERT_WEBHOOK_SECRET)"
            )
        self._url = url
        self._secret = secret  # SecretStr — never unwrapped into a log
        self._timeout = timeout_seconds
        # The overall deadline below (asyncio.timeout) is authoritative; the
        # client timeout is a secondary guard so a stuck socket does not hang
        # past the wall-clock ceiling.
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds)
        )

    def _sign(self, timestamp: str, body: bytes) -> str:
        mac = hmac.new(
            self._secret.get_secret_value().encode("utf-8"),
            f"{timestamp}.".encode("utf-8") + body,
            hashlib.sha256,
        )
        return mac.hexdigest()

    async def send(self, alert: OutboxAlert) -> None:
        # Serialize once, deterministically, and sign + send the EXACT bytes.
        body = json.dumps(
            alert.payload, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        timestamp = str(int(time.time()))
        headers = {
            "Content-Type": "application/json",
            _TIMESTAMP_HEADER: timestamp,
            _SIGNATURE_HEADER: f"sha256={self._sign(timestamp, body)}",
            _IDEMPOTENCY_HEADER: alert.idempotency_key,
        }

        try:
            # ONE overall wall-clock deadline for the whole send — connect,
            # write, read do NOT stack into an unbounded total (ADR-0008 §8;
            # this ceiling is the worker's lock-hold ceiling in Session B).
            async with asyncio.timeout(self._timeout):
                response = await self._client.post(
                    self._url, content=body, headers=headers
                )
        except TimeoutError as exc:
            raise TransientNotifierError(
                f"webhook send exceeded {self._timeout}s deadline"
            ) from exc
        except httpx.TransportError as exc:
            # Covers connection errors and httpx's own timeout exceptions.
            raise TransientNotifierError(
                f"webhook transport error: {type(exc).__name__}"
            ) from exc

        self._raise_for_status(alert, response.status_code)

        _log.info(
            "alert.webhook.delivered",
            idempotency_key=alert.idempotency_key,
            status_code=response.status_code,
            signing_key_fingerprint=_fingerprint(
                self._secret.get_secret_value()
            ),
        )

    @staticmethod
    def _raise_for_status(alert: OutboxAlert, status: int) -> None:
        if 200 <= status < 300:
            return
        if status == 429 or 500 <= status < 600:
            raise TransientNotifierError(
                f"webhook returned retry-worthy status {status}"
            )
        if 400 <= status < 500:
            raise PermanentNotifierError(
                f"webhook returned permanent status {status}"
            )
        # 1xx / 3xx — unexpected for a webhook POST. Cannot classify; raise
        # the base type so it propagates to the worker's default branch.
        raise NotifierError(f"webhook returned unclassifiable status {status}")


def webhook_notifier_from_settings(settings) -> WebhookNotifier:
    """Build a WebhookNotifier from Settings. Fails fast (ValueError) if the
    URL/secret are unset — the worker (Session B) calls this at startup so a
    misconfigured worker never starts."""
    return WebhookNotifier(
        url=settings.alert_webhook_url,
        secret=settings.alert_webhook_secret,
        timeout_seconds=settings.alert_webhook_timeout_seconds,
    )
