"""Test doubles shared across notifier / worker tests.

`StubNotifier` is the reason the Notifier port was abstracted in Session A:
it sits behind the same port as the real WebhookNotifier and lets a test
drive success / transient-failure / permanent-failure deterministically,
without any network. Session B's worker tests reuse it to exercise the
retry / dead-letter / idempotency paths.
"""

from __future__ import annotations

from pdpl.notifications.port import (
    OutboxAlert,
    PermanentNotifierError,
    TransientNotifierError,
)


class StubNotifier:
    """A Notifier (structurally — satisfies the port) that records every
    alert handed to it and fails on demand.

    `mode` selects the behaviour of every `send`:
      - "success"   -> returns normally
      - "transient" -> raises TransientNotifierError
      - "permanent" -> raises PermanentNotifierError
    """

    def __init__(self, *, mode: str = "success") -> None:
        self.mode = mode
        self.calls: list[OutboxAlert] = []

    async def send(self, alert: OutboxAlert) -> None:
        self.calls.append(alert)
        if self.mode == "success":
            return
        if self.mode == "transient":
            raise TransientNotifierError("stub: forced transient failure")
        if self.mode == "permanent":
            raise PermanentNotifierError("stub: forced permanent failure")
        raise AssertionError(f"StubNotifier: unknown mode {self.mode!r}")
