"""GeminiExplainer (C3a, ADR-0009 §5) — fully mocked, no network/key/cost.

Mirrors test_webhook_notifier.py: the real class is driven through
httpx.MockTransport, proving the request shape, the typed transient/permanent
classification, the full-jitter retry, the single per-attempt deadline,
fail-fast construction, response parsing, and that the API key never reaches
the logs. The actual costed run against real Gemini is a separate MANUAL step.
"""

from __future__ import annotations

import json

import httpx
import pytest
from pydantic import SecretStr
from structlog.testing import capture_logs
from types import SimpleNamespace

from pdpl.ai.explainer import (
    Explainer,
    ExplainerError,
    GapContext,
    PermanentExplainerError,
    TransientExplainerError,
)
from pdpl.ai.gemini import GeminiExplainer, gemini_explainer_from_settings

_KEY = "test-gemini-api-key"


def _ctx(**over) -> GapContext:
    base = dict(
        control_code="PDPL-ART12-PRIVACY-NOTICE",
        control_title_ar="إفصاح إشعار الخصوصية لأصحاب البيانات",
        control_description_ar="وصف عربي للبند.",
        status="non_compliant",
        rationale="privacy notice: none of 4 question(s) satisfied",
        severity_weight=7.0,
        unsatisfied_questions_ar=("هل تنشر إشعار خصوصية؟",),
    )
    base.update(over)
    return GapContext(**base)


def _ok_payload(
    text: str = "هذا شرح عربي واضح للفجوة وخطوة علاجية واحدة.",
    *,
    model_version: str | None = "gemini-2.5-flash",
) -> dict:
    payload: dict = {
        "candidates": [
            {"content": {"parts": [{"text": text}]}, "finishReason": "STOP"}
        ],
        "usageMetadata": {"totalTokenCount": 42},
    }
    if model_version is not None:
        payload["modelVersion"] = model_version
    return payload


def _explainer(handler, *, key: str = _KEY, max_attempts: int = 3, timeout: float = 30.0):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return GeminiExplainer(
        api_key=SecretStr(key),
        model="gemini-2.5-flash",
        timeout_seconds=timeout,
        max_attempts=max_attempts,
        # zero backoff keeps retry tests instant and deterministic
        backoff_base_seconds=0.0,
        backoff_cap_seconds=0.0,
        client=client,
    )


# --------------------------------------------------------------------- port


def test_satisfies_the_explainer_port():
    assert isinstance(_explainer(lambda r: httpx.Response(200)), Explainer)


# ----------------------------------------------------------- the request


async def test_success_sends_correct_request_and_returns_text():
    captured: dict[str, httpx.Request] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["req"] = request
        return httpx.Response(200, json=_ok_payload("شرح."))

    out = await _explainer(handler).explain(_ctx())
    assert out.text == "شرح."

    req = captured["req"]
    assert req.url.path.endswith("/v1beta/models/gemini-2.5-flash:generateContent")
    assert req.headers["x-goog-api-key"] == _KEY
    body = json.loads(req.content)
    assert body["systemInstruction"]["parts"][0]["text"]  # non-empty rules
    assert body["contents"][0]["role"] == "user"
    assert body["generationConfig"]["temperature"] == 0.0
    # thinking is disabled so it cannot eat the output budget and truncate
    assert body["generationConfig"]["thinkingConfig"]["thinkingBudget"] == 0
    # the unsatisfied question text is grounded into the prompt
    assert "هل تنشر إشعار خصوصية؟" in body["contents"][0]["parts"][0]["text"]


# -------------------------------------------------------- classification


@pytest.mark.parametrize("status", [429, 500, 502, 503])
async def test_transient_statuses_retry_then_raise_transient(status: int):
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(status)

    with pytest.raises(TransientExplainerError):
        await _explainer(handler, max_attempts=3).explain(_ctx())
    assert calls["n"] == 3  # retried up to max_attempts


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
async def test_client_error_statuses_raise_permanent_without_retry(status: int):
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(status)

    with pytest.raises(PermanentExplainerError):
        await _explainer(handler, max_attempts=3).explain(_ctx())
    assert calls["n"] == 1  # permanent -> no retry


