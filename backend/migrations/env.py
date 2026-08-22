"""Alembic environment.

The database URL is resolved in this order:

1.  ``sqlalchemy.url`` already set on the Alembic config by the caller
    (used by the test suite to migrate a throwaway database), otherwise
2.  ``DATABASE_URL`` from the typed application settings.

There is deliberately no connection string in ``alembic.ini``. Every schema
change in this project goes through Alembic; nothing creates tables at runtime.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.infrastructure.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def _resolve_url() -> str:
    configured = config.get_main_option("sqlalchemy.url", None)
    if configured:
        return configured
    # Imported lazily so the test suite never loads the developer's .env.
    from app.config.settings import get_settings

    url = get_settings().database_url
    config.set_main_option("sqlalchemy.url", url)
    return url


DATABASE_URL = _resolve_url()
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=DATABASE_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            # SQLite cannot ALTER most things in place; batch mode keeps one set
            # of migration scripts working on both backends.
            render_as_batch=connection.dialect.name == "sqlite",
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
