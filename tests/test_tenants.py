"""POST /tenants — proves tenant + audit row land atomically with one correlation_id."""

from __future__ import annotations

from uuid import UUID

import asyncpg
import pytest


async def test_create_tenant_writes_tenant_and_audit_row_in_one_correlation(
    client, app_database_url
):
    incoming = "01963b5e-bbbb-7000-8000-000000000002"
    payload = {"name": "test_tenant_create_one_tx", "business_type": "saas"}

    response = await client.post(
        "/tenants",
        json=payload,
        headers={"X-Request-ID": incoming},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    tenant_id = UUID(body["id"])
    assert body["name"] == payload["name"]
    assert body["business_type"] == payload["business_type"]
    assert body["status"] == "active"
    assert response.headers["x-request-id"] == incoming

    conn = await asyncpg.connect(app_database_url, statement_cache_size=0)
    try:
        tenant_row = await conn.fetchrow(
            "SELECT id, name, business_type, status FROM tenants WHERE id = $1::uuid",
            str(tenant_id),
        )
        assert tenant_row is not None
        assert tenant_row["name"] == payload["name"]

        audit_row = await conn.fetchrow(
            """
            SELECT actor_type, actor_id, event_type, entity_type,
                   entity_id, tenant_id, correlation_id
            FROM audit_log
            WHERE entity_type = 'tenant' AND entity_id = $1::uuid
            """,
            str(tenant_id),
        )
        assert audit_row is not None
        assert audit_row["event_type"] == "tenant.created"
        assert audit_row["actor_type"] == "system"
        assert audit_row["entity_id"] == tenant_id
        assert audit_row["tenant_id"] == tenant_id
        assert audit_row["correlation_id"] == UUID(incoming)
    finally:
        await conn.close()


@pytest.mark.parametrize(
    "bad_payload",
    [
        {"name": "", "business_type": "ecommerce"},
        {"name": "ok", "business_type": "not_in_enum"},
        {"business_type": "ecommerce"},  # missing name
    ],
)
async def test_create_tenant_rejects_bad_input(client, bad_payload):
    response = await client.post("/tenants", json=bad_payload)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
