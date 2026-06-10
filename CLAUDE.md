> Context and build rules for the **PDPL Autopilot** project. Read this before making any change.

## What this project is
A PDPL readiness assistant for small Saudi businesses. This is a **learning product** — the goal is building CTO-level skills, so architecture and documentation quality matter **more than speed**. No quick hacks at the expense of design.

## Core decision principle (do not break)
- AI **reads / suggests / explains**. Deterministic logic **decides / scores / classifies**.
- A compliance decision (e.g. "you are compliant") must **never** reach the user directly from an AI output — it must pass through a **deterministic verification layer** first.

## Stack (planned)
- **Backend:** FastAPI (Python).
- **DB:** **PostgreSQL** — compliance data is relational (controls ↔ evidence ↔ tenants ↔ audit_log). No Firestore here unless justified in an ADR.
- **AI:** Gemini (for reading/explaining/drafting only).
- **Infra:** Docker Compose · Hetzner VPS.

## Build rules (the gaps we're here to learn — apply from day one)
1. **Design-first:** no code for a feature before it has a design/ADR.
2. **An ADR for every architectural decision** → `docs/adr/NNNN-title.md`.
3. **Observability from the start:** structured (JSON) logging + a correlation ID per request + metrics. No `print`.
4. **Reliability:** every external call uses retry with exponential backoff + an idempotency key + a failure path (DLQ / failure table). Never duplicate an operation.
5. **Secrets:** in environment / a secrets manager only. **Zero secrets in code or the client.**
6. **Immutable audit log** for every check and decision.
7. **Tests + eval:** deterministic parts get unit tests; AI parts get an eval set measuring precision/recall numerically.

## Repo structure
```
pdpl-autopilot/
├── CLAUDE.md            ← this file
├── README.md
├── docs/
│   ├── product-definition.md
│   ├── architecture.md
│   └── adr/
├── build-log/           ← a note after every session
├── src/
└── tests/
```

## Definition of Done (for any feature)
- [ ] Has a design/ADR if it's an architectural decision.
- [ ] Logging + correlation ID.
- [ ] Error handling + retry on external calls.
- [ ] Tests (deterministic) or eval (AI).
- [ ] No secrets in code.
- [ ] A note in `build-log/`.

## Forbidden
- Do not touch the `Review Agent` or `Clinic AI` systems (separate projects, outside this repo).
- Do not add a feature outside MVP scope (see `docs/product-definition.md`) without an explicit decision.
- Do not let AI make a final compliance decision without a deterministic verification layer.

## Working style
- Small, clear commits (conventional commits: `feat:`, `fix:`, `docs:`, `chore:`).
- Discuss with me in Arabic (Saudi dialect); keep all files, code, commits, and ADRs in English.
- Be frank: if a decision is wrong or over-engineered, say so.
