"""SQLAlchemy engine factory — shared across storage and cache layers."""

from __future__ import annotations

from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session


@lru_cache(maxsize=1)
def get_engine():
    from configs.config import get_config
    cfg = get_config().database.postgres
    return create_engine(
        cfg.dsn,
        pool_size=cfg.pool_size,
        max_overflow=cfg.max_overflow,
        echo=cfg.echo,
        future=True,
    )


def get_session() -> Session:
    """Return a new ORM session (caller must close)."""
    factory = sessionmaker(bind=get_engine(), expire_on_commit=False)
    return factory()
