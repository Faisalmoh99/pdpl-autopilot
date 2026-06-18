"""The compliance-assertion denylist — the curated heart of the safety check
(ADR-0009 §3 check 1).

This is the single most important check: it is the literal product safety
line — *AI must never tell the user "you are compliant."* Be honest about its
mechanism: it is a **curated denylist**, best-effort and fragile to paraphrase.
A novel wording the list has not seen can evade it. Its coverage is therefore
**bounded and growing, not complete** — the gate-bug loop (ADR-0010 §1) turns
every miss the eval finds into a permanent addition here, in this one file.

What belongs here: ASSERTION PHRASES that claim the user *is* compliant /
has no gaps. What deliberately does NOT belong here: bare compliance words
(«ملتزم», «متوافق», «ممتثل», "compliant with", "in compliance"). Those occur
in legitimate remediation guidance — *"to become compliant with the article,
do X"* — so banning them would false-reject good explanations and depress
`gate_pass_rate` (a gate-too-strict artdefact, not a model problem),
corrupting the headline metric and worsening UX. We ban the assertion, not
the vocabulary.

Entries are matched as substrings AFTER `normalize()` is applied to both the
candidate text and the phrase, so Arabic orthographic variants and casing do
not let a known assertion slip through.
"""

from __future__ import annotations

# Curated, human-readable. The gate-bug loop (ADR-0010 §1) appends to this
# list. Matched after normalization — see module docstring.
COMPLIANCE_ASSERTIONS: tuple[str, ...] = (
    # --- Arabic: direct "you are compliant" assertions ---
    "أنت ملتزم",
    "أنت متوافق",
    "مطابق للنظام",
    "امتثال كامل",
    # --- Arabic: "you have no gaps / nothing to fix" assertions ---
    "لا توجد مخالفات",
    "لا توجد ثغرات",
    "نظامك سليم",
    "ما عليك ملاحظات",
    "نظامك متوافق",
    "نظامك مطابق",
    # --- English ---
    "you are compliant",
    "fully compliant",
    "your system is compliant",
    "no gaps",
    "no violations",
)
