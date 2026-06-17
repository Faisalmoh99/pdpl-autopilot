"""Session B1 — the WebhookNotifier (ADR-0008 §3).

Two layers, no real network:
  - StubNotifier behind the port — proves the test double the worker (B2)
    will lean on conforms to the contract and fails on demand.
  - The real WebhookNotifier driven through httpx.MockTransport — proves the
    signed request shape, the typed transient/permanent classification, the
    single overall send deadline, fail-fast construction, and that the
    signing secret never reaches the logs.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from types import SimpleNamespace

import httpx
import pytest
import uuid6
from pydantic import SecretStr
from structlog.testing import capture_logs

from pdpl.notifications.port import (
    Notifier,
    NotifierError,
    OutboxAlert,
    PermanentNotifierError,
    TransientNotifierError,
)
from pdpl.notifications.webhook import (
    WebhookNotifier,
    webhook_notifier_from_settings,
)
from tests.stubs import StubNotifier

_SECRET = "super-secret-signing-key"


def _alert() -> OutboxAlert:
    return OutboxAlert(
        id=uuid6.uuid7(),
        topic="finding.worsened",
        idempotency_key=f"alert:finding-transition:{uuid6.uuid7()}",
        payload={
            "control_code": "PDPL-ART4-DSR-ACCESS",
            "from_status": "compliant",
            "to_status": "non_compliant",
        },
        attempts=0,
    )


def _notifier(handler, *, secret: str = _SECRET, timeout: float = 5.0):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return WebhookNotifier(
        url="https://hook.test/alerts",
        secret=SecretStr(secret),
        timeout_seconds=timeout,
        client=client,
    )


# ---------------------------------------------------------------------
# StubNotifier — the port-level test double.
# ---------------------------------------------------------------------


def test_stub_notifier_satisfies_the_port():
    assert isinstance(StubNotifier(), Notifier)


async def test_stub_notifier_records_and_fails_on_demand():
    ok = StubNotifier(mode="success")
    a = _alert()
    await ok.send(a)
    assert ok.calls == [a]

    with pytest.raises(TransientNotifierError):
        await StubNotifier(mode="transient").send(_alert())
    with pytest.raises(PermanentNotifierError):
        await StubNotifier(mode="permanent").send(_alert())


# ---------------------------------------------------------------------
# WebhookNotifier — the signed request.
# ---------------------------------------------------------------------


async def test_success_sends_correctly_signed_request():
    captured: dict[str, httpx.Request] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["req"] = request
        return httpx.Response(200)

    alert = _alert()
    await _notifier(handler).send(alert)

    req = captured["req"]
    # Headers.
    assert req.headers["Content-Type"] == "application/json"
    assert req.headers["Idempotency-Key"] == alert.idempotency_key
    ts = req.headers["X-PDPL-Timestamp"]
    assert ts.isdigit()
    sig = req.headers["X-PDPL-Signature"]
    assert sig.startswith("sha256=")

    # The body is the deterministic JSON of the payload, and the signature is
    # a valid HMAC-SHA256 over "{timestamp}.{body}" with the secret.
    expected_body = json.dumps(
        alert.payload, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    assert req.content == expected_body
    expected_sig = hmac.new(
        _SECRET.encode("utf-8"),
        f"{ts}.".encode("utf-8") + req.content,
        hashlib.sha256,
    ).hexdigest()
    assert sig == f"sha256={expected_sig}"


# ---------------------------------------------------------------------
# WebhookNotifier — status classification.
# ---------------------------------------------------------------------


@pytest.mark.parametrize("status", [429, 500, 502, 503])
async def test_transient_statuses_raise_transient(status: int):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status)

    with pytest.raises(TransientNotifierError):
        await _notifier(handler).send(_alert())


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
async def test_client_error_statuses_raise_permanent(status: int):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status)

    with pytest.raises(PermanentNotifierError):
        await _notifier(handler).send(_alert())


async def test_unclassifiable_status_raises_base_notifier_error():
    # 3xx is unexpected for a webhook POST (redirects are not followed).
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "https://elsewhere"})

    with pytest.raises(NotifierError) as exc_info:
        await _notifier(handler).send(_alert())
    # Base type only — neither transient nor permanent, so the worker's
    # default branch (treat-as-transient) owns it (ADR-0008, B2).
    assert not isinstance(
        exc_info.value, (TransientNotifierError, PermanentNotifierError)
    )


# ---------------------------------------------------------------------
# WebhookNotifier — network failures and the overall deadline.
# ---------------------------------------------------------------------


async def test_connection_error_is_transient():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(TransientNotifierError):
        await _notifier(handler).send(_alert())


async def test_httpx_timeout_is_transient():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow upstream", request=request)

    with pytest.raises(TransientNotifierError):
        await _notifier(handler).send(_alert())


async def test_overall_deadline_bounds_a_slow_send():
    class _SlowTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(
            self, request: httpx.Request
        ) -> httpx.Response:
            await asyncio.sleep(1.0)
            return httpx.Response(200)

    notifier = WebhookNotifier(
        url="https://hook.test/alerts",
        secret=SecretStr(_SECRET),
        timeout_seconds=0.1,
        client=httpx.AsyncClient(transport=_SlowTransport()),
    )
    # The single wall-clock deadline (0.1s) fires before the 1s send and is
    # reported as a transient failure — connect/read do not stack past it.
    with pytest.raises(TransientNotifierError):
        await notifier.send(_alert())


# ---------------------------------------------------------------------
# WebhookNotifier — fail-fast construction.
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "url,secret",
    [
        (None, SecretStr(_SECRET)),
        ("", SecretStr(_SECRET)),
        ("https://hook.test/x", None),
        ("https://hook.test/x", SecretStr("")),
    ],
)
def test_construction_fails_fast_on_missing_config(url, secret):
    with pytest.raises(ValueError):
        WebhookNotifier(url=url, secret=secret)


def test_from_settings_fails_fast_when_unset():
    settings = SimpleNamespace(
        alert_webhook_url=None,
        alert_webhook_secret=None,
        alert_webhook_timeout_seconds=5.0,
    )
    with pytest.raises(ValueError):
        webhook_notifier_from_settings(settings)


def test_from_settings_builds_when_configured():
    settings = SimpleNamespace(
        alert_webhook_url="https://hook.test/alerts",
        alert_webhook_secret=SecretStr(_SECRET),
        alert_webhook_timeout_seconds=3.0,
    )
    notifier = webhook_notifier_from_settings(settings)
    assert isinstance(notifier, WebhookNotifier)


# ---------------------------------------------------------------------
# WebhookNotifier — the secret never reaches the logs.
# ---------------------------------------------------------------------


async def test_signing_secret_never_appears_in_logs():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    with capture_logs() as logs:
        await _notifier(handler).send(_alert())

    blob = json.dumps(logs)
    assert _SECRET not in blob, "the signing secret leaked into a log event"
    # A fingerprint IS logged (diagnosable) and is not the secret itself.
    fingerprints = [e.get("signing_key_fingerprint") for e in logs]
    assert any(fingerprints), "expected a signing_key_fingerprint log field"
    assert _SECRET not in fingerprints