async def test_unclassifiable_status_is_base_error_and_bounded_retry():
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(302, headers={"Location": "https://elsewhere"})

    with pytest.raises(ExplainerError) as exc_info:
        await _explainer(handler, max_attempts=2).explain(_ctx())
    assert not isinstance(
        exc_info.value, (TransientExplainerError, PermanentExplainerError)
    )
    assert calls["n"] == 2  # base type retried (bounded), then raised


async def test_connection_error_is_transient():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(TransientExplainerError):
        await _explainer(handler, max_attempts=2).explain(_ctx())


async def test_httpx_timeout_is_transient():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(TransientExplainerError):
        await _explainer(handler, max_attempts=1).explain(_ctx())


async def test_overall_deadline_bounds_a_slow_attempt():
    import asyncio

    class _Slow(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            await asyncio.sleep(1.0)
            return httpx.Response(200, json=_ok_payload())

    explainer = GeminiExplainer(
        api_key=SecretStr(_KEY),
        model="gemini-2.5-flash",
        timeout_seconds=0.1,
        max_attempts=1,
        client=httpx.AsyncClient(transport=_Slow()),
    )
    with pytest.raises(TransientExplainerError):
        await explainer.explain(_ctx())


async def test_retries_then_succeeds():
    seq = iter([httpx.Response(503), httpx.Response(200, json=_ok_payload("نجح."))])

    def handler(_request: httpx.Request) -> httpx.Response:
        return next(seq)

    out = await _explainer(handler, max_attempts=3).explain(_ctx())
    assert out.text == "نجح."


# --------------------------------------------------------------- parsing


async def test_no_candidates_is_permanent():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"candidates": []})

    with pytest.raises(PermanentExplainerError):
        await _explainer(handler).explain(_ctx())


async def test_empty_text_is_permanent():
    # finishReason STOP but whitespace-only text -> the empty-text branch.
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"candidates": [{"content": {"parts": [{"text": "  "}]}, "finishReason": "STOP"}]},
        )

    with pytest.raises(PermanentExplainerError):
        await _explainer(handler).explain(_ctx())


@pytest.mark.parametrize("reason", ["MAX_TOKENS", "SAFETY", "RECITATION", None])
async def test_non_stop_finish_reason_is_permanent_not_returned(reason):
    """A truncated/blocked completion (finishReason != STOP) is a typed PERMANENT
    error, even WITH partial text — never silently returned, never retried. This
    is the structural fix: a truncation/block can never silently reach a user or
    pass the gate again (the C3a invalid-run bug)."""
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        cand = {"content": {"parts": [{"text": "نص عربي مقطوع في منتص"}]}}
        if reason is not None:
            cand["finishReason"] = reason
        return httpx.Response(200, json={"candidates": [cand]})

    with pytest.raises(PermanentExplainerError):
        await _explainer(handler, max_attempts=3).explain(_ctx())
    assert calls["n"] == 1  # permanent -> no retry


async def test_non_json_body_is_permanent():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json")

    with pytest.raises(PermanentExplainerError):
        await _explainer(handler).explain(_ctx())


# ------------------------------------------------------- fail-fast ctor


@pytest.mark.parametrize(
    "key,model",
    [
        (None, "gemini-2.5-flash"),
        (SecretStr(""), "gemini-2.5-flash"),
        (SecretStr(_KEY), ""),
    ],
)
def test_construction_fails_fast(key, model):
    with pytest.raises(ValueError):
        GeminiExplainer(api_key=key, model=model)


# ---------------------------------------------------- the key never logs


