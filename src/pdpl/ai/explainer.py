"""The Explainer port + its input contract `GapContext` (ADR-0009 §1-2).

Mirrors the Notifier port (ADR-0008 §3 / `pdpl.notifications.port`): an
abstract contract with swappable implementations, all inside the untrusted
`pdpl.ai` namespace. The orchestration (`pdpl.explanations`) depends on this
port, never on a concrete model, so the verification gate and the eval work
against `StubExplainer` before any real LLM exists. `GeminiExplainer` slots
in behind the identical seam later (C3) with no change to callers.

`GapContext` is the producer's input contract and therefore lives here, with
the producer that owns it. It is **tenant-agnostic by construction** (ADR-0009
§2): it carries only the deterministic, non-personal facts of a gap — never
the tenant's raw questionnaire answers, customer records, or any PII. The
`rationale` is the deterministic engine's mechanical statement of *what made
the status what it is* (ADR-0006 §4), not personal data. This single choice
keeps PII out of the LLM call, keeps the content-hash cache leak-free across
tenants (C3), and keeps the explanation a pure function of public control
text + a deterministic verdict.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


class ExplainerError(Exception):
    """Base error raised by a real Explainer when it cannot produce output.

    Mirrors the Notifier taxonomy (ADR-0008 / `pdpl.notifications.port`):
    `TransientExplainerError` -> retry with backoff (bounded by max_attempts);
    `PermanentExplainerError` -> do not retry. A failure the Explainer cannot
    classify is raised as this base type and treated as transient (bounded).
    On exhausted retries or a permanent failure, the C4 orchestration falls
    back to the deterministic `rationale` — a failed call is never a failed
    request (ADR-0009 §5).
    """


class TransientExplainerError(ExplainerError):
    """A retry-worthy failure: timeout, connection error, HTTP 5xx or 429."""


class PermanentExplainerError(ExplainerError):
    """A non-retryable failure: HTTP 4xx (other than 429), or a malformed
    response the Explainer cannot parse."""


@dataclass(frozen=True)
class GapContext:
    """The non-personal facts of one gap, handed to an `Explainer` (ADR-0009 §2).

    Tenant-agnostic by construction — it NEVER carries raw answers, customer
    records, or any PII. Two tenants with the identical gap produce the
    identical `GapContext`, which is what makes the explanation cacheable
    (C3) and eval-able (ADR-0010).
    """

    control_code: str  # e.g. "PDPL-ART12-PRIVACY-NOTICE" — the developer token
    control_title_ar: str  # the obligation named in Arabic (the layperson hook)
    control_description_ar: str  # a fuller Arabic description of the control
    status: str  # the deterministic verdict (non_compliant / partial / ...)
    rationale: str  # deterministic "what made the status what it is" (ADR-0006)
    severity_weight: float  # the control's weight (ADR-0007)
    # The READABLE Arabic text of the unsatisfied/unanswered questions behind
    # this gap (C3a). The `rationale` only carries cryptic question CODES (e.g.
    # "gap(s): Q-ART12-NOTICE-RECIPIENTS"); without the text the model would
    # have to decode them and could explain the wrong gap. The C4 runtime fills
    # this from the engine's structured `unsatisfied_codes` via
    # `pdpl.catalog.prompts_ar_for`; it is empty for controls with no rule (the
    # model then binds to the control TITLE alone). Still tenant-agnostic — this
    # is static control metadata, never a tenant answer or PII.
    unsatisfied_questions_ar: tuple[str, ...] = ()
    lang: str = "ar"  # output language; MVP is Arabic-only


@runtime_checkable
class Explainer(Protocol):
    """Turns a deterministic `GapContext` into a short, human Arabic
    explanation — *"why this is a gap, and one step to fix it"* (ADR-0009 §1).

    An implementation is the UNTRUSTED producer: its output is never shown to
    a user directly. The orchestration (`pdpl.explanations`) runs every
    candidate through `pdpl.verification.verify_explanation` and falls back to
    the deterministic `rationale` on failure.
    """

    async def explain(self, ctx: GapContext) -> str: ...


class StubExplainer:
    """An Explainer (structurally — satisfies the port) with injectable output
    and no network, mirroring `StubNotifier`.

    Every `explain` returns `output` verbatim, regardless of the `GapContext`,
    and records the contexts it was handed. This lets a test drive the gate
    and the orchestration deterministically: a known-good Arabic explanation,
    or — for the keystone proof-of-safety test — a deliberately UNSAFE output
    that asserts compliance, which the gate MUST reject.
    """

    # The keystone bad output: a bald compliance assertion the gate must catch
    # (ADR-0009 §3 check 1 / ADR-0010 §5). "You are compliant with the system,
    # there are no gaps."
    _COMPLIANCE_ASSERTION = (
        "أنت ملتزم بالنظام بشكل كامل ولا توجد أي ثغرات في معالجة البيانات لديك."
    )

    def __init__(self, *, output: str) -> None:
        self.output = output
        self.calls: list[GapContext] = []

    async def explain(self, ctx: GapContext) -> str:
        self.calls.append(ctx)
        return self.output

    @classmethod
    def good(cls, text: str) -> StubExplainer:
        """A stub returning a known-good explanation supplied by the caller."""
        return cls(output=text)

    @classmethod
    def asserting_compliance(cls) -> StubExplainer:
        """The keystone bad stub: it asserts compliance («أنت ملتزم …»).

        The gate MUST reject this, and the orchestration MUST fall back to the
        deterministic `rationale` — proving the safety line is wired and real,
        not assumed (ADR-0010 §5).
        """
        return cls(output=cls._COMPLIANCE_ASSERTION)
