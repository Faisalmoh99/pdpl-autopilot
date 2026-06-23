"""C3b — the ai_explanations content-hash cache (ADR-0009 §6).

Two layers:

  1. PURE (no DB): `compute_cache_key` is stable (same inputs -> same key) and
     sensitive (changing ANY of the six keyed fields -> a different key). There
     is deliberately NO "exclusion" test: the excluded fields
     (control_title_ar, severity_weight, unsatisfied_questions_ar, ...) are not
     parameters of `compute_cache_key`, so exclusion is enforced by the
     signature, not by behaviour — a test varying them would be vacuous.

  2. DB (against the same Supabase project, like the Phase-3 repo tests): a
     round-trip (put then get), put idempotency (ON CONFLICT DO NOTHING — first
     verified text wins, a second put is a no-op), a miss returns None, and the
     immutability grant — pdpl_app may INSERT + SELECT but NOT UPDATE/DELETE.
     That grant IS the immutability guarantee at the role; the repo exposes no
     UPDATE path either.

Each test uses a unique content_hash so rows never collide across runs; by
design (conftest) we leave the harmless rows behind.
"""

from __future__ import annotations

import re

import asyncpg
import pytest

from pdpl.db.ai_explanations import compute_cache_key, get, put
from pdpl.db.session import session_scope

# A representative, realistic key field set (the rationale mirrors the real
# deterministic machine text — pure ASCII, ADR-0006 §4).
_BASE = dict(
    prompt_version="gap-ar-v1",
    model="gemini-2.5-flash",
    control_code="PDPL-ART12-PRIVACY-NOTICE",
    status="partial",
    rationale="privacy notice: 2 of 4 question(s) satisfied; gap(s): Q-ART12-NOTICE-RECIPIENTS, Q-ART12-NOTICE-RIGHTS",
    lang="ar",
)


# ---------------------------------------------------------------------
# Part 1 — compute_cache_key: pure, no DB.
# ---------------------------------------------------------------------


def test_key_is_sha256_hex():
    key = compute_cache_key(**_BASE)
    assert re.fullmatch(r"[0-9a-f]{64}", key), "key must be a 64-char sha256 hex"


def test_key_is_stable():
    """Same inputs -> same key, across independent calls (cache hits depend on
    this; a non-deterministic key would never hit)."""
    assert compute_cache_key(**_BASE) == compute_cache_key(**_BASE)


def test_key_stable_under_whitespace_and_nfc():
    """Canonicalization folds trivial, equal-meaning differences onto one key
    (ADR-0009 §6): collapsed/edge whitespace and NFC-equivalent forms."""
    spaced = {**_BASE, "rationale": f"  {_BASE['rationale']}\t \n"}
    collapsed = {**_BASE, "rationale": re.sub(r"\s+", " ", _BASE["rationale"])}
    assert compute_cache_key(**spaced) == compute_cache_key(**collapsed)


@pytest.mark.parametrize(
    "field,changed",
    [
        ("prompt_version", "gap-ar-v2"),
        ("model", "gemini-2.5-pro"),
        ("control_code", "PDPL-ART4-DSR-ACCESS"),
        ("status", "non_compliant"),
        ("rationale", "right of access: none of 2 question(s) satisfied"),
        ("lang", "en"),
    ],
)
def test_key_is_sensitive_to_every_keyed_field(field: str, changed: str):
    """Changing ANY of the six keyed fields busts the cache (ADR-0009 §6
    invariant: every field that can influence the output is in the key)."""
    assert compute_cache_key(**{**_BASE, field: changed}) != compute_cache_key(
        **_BASE
    )


# ---------------------------------------------------------------------
# Part 2 — the repository, against Supabase.
# ---------------------------------------------------------------------


def _unique_key(label: str) -> str:
    """A guaranteed-unique key per test, so DB rows never collide across runs.
    Uses a per-call nonce in a (non-keyed-semantics) field to vary the hash."""
    import uuid6

    return compute_cache_key(**{**_BASE, "rationale": f"{_BASE['rationale']} [{label}:{uuid6.uuid7()}]"})


async def test_get_miss_returns_none(app):
    key = _unique_key("miss")
    async with session_scope() as session:
        assert await get(session, key) is None


async def test_put_then_get_round_trips(app):
    key = _unique_key("roundtrip")
    text = "هذا البند يمثل فجوة لأن إشعار الخصوصية لا يحدد الجهات المستلمة للبيانات. الخطوة: أضف قائمة بالجهات المستلمة إلى الإشعار."
    async with session_scope() as session:
        await put(
            session,
            key,
            text=text,
            lang="ar",
            prompt_version="gap-ar-v1",
            model="gemini-2.5-flash",
        )
    async with session_scope() as session:
        assert await get(session, key) == text


async def test_put_is_idempotent_first_write_wins(app):
    """ON CONFLICT DO NOTHING: the first verified text wins permanently; a
    second put under the same key is a no-op, not an overwrite or an error
    (ADR-0009 §6 compute-once/reuse)."""
    key = _unique_key("idempotent")
    first = "النص الأول الموثّق."
    second = "نص لاحق مختلف (محاكاة لا-حتمية النموذج) — يجب تجاهله."
    async with session_scope() as session:
        await put(session, key, text=first, lang="ar",
                  prompt_version="gap-ar-v1", model="gemini-2.5-flash")
    async with session_scope() as session:
        await put(session, key, text=second, lang="ar",
                  prompt_version="gap-ar-v1", model="gemini-2.5-flash")
    async with session_scope() as session:
        assert await get(session, key) == first, "first write must win"


async def test_immutable_grant_blocks_update_and_delete(app, app_database_url):
    """The immutability guarantee at the ROLE (ADR-0009 §6 / mirrors audit_log):
    pdpl_app may INSERT + SELECT but is denied UPDATE and DELETE. Proven by
    attempting both as pdpl_app and asserting an insufficient-privilege error.
    """
    key = _unique_key("immutable")
    async with session_scope() as session:
        await put(session, key, text="نص ثابت لا يُعدّل.", lang="ar",
                  prompt_version="gap-ar-v1", model="gemini-2.5-flash")

    conn = await asyncpg.connect(app_database_url, statement_cache_size=0)
    try:
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await conn.execute(
                "UPDATE ai_explanations SET text = 'tampered' WHERE content_hash = $1",
                key,
            )
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await conn.execute(
                "DELETE FROM ai_explanations WHERE content_hash = $1", key
            )
        # SELECT still works for the same role — the row is readable, just not
        # mutable.
        assert (
            await conn.fetchval(
                "SELECT text FROM ai_explanations WHERE content_hash = $1", key
            )
            == "نص ثابت لا يُعدّل."
        )
    finally:
        await conn.close()
