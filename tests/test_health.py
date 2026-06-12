from __future__ import annotations


async def test_health_returns_ok_and_correlation_id_header(client):
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    # Every response carries an X-Request-ID — generated when not sent in.
    assert "x-request-id" in {k.lower(): v for k, v in response.headers.items()}


async def test_health_echoes_incoming_request_id(client):
    incoming = "01963b5e-aaaa-7000-8000-000000000001"
    response = await client.get("/health", headers={"X-Request-ID": incoming})
    assert response.status_code == 200
    assert response.headers["x-request-id"] == incoming
