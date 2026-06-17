"""Background workers (ADR-0008). The outbox worker delivers enqueued alerts.

Workers live OUTSIDE the import-linter–guarded deterministic core: they perform
I/O (DB + the outbound webhook) and depend on the Notifier port. The core
(decision/checks/scoring/alerts) never imports a worker.
"""
