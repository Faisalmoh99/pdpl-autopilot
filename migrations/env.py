"""Alembic environment.

Reads DATABASE_URL_DIRECT from the environment (via .env / python-dotenv) and
runs migrations against the direct Supabase connection. The application's
runtime (asyncpg, pooled) is a separate connection and is not used here.

target_metadata is None on purpose: there are no SQLAlchemy ORM models in
this phase. Migrations are hand-written. When the FastAPI app layer arrives
and ORM models are added, set target_metadata to Base.metadata to enable
autogenerate as a diff helper (still review every output before committing).
See docs/02-data-model.md.
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from dotenv import load_dotenv
from sqlalchemy import engine_from_config, pool

load_dotenv()

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

database_url = os.environ.get("DATABASE_URL_DIRECT")
if not database_url:
    raise RuntimeError(
        "DATABASE_URL_DIRECT is not set. Copy .env.example to .env and fill in "
        "the direct (port 5432, non-pooled) Supabase connection string. "
        "Alembic cannot use the pooler URL — DDL is not supported there."
    )
config.set_main_option("sqlalchemy.url", database_url)

target_metadata = None


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting to the database."""
    context.configure(
        url=database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live database."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
