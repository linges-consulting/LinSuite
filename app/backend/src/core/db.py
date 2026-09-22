"""Two engines, two roles (ADR-0001).

`get_engine()` is the application engine: request handlers and Celery tasks use it via
`get_session`. `get_purge_engine()` connects as `linsuite_purge` and is reserved for the
retention-expiry job. Nothing else may import it: `customers/tasks.py` is its one production
caller, and `test_only_the_purge_tasks_reach_the_purge_role` holds that line — the web
process never builds it, so no request handler can reach the purge role at all.
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
from sqlalchemy.pool import NullPool

from core.config import get_settings


class Base(DeclarativeBase):
    """Declarative base for every domain's models; Alembic autogenerate targets its metadata."""


@lru_cache
def get_engine() -> AsyncEngine:
    return create_async_engine(get_settings().database_url, pool_pre_ping=True)


@lru_cache
def get_purge_engine() -> AsyncEngine:
    # `max_overflow=0` beside `pool_size=1`: the privileged role never holds more than one
    # connection, however many callers reach for it at once — the rest queue rather than the
    # pool quietly handing out a second one.
    return create_async_engine(
        get_settings().database_url_purge, pool_pre_ping=True, pool_size=1, max_overflow=0
    )


def get_task_engines() -> tuple[AsyncEngine, AsyncEngine]:
    """(app, purge) engines for one Celery task run, unpooled — dispose both after.

    Not the cached engines above: a task runs under its own `asyncio.run`, a fresh event loop
    each time, and a pooled asyncpg connection from last run's loop is unusable in this one.
    `NullPool` keeps the purge role to the one connection a run actually opens."""
    settings = get_settings()
    return (
        create_async_engine(settings.database_url, poolclass=NullPool),
        create_async_engine(settings.database_url_purge, poolclass=NullPool),
    )


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
