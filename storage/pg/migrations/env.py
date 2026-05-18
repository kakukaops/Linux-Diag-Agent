"""Alembic env.py — loads DSN from project config."""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# Add repo root to sys.path so we can import project packages
_REPO_ROOT = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Override DSN from project config if available
try:
    from configs.config import get_config as _get_config
    _dsn = _get_config().database.postgres.dsn
    config.set_main_option("sqlalchemy.url", _dsn)
except Exception:
    pass  # fall back to alembic.ini sqlalchemy.url

# Import SQLAlchemy metadata for autogenerate support
from storage.pg.models import Base  # noqa: E402
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
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
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
