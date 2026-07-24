"""Alembic environment — sync psycopg engine, URL + metadata from the app."""

from __future__ import annotations

from logging.config import fileConfig

from sqlalchemy import create_engine, pool

import truth_engine.db.models  # noqa: F401  (register all tables on the metadata)
from alembic import context
from truth_engine.config import get_settings
from truth_engine.db.base import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _url() -> str:
    # psycopg 3 handles the "+psycopg" URL for both sync and async engines.
    return get_settings().database_url


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = create_engine(_url(), poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
