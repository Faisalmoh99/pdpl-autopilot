"""GeminiExplainer — the real LLM Explainer (ADR-0009 §5, C3a).

Calls the Gemini `generateContent` REST endpoint with httpx, reusing the
Phase-3 reliability patterns proven by `WebhookNotifier`:

  - a SINGLE overall wall-clock deadline per attempt via `asyncio.timeout`
    (connect/write/read do not stack into an unbounded total);
  - TYPED failure classification — timeout / connection / HTTP 5xx / 429 ->
    transient; HTTP 4xx -> permanent; a malformed/blocked response ->
    permanent; anything unclassified -> transient (bounded);
  - RETRY with full-jitter exponential backoff for transient failures, bounded
    by `max_attempts`;
  - the API key is a `SecretStr`, sent in the `x-goog-api-key` header and NEVER
    logged (at most a short fingerprint).

We deliberately use the REST API over httpx rather than the `google` SDK: the
SDK's `google` namespace is forbidden to the decision core and the verifier
(`.importlinter`), and httpx lets us reuse the exact, tested reliability shape.

On exhausted retries or a permanent failure this raises an `ExplainerError`.
The C4 orchestration catches it and falls back to the deterministic
`rationale` — a failed LLM call is never a failed request (ADR-0009 §5). The
eval (C3a) calls this directly and surfaces the failure to the operator.
"""

from __future__ import annotations

import asyncio
import hashlib
import random
from typing import Any, Final

import httpx
from pydantic import SecretStr

from pdpl.ai.explainer import (
    ExplainerError,
    GapContext,
    PermanentExplainerError,
    TransientExplainerError,
)
from pdpl.ai.prompt import PROMPT_VERSION, SYSTEM_INSTRUCTION, build_user_prompt
from pdpl.observability.logging import get_logger
from pdpl.observability.metrics import counter, histogram

_log = get_logger("pdpl.ai.gemini")

_DEFAULT_BASE_URL: Final = "https://generativelanguage.googleapis.com"
_API_KEY_HEADER: Final = "x-goog-api-key"


def _fingerprint(secret: str) -> str:
    """A short, non-reversible fingerprint of the API key — safe to log so a
    key mismatch is diagnosable without ever revealing the secret."""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:8]


