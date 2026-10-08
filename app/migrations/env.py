from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.config import settings
from app.database import Base

# Import all models so Alembic can detect them for autogenerate
from app.models import *  # noqa: F403

# Alembic Config object — provides access to alembic.ini values
config = context.config

# ── Database URL resolution ─────────────────────────────────────────────
#
# WHY THIS IS NOT AN UNCONDITIONAL ASSIGNMENT:
#
# This used to be `config.set_main_option("sqlalchemy.url", settings.DATABASE_URL)`,
# which clobbered any URL a programmatic caller had already chosen. The test
# fixture in tests/conftest.py drives Alembic in-process against
# settings.TEST_DATABASE_URL, and then runs `downgrade base` on teardown.
# In CI both URLs point at the same throwaway `weft_test` database so the
# clobber was invisible — but locally DATABASE_URL is `weft_dev`, so the
# unconditional assignment would have migrated and then *dropped every table
# in the developer's dev database*.
#
# `config.attributes` is empty when Alembic is driven from the CLI and is only
# populated by an in-process caller that builds its own `Config`. So:
#   - programmatic caller sets attributes["sqlalchemy_url"]  -> that URL wins
#   - plain `alembic ...` from a shell or from CI            -> settings.DATABASE_URL
#
# Do NOT reinstate the unconditional assignment.
_url_override = config.attributes.get("sqlalchemy_url")
config.set_main_option("sqlalchemy.url", _url_override or settings.DATABASE_URL)

# Configure Python logging from alembic.ini.
# disable_existing_loggers=False because env.py is now also loaded in-process by
# tests/conftest.py: the default (True) would switch off every logger created
# before this point (pytest's, the app's, the test suite's) for the rest of the
# session. CLI behaviour is unaffected — the alembic.ini handlers still apply.
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# MetaData for autogenerate support
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    Generates SQL scripts without requiring a live database connection.
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    """Execute migrations against a live connection."""
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Run migrations in 'online' mode using an async engine."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Entry point for online migrations — wraps async execution."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
