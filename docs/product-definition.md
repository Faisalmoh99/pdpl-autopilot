# PDPL Autopilot — Product Definition

## Overview
**A PDPL readiness assistant for small and medium Saudi businesses.**
It connects a company's data, **continuously** monitors compliance with the Saudi Personal Data Protection Law (PDPL), and flags issues before a fine: where the gap is, why, and what to do.

> Reference model: Vanta / Drata — but tailored to Saudi regulation, starting with PDPL.
> Primary purpose: a **learning product** (building CTO/consulting skills). Revenue is secondary.

---

## Problem
- PDPL is **enforced and penalized** (fines up to SAR 5M · 72-hour breach notification).
- Owners don't know whether they're compliant, where their gaps are, or when something changed — they **find out after the fine**.
- The current alternative is a manual consultant: point-in-time, expensive, no continuous monitoring.

## Target user (hypothesis)
A small Saudi e-commerce business (10–50 employees) processing personal data of Saudi customers, with no DPO and no in-house compliance function.
*Alternatives:* a clinic (sensitive data) · an early-stage SaaS startup. — **An assumption to validate later, not a confirmed customer.**

## Job-to-be-done
> "Show me where I stand on PDPL, what to fix, and warn me continuously — without hiring a compliance expert."

---

## MVP scope

**In scope:**
- Initial readiness questionnaire → gap report + readiness score.
- Continuous monitoring (scheduled checks) + alerts.
- Read an uploaded privacy policy and extract what's covered.
- Explain each gap in Arabic + a remediation step.

**Out of scope (for now):**
- ZATCA / e-invoicing (crowded market — deferred).
- Other frameworks (SOC2, GDPR…).
- Legal guarantee (the product is a "readiness assistant," not a lawyer).

---

## AI vs deterministic decision (core principle)
> AI **reads / suggests / explains**. Deterministic logic **decides / scores / classifies**. AI must **never** say "you are compliant."

| Deterministic (no AI) | AI |
|---|---|
| Obligation checks (yes/no) · scoring · deadline tracking · immutable audit log | Document reading · explaining gaps in Arabic · drafting templates · summarizing regulatory changes · classifying free-text |

**Safety line:** every AI output passes through a deterministic verification layer before it reaches the user as a decision.

---

## Success metrics (before any code)
**Product:**
- Time to first report < 10 minutes.
- Detect ≥ 90% of real gaps (on a test set of 10 synthetic companies).
- False-positive rate < 10% ← **the most important** (false alarms break trust).

**Learning:**
- Document-extraction precision/recall measured numerically (eval).
- Scheduled-check success rate ≥ 99%.
- Known breaking point under load (k6).

---

## Non-goals (clarity of intent)
- Not a POS, not accounting, not e-invoicing.
- Not a general enterprise AI agent (that's the turf of funded players like Velents).
- Not a legal compliance guarantee.

> *Disclaimer:* PDPL details are regulatory and change over time — check the latest SDAIA updates before making any promise to a customer.
