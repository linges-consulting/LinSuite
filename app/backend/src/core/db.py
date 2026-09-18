"""Two engines, two roles (ADR-0001).

`get_engine()` is the application engine: request handlers and Celery tasks use it via
`get_session`. `get_purge_engine()` connects as `linsuite_purge` and is reserved for the
retention-expiry job. Nothing else may import it.
"""

from collections.abc import AsyncIterator
from functools import lru_cache
from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from core.config import get_settings


class Base(DeclarativeBase):
    """Declarative base for every domain's models; Alembic autogenerate targets its metadata."""


@lru_cache
def get_engine() -> AsyncEngine:
    return create_async_engine(get_settings().database_url, pool_pre_ping=True)


@lru_cache
def get_purge_engine() -> AsyncEngine:
    return create_async_engine(get_settings().database_url_purge, pool_pre_ping=True, pool_size=1)


@lru_cache
def _session_factory() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(get_engine(), expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: one application-role session per request."""
    async with _session_factory()() as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(get_session)]


def session_scope() -> AsyncSession:
    """A session outside a request — application startup, Celery tasks. `async with` it."""
    return _session_factory()()
