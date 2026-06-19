"""The eval harness — measurement tooling for the gap-explanation feature (ADR-0010).

This package is TOOLING, not production: it measures, numerically, how well an
`Explainer` does its job. Nothing in production imports it — the `.importlinter`
`production-no-eval` contract (ADR-0010 §, mirroring ADR-0009 §7) enforces that
mechanically, the same way the trust-region fences are enforced.

It reuses the runtime's exact safety gate: `run()` calls the SAME
`pdpl.verification.verify_explanation` the orchestration calls (never a copy),
so its Layer-A numbers describe the gate that actually protects users. The
harness is a pure, testable `run(explainer, cases) -> EvalMetrics`; a thin CLI
(`python -m pdpl.eval`) wraps it to print the metric table — same testability
discipline as the outbox worker's `run_once`.

What this package does NOT do (ADR-0010): it does not re-implement any check,
it does not call a real LLM (C3), and it does not fake a `quality_score` off a
stub — Layer-B human rating is exercised against the real model in C3.
"""