async def test_api_key_never_appears_in_logs():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_ok_payload())

    with capture_logs() as logs:
        await _explainer(handler).explain(_ctx())

    blob = json.dumps(logs)
    assert _KEY not in blob, "the API key leaked into a log event"
    fingerprints = [e.get("api_key_fingerprint") for e in logs]
    assert any(fingerprints)
    assert _KEY not in fingerprints


async def test_usage_is_counted():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_ok_payload())

    with capture_logs() as logs:
        await _explainer(handler).explain(_ctx())

    metrics = [e for e in logs if e.get("event") == "metric"]
    names = {e.get("metric_name") for e in metrics}
    assert "ai.gemini.calls" in names
    assert "ai.gemini.tokens" in names


# --------------------------------------------------------- from_settings


def test_from_settings_fails_fast_when_unset():
    settings = SimpleNamespace(
        gemini_api_key=None,
        gemini_model="gemini-2.5-flash",
        gemini_timeout_seconds=30.0,
        gemini_max_attempts=3,
        gemini_backoff_base_seconds=0.5,
        gemini_backoff_cap_seconds=8.0,
        gemini_temperature=0.0,
        gemini_max_output_tokens=512,
        gemini_thinking_budget=0,
    )
    with pytest.raises(ValueError):
        gemini_explainer_from_settings(settings)


def test_from_settings_builds_when_configured():
    settings = SimpleNamespace(
        gemini_api_key=SecretStr(_KEY),
        gemini_model="gemini-2.5-flash",
        gemini_timeout_seconds=30.0,
        gemini_max_attempts=3,
        gemini_backoff_base_seconds=0.5,
        gemini_backoff_cap_seconds=8.0,
        gemini_temperature=0.0,
        gemini_max_output_tokens=512,
        gemini_thinking_budget=0,
    )
    assert isinstance(gemini_explainer_from_settings(settings), GeminiExplainer)


# ------------------------------------------------ modelVersion provenance (C4a)


async def test_model_version_is_captured_on_the_output():
    """The concrete `modelVersion` the API answered with is surfaced on
    ExplainerOutput for provenance (ADR-0011 §6)."""
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_ok_payload(model_version="gemini-2.5-flash-002"))

    out = await _explainer(handler).explain(_ctx())
    assert out.model_version == "gemini-2.5-flash-002"


async def test_absent_model_version_is_none_not_fabricated():
    """An older response without `modelVersion` -> provenance is None, never
    invented."""
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_ok_payload(model_version=None))

    out = await _explainer(handler).explain(_ctx())
    assert out.model_version is None


async def test_model_version_mismatch_is_warned_but_not_blocked():
    """DETECT, not prevent (ADR-0011 §6): a returned modelVersion differing from
    the requested id still returns the output, and logs a mismatch warning so a
    human can notice and bump prompt_version."""
    def handler(_request: httpx.Request) -> httpx.Response:
        # requested default is gemini-2.5-flash; the provider answered with a
        # different concrete snapshot (a silently re-pointed alias).
        return httpx.Response(200, json=_ok_payload("شرح.", model_version="gemini-2.5-flash-NEXT"))

    with capture_logs() as logs:
        out = await _explainer(handler).explain(_ctx())

    assert out.text == "شرح."  # not blocked
    assert out.model_version == "gemini-2.5-flash-NEXT"
    mismatch = [e for e in logs if e.get("event") == "ai.gemini.model_version_mismatch"]
    assert len(mismatch) == 1
    assert mismatch[0]["requested_model"] == "gemini-2.5-flash"
    assert mismatch[0]["returned_model_version"] == "gemini-2.5-flash-NEXT"


async def test_matching_model_version_does_not_warn():
    """When the returned modelVersion matches the requested id, no mismatch
    warning is emitted (the alias has not drifted)."""
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_ok_payload("شرح.", model_version="gemini-2.5-flash"))

    with capture_logs() as logs:
        await _explainer(handler).explain(_ctx())

    assert not [e for e in logs if e.get("event") == "ai.gemini.model_version_mismatch"]
