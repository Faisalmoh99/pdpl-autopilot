"""ai_explanations content-hash cache — repository (ADR-0009 §6, C3b).

A thin, testable persistence layer over the `ai_explanations` table: a pure
`compute_cache_key`, a `get` (cache lookup), and a `put` (cache write). It is a
plain DB layer like `db/audit.py` / `db/outbox.py` — it imports nothing from
`pdpl.ai`, the decision core, or the verifier, so it never drags the trust
boundary across the cache.

WIRING IS DEFERRED to C4 (ADR-0009 §6 / open questions). The intended runtime
sequence — and the one safety property a reader must understand — is:

    key = compute_cache_key(...)            # caller assembles the 6 key fields
    hit = await get(session, key)
    if hit is not None:
        return hit                          # compute-once, reuse
    candidate = await explainer.explain(ctx)
    verdict = verify_explanation(candidate, ...)   # THE GATE (pdpl.verification)
    if not verdict.passed:
        return fallback(...)                # never cached
    await put(session, key, text=candidate, ...)   # ONLY verified text
    return candidate

The verified-only invariant lives in that CALLER, never here: `put` cannot and
does not verify — "store text the caller has already gate-verified" is the
caller's contract, and the gate (ADR-0010 §1) is the safety guarantee. The
cache enforces no safety; it only remembers.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


def _canonicalize(value: str) -> str:
    """NFC-normalize, then trim and collapse internal whitespace runs.

    This is an IDENTITY canonicalization for a cache key — deliberately NOT
    `pdpl.verification.normalize` (the fuzzy Arabic fold used for denylist
    matching). That fold is lossy on purpose (alef variants, hamza, ة/ه, ى/ي,
    lowercasing) which is correct for tolerant *matching* but WRONG for an
    identity key: lossy folding would collide two distinct inputs onto one row
    and serve the wrong explanation. NFC + whitespace is the minimal,
    collision-safe canonicalization (ADR-0009 §6).

    NFC is defensive for today's keyed fields (the deterministic `rationale` is
    pure ASCII machine text, e.g. "privacy notice: 2 of 4 question(s)
    satisfied; gap(s): Q-..."); it becomes load-bearing automatically if a
    keyed field ever carries Arabic.
    """
    return " ".join(unicodedata.normalize("NFC", value).split())


def compute_cache_key(
    *,
    prompt_version: str,
    model: str,
    control_code: str,
    status: str,
    rationale: str,
    lang: str,
) -> str:
    """The content hash for one explanation (ADR-0009 §6).

    The explanation is a pure function of exactly these six fields, so the key
    is too. Every field that can influence the output is here; the EXCLUDED
    fields (`control_title_ar`, `control_description_ar`, `severity_weight`,
    `unsatisfied_questions_ar`) are static functions of `control_code` /
    `(control_code, status, rationale)` and are therefore not parameters at all
    — exclusion is enforced by this signature, not by runtime behaviour. A
    re-seed of question wording must bump `prompt_version` (which IS here) so a
    stale entry can never be served (ADR-0009 §6 / pdpl.catalog discipline).

    Serialization is canonical JSON (sorted keys, unescaped Unicode): it is
    unambiguous regardless of field content — a newline or delimiter inside any
    field is JSON-escaped, so no two distinct field sets can ever serialize to
    the same bytes (a risk a naive delimiter-join would carry). The bytes are
    sha256'd to a 64-char hex digest, matching the `content_hash` TEXT PK.
    """
    payload = {
        "prompt_version": _canonicalize(prompt_version),
        "model": _canonicalize(model),
        "control_code": _canonicalize(control_code),
        "status": _canonicalize(status),
        "rationale": _canonicalize(rationale),
        "lang": _canonicalize(lang),
    }
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


_SELECT_SQL = text(
    "SELECT text FROM ai_explanations WHERE content_hash = :content_hash"
)

_INSERT_SQL = text(
    """
    INSERT INTO ai_explanations (content_hash, lang, text, prompt_version, model)
    VALUES (:content_hash, :lang, :text, :prompt_version, :model)
    ON CONFLICT (content_hash) DO NOTHING
    """
)


async def get(session: AsyncSession, content_hash: str) -> str | None:
    """Return the cached explanation for `content_hash`, or None on a miss."""
    return (
        await session.execute(_SELECT_SQL, {"content_hash": content_hash})
    ).scalar_one_or_none()


async def put(
    session: AsyncSession,
    content_hash: str,
    *,
    text: str,
    lang: str,
    prompt_version: str,
    model: str,
) -> None:
    """Cache one VERIFIED explanation under `content_hash`, inside the caller's
    transaction.

    Stores text the caller has ALREADY gate-verified; the repo does not and
    cannot verify — verified-only is the CALLER's contract (the gate, ADR-0010
    §1, is the safety guarantee, never this table).

    `ON CONFLICT DO NOTHING` semantic (deliberate, ADR-0009 §6): the FIRST
    verified explanation for a key wins PERMANENTLY. Two concurrent requests for
    the same uncached gap may each generate and try to insert; the second is a
    no-op, not a duplicate or an error. Model non-determinism (even at
    temperature 0) means a later generation is silently discarded — that is the
    intended compute-once / reuse semantic, not an accident. A refresh requires
    a `prompt_version` bump (a NEW key), never an in-place update (the row is
    immutable at the DB role, too).
    """
    await session.execute(
        _INSERT_SQL,
        {
            "content_hash": content_hash,
            "lang": lang,
            "text": text,
            "prompt_version": prompt_version,
            "model": model,
        },
    )
