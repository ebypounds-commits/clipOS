"""Alembic environment: DATABASE_URL drives the target database.

``DATABASE_URL`` is the single URL source (for example
``postgresql+psycopg://clipos_dev:...@localhost:5433/clipos_dev``).  When unset,
the fallback ``sqlite:///.alembic-dev.db`` exists only for local offline SQL
rendering; tests and CI always set a URL explicitly.  Model metadata is imported
from ``clipos_api.repository``.
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool

from clipos_api import repository

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option(
    "sqlalchemy.url", os.environ.get("DATABASE_URL", "sqlite:///.alembic-dev.db")
)
target_metadata = repository.Base.metadata


def run_migrations_offline() -> None:
    """Render SQL without a database connection."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Apply migrations using an engine created from the configured URL."""
    from sqlalchemy import engine_from_config

    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
