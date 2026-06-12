"""Shared fixtures.

Tests run against the same Supabase project the app uses — this is a
solo-dev MVP and provisioning a throwaway Postgres per run is friction
we are explicitly skipping. Every test writes data that is either
trivially identifiable (test_* tenant names) or is by design append-only
and harmless (audit_log entries marking the test runs). See ADR-0004.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
from dotenv import load_dotenv
from httpx import ASGITransport, AsyncClient

load_dotenv()


@pytest.fixture(scope="session")
def app():
    """Build the FastAPI app once per test session."""
    # Import inside the fixture so .env is loaded first.
    from pdpl.main import create_app

    return create_app()


@pytest.fixture
async def client(app) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


@pytest.fixture(scope="session")
def app_database_url() -> str:
    url = os.environ.get("APP_DATABASE_URL")
    if not url:
        pytest.skip("APP_DATABASE_URL not set")
    return url.replace("postgresql+asyncpg://", "postgresql://")
