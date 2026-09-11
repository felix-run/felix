"""Alembic async environment for Felix harness tables."""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from felix.config import get_settings
from felix.db.models import Base
from felix.db.session import _connect_args
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def get_url() -> str:
    """The database to migrate.

    Normally `FELIX_DATABASE_URL`. A caller driving Alembic in-process — the store
    conformance suite, which migrates a throwaway database while the ambient settings
    still point at `memory://` — passes an override through `config.attributes` rather
    than mutating the environment and clearing the settings cache.
    """
    override = config.attributes.get("felix_url")
    return str(override) if override else get_settings().database_url


def run_migrations_offline() -> None:
    url = get_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    configuration = config.get_section(config.config_ini_section) or {}
    url = get_url()
    configuration["sqlalchemy.url"] = url
    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        # The same driver bounds and name as the runtime engines: a migration against a
        # blackholed host must give up too, and show as felix-cli in pg_stat_activity.
        connect_args=_connect_args(get_settings(), url),
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
