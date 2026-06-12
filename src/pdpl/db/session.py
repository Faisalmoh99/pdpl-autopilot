"""Async DB session — runs as pdpl_app (see ADR-0004 §5).

The role IS the enforcement. The pooler vs direct port is a perf choice,
not a security one. If APP_DATABASE_URL points at the Supavisor pool
(transaction mode), prepared statements must be disabled — see comment
on `connect_args` below.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from pdpl.config import get_settings

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def _build_engine() -> AsyncEngine:
    settings = get_settings()
    url = settings.app_database_url.get_secret_value()
    # Supavisor's transaction-mode pool does NOT support prepared statements.
    # SQLAlchemy + asyncpg will eagerly prepare statements unless we disable
    # both caches. Harmless on a direct connection; required on the pool.
    # See ADR-0004 §5 "Supavisor + asyncpg technical note".
    return create_async_engine(
        url,
        pool_pre_ping=True,
        connect_args={
            "statement_cache_size": 0,
            "prepared_statement_cache_size": 0,
        },
    )


def get_engine() -> AsyncEngine:
    global _engine, _session_factory
    if _engine is None:
        _engine = _build_engine()
        _session_factory = async_sessionmaker(
            _engine,
            expire_on_commit=False,
            class_=AsyncSession,
        )
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    get_engine()
    assert _session_factory is not None
    return _session_factory


async def dispose_engine() -> None:
    """Close the engine and reset module state. Used on shutdown / in tests."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """One transaction per call. Commits on clean exit, rolls back on raise."""
    factory = get_session_factory()
    async with factory() as session:
        async with session.begin():
            yield session
