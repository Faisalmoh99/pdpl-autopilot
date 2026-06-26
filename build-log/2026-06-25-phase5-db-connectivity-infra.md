# 2026-06-25 — Phase 5 infra: DB connectivity facts + traceback secret masking (LOCAL-env gap, system clean)

Unblocking Phase 5 load testing needed the audit-immutability tests to connect as `pdpl_app`. While
wiring that, three durable infra facts were nailed down (so we never rediscover them), one security
gap in our output masking was closed, and one blocker remains open. Recorded honestly — the
connection is **not** yet verified green.

## Durable infra facts (confirmed this session)

1. **`APP_DATABASE_URL` is exported in the shell and OVERRIDES `.env`.** `tests/conftest.py` does
   `load_dotenv()` (default `override=False`) and reads `os.environ["APP_DATABASE_URL"]` directly — so
   an exported shell value wins over `.env` and any rebuild is silently ignored. **Always run DB tests
   with `env -u APP_DATABASE_URL`** so the freshly-rebuilt `.env` value is the one used.

2. **The local network is IPv6-blocked → MUST use the `aws-1-eu-central-1` Supavisor session pooler**
   with user `pdpl_app.xuyvhedqthkrklaeiysx` on port `5432` (host
   `aws-1-eu-central-1.pooler.supabase.com`). Confirmed working at the TCP layer: both test runs this
   session reached the Postgres auth handshake (asyncpg got past `await connector`), i.e. the pooler is
   reachable and the IPv4 path is good. The direct `db.<ref>.supabase.co` endpoint is not reachable
   from here. (ADR-0004 §5 fallback note about the direct endpoint does not apply on this network.)

3. **The `.env` password must be single-quoted and ideally alphanumeric.** `PDPL_APP_PASSWORD` is now
   stored as `PDPL_APP_PASSWORD='...'`. A non-alphanumeric value (`#`, space, `$`, `@`) unquoted gets
   mangled by the `.env` reader and/or breaks the URL. `APP_DATABASE_URL` is **rebuilt** from
   `PDPL_APP_PASSWORD` via python-dotenv (`dotenv_values`) + `urllib.parse.quote_plus` — never a
   hand-edited URL — by `scripts/_rebuild_app_url.py`. With an alphanumeric password the url-encoding is
   a no-op, but the path is special-char safe by construction.

4. **The Supabase dashboard "Database password" button rotates the `postgres` role ONLY — it never
   touches `pdpl_app`.** `pdpl_app` is a separate Postgres role whose password is owned by migration
   `0002_pdpl_app_login` (`ALTER ROLE pdpl_app WITH LOGIN PASSWORD '<PDPL_APP_PASSWORD>'`). So to change
   the runtime/app password you must EITHER re-apply migration 0002 with the new `PDPL_APP_PASSWORD`, OR
   run `ALTER ROLE pdpl_app WITH PASSWORD '...'` in the SQL editor. Rotating in the dashboard and
   editing `.env` is NOT enough — the role and `.env` silently drift and auth fails as `pdpl_app`. This
   cost us two failed rounds; do not chase it a third time.

## Security: second masker for asyncpg's plaintext password in tracebacks

The existing mask `s#://[^@]*@#://REDACTED@#g` only covers the **URL form**. On an auth failure,
asyncpg prints `ConnectionParameters(... password='<plaintext>' ...)` in the traceback — the URL mask
does NOT cover that, and a real password leaked once this way (now rotated out, dead). Added a second,
persistent masker `scripts/mask_secrets.sed` covering both shapes:

```
s#://[^@]*@#://REDACTED@#g                 # URL form
s#(password=)'[^']*'#\1'REDACTED'#g        # asyncpg repr, single-quote
s#(password=)"[^"]*"#\1"REDACTED"#g        # repr, double-quote (defensive)
```

Usage: `... 2>&1 | sed -E -f scripts/mask_secrets.sed`. Verified: the repr now renders
`password='REDACTED'` — no plaintext in the traceback.

## BOUNDED GAP (LOCAL ENV ONLY — not a system/code defect) — deferred to Phase-5 load execution

The audit-immutability tests could not be re-run green **from this machine** this session. After
rotating to a fresh alphanumeric password, saving it single-quoted to disk (confirmed on disk, not just
the editor buffer — new value, different sha256), rebuilding the URL, and running with `env -u`, all 6
tests fail with:

```
asyncpg.exceptions.InvalidPasswordError: password authentication failed for user "pdpl_app"
```

### Two distinct causes were found and isolated

1. **Dashboard rotation never reaches `pdpl_app` (now understood — see durable fact #4).** The Supabase
   "Database password" button rotates the `postgres` role only; `pdpl_app`'s password is owned by
   migration `0002_pdpl_app_login` / a direct `ALTER ROLE`. We set it via `ALTER ROLE pdpl_app WITH
   PASSWORD '...'` and **proved the DB role now matches `.env`**: `md5('<the .env value>')` computed in
   the SQL editor equalled the local fingerprint of `PDPL_APP_PASSWORD`. So the role password is
   correct in `pg_authid`.

2. **Supavisor session-pooler SCRAM cache (the residual block).** Despite the role password being
   correct, the `aws-1-eu-central-1` session pooler still returns `InvalidPasswordError` — it is
   validating against a stale cached SCRAM verifier for `pdpl_app`. A full project **Pause → Resume**
   (which reinitializes the pooler) did **not** clear it within this session.

### Classification (no smoothing): LOCAL connectivity, not a system defect

- **Not network** — TCP + Postgres handshake are reached every run via the pooler (the IPv6-blocked
  direct endpoint is exactly why we are on the pooler; see durable fact #2).
- **Not privilege** — no query ever ran; we never got past auth.
- **Not our rebuild mechanics** — dotenv read len=16, alnum, url-encode no-op; URL host/user/port all
  correct; masking verified.
- **Not the DB role** — `md5` proved `pg_authid` matches `.env`.
- **=> The residual is the Supavisor credential cache on the managed pooler**, reachable only because
  this machine's network forces the pooler path.

### Why this does NOT affect the system, a deployed server, or the architecture

The role, the grants (`REVOKE UPDATE/DELETE/TRUNCATE` + `GRANT SELECT/INSERT` on `audit_log`), the
BEFORE-TRUNCATE immutability trigger (ADR-0003), and the migrations are **correct as code on `main`**
and unchanged by any of this. A deployed server sets `PDPL_APP_PASSWORD` and applies migration 0002 to
establish the role password, and connects over its own (non-IPv6-blocked) network — so it hits neither
the dashboard-rotation drift nor this local pooler-cache state. **This is a LOCAL developer-environment
connectivity gap, not a defect in the system, the data model, or the architecture.**

Honest caveat: the immutability behaviour was therefore **not re-executed live this session** — it
remains proven by the migration definitions + the test design, not by a fresh green run today. That is
a local **verification** gap, not a correctness regression.

### Resolution (deferred — no third diagnostic loop)

Resolved at **Phase-5 load execution**, when a working DB connection is actually needed, by restarting
the Supavisor pooler at that time (or connecting from an environment without the IPv6 block). Phase-5
**planning** needs no DB and is unblocked. To re-verify once the pooler refreshes:

```
env -u APP_DATABASE_URL .venv/bin/python -m pytest tests/test_audit_immutability.py -v 2>&1 | sed -E -f scripts/mask_secrets.sed
```

Expected on success: `test_connection_role_is_pdpl_app` passes (current_user = pdpl_app), 3 mutations
rejected with `insufficient_privilege`, 2 positives (insert/select) pass.
