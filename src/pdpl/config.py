"""Settings — env-driven, fail-fast, secrets as SecretStr.

See ADR-0004 §4. The Settings instance is built once at import time; if any
required var is missing or malformed, Pydantic raises ValidationError and the
process exits before serving a request.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Runtime DB URL — as pdpl_app, NOT as postgres. See ADR-0004 §5.
    app_database_url: SecretStr = Field(..., alias="APP_DATABASE_URL")

    # Connection-pool sizing (ADR-0014 §5). BOTH default to None, which means
    # "do not pass the kwarg to create_async_engine" — so SQLAlchemy's own
    # defaults apply (pool_size=5 + max_overflow=10 = 15). Production NEVER sets
    # these: unset is byte-for-byte the prior behaviour. They exist ONLY for the
    # Phase-5 load-test pool-size sweep (the causal-isolation diagnostic), set
    # via env for that run alone — never the headline config.
    db_pool_size: int | None = Field(None, alias="DB_POOL_SIZE")
    db_max_overflow: int | None = Field(None, alias="DB_MAX_OVERFLOW")

    # Logging.
    log_level: str = Field("INFO", alias="LOG_LEVEL")

    # Server.
    service_name: str = Field("pdpl-autopilot", alias="SERVICE_NAME")
    env_name: str = Field("dev", alias="ENV_NAME")

    # Alert webhook (ADR-0008). Optional at import — the API and most tests
    # never touch alerting — so a missing value does NOT stop the process
    # from serving requests. The WebhookNotifier fails fast at CONSTRUCTION
    # if URL/secret are absent, so the worker (Session B) still cannot run
    # misconfigured. The signing secret is a SecretStr and is never logged.
    alert_webhook_url: str | None = Field(None, alias="ALERT_WEBHOOK_URL")
    alert_webhook_secret: SecretStr | None = Field(
        None, alias="ALERT_WEBHOOK_SECRET"
    )
    # A single overall wall-clock ceiling for one send (NOT per connect/read
    # phase). In Session B the worker holds the outbox row lock across this
    # send, so this value is the lock-hold ceiling.
    alert_webhook_timeout_seconds: float = Field(
        5.0, alias="ALERT_WEBHOOK_TIMEOUT_SECONDS"
    )

    # Outbox worker (ADR-0008, Session B2). The worker builds its OWN engine
    # from this DEDICATED url — a session-level / direct connection AS
    # pdpl_app, NOT the transaction pooler (FOR UPDATE + a held transaction do
    # not fit the transaction pooler). Optional at import (the API never runs
    # the worker); the worker entry point fails fast if it is unset.
    worker_database_url: SecretStr | None = Field(
        None, alias="WORKER_DATABASE_URL"
    )
    # Retry / backoff policy. Full-jitter exponential backoff:
    # next_attempt_at = now() + random(0, min(base * 2^(attempts-1), cap)).
    outbox_max_attempts: int = Field(5, alias="OUTBOX_MAX_ATTEMPTS")
    outbox_backoff_base_seconds: float = Field(
        60.0, alias="OUTBOX_BACKOFF_BASE_SECONDS"
    )
    outbox_backoff_cap_seconds: float = Field(
        3600.0, alias="OUTBOX_BACKOFF_CAP_SECONDS"
    )
    outbox_poll_interval_seconds: float = Field(
        5.0, alias="OUTBOX_POLL_INTERVAL_SECONDS"
    )

    # Gemini explainer (ADR-0009 §5, C3a). Optional at import — the API and the
    # tests never call the real model — so a missing key does NOT stop the
    # process. GeminiExplainer fails fast at CONSTRUCTION if the key/model are
    # absent, so the manual eval run cannot start misconfigured. The key is a
    # SecretStr and is never logged (at most a short fingerprint). Defaults
    # match ADR-0009 §5: flash-tier model, 30s per-attempt wall-clock deadline,
    # 3 attempts, full-jitter backoff 0.5s..8s, temperature 0 for the eval's
    # reproducibility.
    gemini_api_key: SecretStr | None = Field(None, alias="GEMINI_API_KEY")
    gemini_model: str = Field("gemini-2.5-flash", alias="GEMINI_MODEL")
    gemini_timeout_seconds: float = Field(30.0, alias="GEMINI_TIMEOUT_SECONDS")
    gemini_max_attempts: int = Field(3, alias="GEMINI_MAX_ATTEMPTS")
    gemini_backoff_base_seconds: float = Field(
        0.5, alias="GEMINI_BACKOFF_BASE_SECONDS"
    )
    gemini_backoff_cap_seconds: float = Field(
        8.0, alias="GEMINI_BACKOFF_CAP_SECONDS"
    )
    gemini_temperature: float = Field(0.0, alias="GEMINI_TEMPERATURE")
    # 1024 is a ceiling with headroom — the prompt's "2-4 sentences" and the
    # 600-char gate bound govern the ACTUAL length. It also leaves room for the
    # answer even if thinking is not fully disabled (the fallback-by-construction
    # for thinking_budget=0, see GeminiExplainer).
    gemini_max_output_tokens: int = Field(1024, alias="GEMINI_MAX_OUTPUT_TOKENS")
    # gemini-2.5-flash runs THINKING by default, and thinking tokens count
    # toward maxOutputTokens — at 512 they ate the budget and truncated the
    # answer mid-word. 0 disables thinking on flash: for this short, already-
    # grounded, deterministic task thinking adds nothing but cost, latency, and
    # non-determinism. (-1 = dynamic/auto if ever needed.)
    gemini_thinking_budget: int = Field(0, alias="GEMINI_THINKING_BUDGET")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
