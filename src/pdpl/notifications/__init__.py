"""Alert delivery — the Notifier port and its implementations (ADR-0008 §3).

Reliability (retry, full-jitter backoff, dead-lettering, idempotency) wraps
the PORT, not a vendor. This package defines the abstract `Notifier` port;
concrete implementations (an HMAC-signed webhook first; email / WhatsApp as
deferred swap-ins) live behind it without changing the worker.
"""
