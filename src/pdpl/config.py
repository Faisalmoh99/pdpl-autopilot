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


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
