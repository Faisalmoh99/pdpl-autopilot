"""The AI layer — the UNTRUSTED producer namespace (ADR-0009).

This package is where AI lives. It *reads / suggests / explains*; it never
*decides / scores / classifies*. Per the core decision principle (CLAUDE.md),
nothing in here may reach the user without first passing the deterministic
gate in `pdpl.verification`.

Two architectural facts about this namespace are mechanically enforced by the
`.importlinter` contracts (see `tests/test_architecture.py`):

  - the deterministic core (`pdpl.services.decision/scoring/checks/alerts`)
    may NOT import this package — the verdict path stays provably AI-free; and
  - this package may NOT import the decision core — the AI reads deterministic
    outputs *as data* (a `GapContext` handed to it), and cannot recompute or
    feed back into a verdict.

This package owns the `Explainer` port and its input contract, `GapContext`.
`StubExplainer` (deterministic, no network) ships first so the verification
gate, the orchestration, and the eval (ADR-0010) all work before any real
LLM exists. `GeminiExplainer` slots in behind the identical port later (C3).
"""