class GeminiExplainer:
    """A real `Explainer` backed by Gemini `generateContent`. Satisfies the
    Explainer port. Fails fast at construction if the key or model are missing."""

    def __init__(
        self,
        *,
        api_key: SecretStr | None,
        model: str,
        timeout_seconds: float = 30.0,
        max_attempts: int = 3,
        backoff_base_seconds: float = 0.5,
        backoff_cap_seconds: float = 8.0,
        temperature: float = 0.0,
        max_output_tokens: int = 512,
        base_url: str = _DEFAULT_BASE_URL,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if api_key is None or not api_key.get_secret_value():
            raise ValueError(
                "GeminiExplainer requires a non-empty API key (set GEMINI_API_KEY)"
            )
        if not model:
            raise ValueError(
                "GeminiExplainer requires a model name (set GEMINI_MODEL)"
            )
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        self._api_key = api_key  # SecretStr — never unwrapped into a log
        self._model = model
        self._timeout = timeout_seconds
        self._max_attempts = max_attempts
        self._backoff_base = backoff_base_seconds
        self._backoff_cap = backoff_cap_seconds
        self._temperature = temperature
        self._max_output_tokens = max_output_tokens
        self._url = f"{base_url.rstrip('/')}/v1beta/models/{model}:generateContent"
        # The per-attempt deadline below (asyncio.timeout) is authoritative; the
        # client timeout is a secondary guard so a stuck socket cannot hang past
        # the wall-clock ceiling.
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds)
        )

    def _full_jitter(self, attempt: int) -> float:
        """random(0, min(base * 2^(attempt-1), cap)) — the same full-jitter
        backoff the outbox worker uses (ADR-0008)."""
        ceiling = min(self._backoff_base * (2 ** (attempt - 1)), self._backoff_cap)
        return random.uniform(0, ceiling)

    async def explain(self, ctx: GapContext) -> str:
        system = SYSTEM_INSTRUCTION
        user = build_user_prompt(ctx)

        last: ExplainerError | None = None
        for attempt in range(1, self._max_attempts + 1):
            try:
                return await self._call_once(ctx, system, user, attempt)
            except PermanentExplainerError:
                raise
            except ExplainerError as exc:  # transient or unclassifiable base
                last = exc
            except Exception as exc:  # unclassified -> transient (bounded)
                last = TransientExplainerError(
                    f"unexpected error: {type(exc).__name__}"
                )
                last.__cause__ = exc
            if attempt >= self._max_attempts:
                break
            await asyncio.sleep(self._full_jitter(attempt))

        assert last is not None
        raise last

    async def _call_once(
        self, ctx: GapContext, system: str, user: str, attempt: int
    ) -> str:
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {
                "temperature": self._temperature,
                "maxOutputTokens": self._max_output_tokens,
            },
        }
        headers = {
            "Content-Type": "application/json",
            _API_KEY_HEADER: self._api_key.get_secret_value(),
        }

        try:
            # ONE overall wall-clock deadline for this attempt — connect, write,
            # read do not stack into an unbounded total (mirrors WebhookNotifier).
            async with asyncio.timeout(self._timeout):
                response = await self._client.post(
                    self._url, json=body, headers=headers
                )
        except TimeoutError as exc:
            raise TransientExplainerError(
                f"gemini call exceeded {self._timeout}s deadline"
            ) from exc
        except httpx.TransportError as exc:
            raise TransientExplainerError(
                f"gemini transport error: {type(exc).__name__}"
            ) from exc

        self._raise_for_status(response.status_code)
        text, total_tokens = self._parse(response)

        counter("ai.gemini.calls", 1, model=self._model, prompt_version=PROMPT_VERSION)
        if total_tokens is not None:
            histogram("ai.gemini.tokens", float(total_tokens), model=self._model)
        _log.info(
            "ai.gemini.explained",
            model=self._model,
            prompt_version=PROMPT_VERSION,
            control_code=ctx.control_code,
            status=ctx.status,
            attempt=attempt,
            total_tokens=total_tokens,
            api_key_fingerprint=_fingerprint(self._api_key.get_secret_value()),
        )
        return text

    @staticmethod
    def _raise_for_status(status: int) -> None:
        if 200 <= status < 300:
            return
        if status == 429 or 500 <= status < 600:
            raise TransientExplainerError(
                f"gemini returned retry-worthy status {status}"
            )
        if 400 <= status < 500:
            raise PermanentExplainerError(
                f"gemini returned permanent status {status}"
            )
        # 1xx / 3xx — unexpected. Cannot classify; raise the base type, which
        # the retry loop treats as transient (bounded).
        raise ExplainerError(f"gemini returned unclassifiable status {status}")

    @staticmethod
    def _parse(response: httpx.Response) -> tuple[str, int | None]:
        """Extract the candidate text + approximate token usage. A malformed or
        blocked response (no candidate text) is PERMANENT — retrying will not
        help, and an empty string must never reach the gate as a candidate."""
        try:
            data: dict[str, Any] = response.json()
        except ValueError as exc:
            raise PermanentExplainerError("gemini response was not JSON") from exc

        candidates = data.get("candidates") or []
        parts = (
            candidates[0].get("content", {}).get("parts", [])
            if candidates
            else []
        )
        text = "".join(p.get("text", "") for p in parts).strip()
        if not text:
            reason = candidates[0].get("finishReason") if candidates else "no candidates"
            raise PermanentExplainerError(
                f"gemini returned no usable text (finishReason={reason})"
            )

        usage = data.get("usageMetadata") or {}
        total_tokens = usage.get("totalTokenCount")
        return text, total_tokens


def gemini_explainer_from_settings(settings) -> GeminiExplainer:
    """Build a GeminiExplainer from Settings. Fails fast (ValueError) if the
    key/model are unset — the manual eval run calls this so a misconfigured run
    never starts."""
    return GeminiExplainer(
        api_key=settings.gemini_api_key,
        model=settings.gemini_model,
        timeout_seconds=settings.gemini_timeout_seconds,
        max_attempts=settings.gemini_max_attempts,
        backoff_base_seconds=settings.gemini_backoff_base_seconds,
        backoff_cap_seconds=settings.gemini_backoff_cap_seconds,
        temperature=settings.gemini_temperature,
        max_output_tokens=settings.gemini_max_output_tokens,
    )
