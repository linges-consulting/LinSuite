"""Two engines, two roles (ADR-0001).

`get_engine()` is the application engine: request handlers and Celery tasks use it via
`get_session`. The purge role (`linsuite_purge`) is reached only through
`get_task_engines()`, which the Celery tasks in `customers/tasks.py` call once per run.
`get_purge_engine()` is a pooled purge engine the test suite uses to inspect and clean up;
no production code calls it. `test_only_the_purge_tasks_reach_the_purge_role` enforces this:
the web process never builds a purge engine, so no request handler can reach the purge role.
It does not even need the purge DSN (`DATABASE_URL_PURGE` is blanked for the `app` service).

`run_task()` is the shared shape every Celery task with async work inside uses to run it
(`customers/tasks.py`'s erasure tasks, `notifications/tasks.py`'s send tasks): see its own
docstring for why a plain `asyncio.run` isn't always enough.
"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from typing import Annotated, Any

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
    return create_async_engine(purge_dsn(), pool_pre_ping=True, pool_size=1, max_overflow=0)


def get_task_engines() -> tuple[AsyncEngine, AsyncEngine]:
    """(app, purge) engines for one Celery task run, unpooled — dispose both after.

    Not the cached engines above: a task runs under its own `asyncio.run`, a fresh event loop
    each time, and a pooled asyncpg connection from last run's loop is unusable in this one.
    `NullPool` keeps the purge role to the one connection a run actually opens."""
    settings = get_settings()
    return (
        create_async_engine(settings.database_url, poolclass=NullPool),
        create_async_engine(purge_dsn(), poolclass=NullPool),
    )


def purge_dsn() -> str:
    """`DATABASE_URL_PURGE`. Required only where the purge role is used. The worker also
    checks it at boot (`core/celery_app.py`), so a worker without it never starts."""
    dsn = get_settings().database_url_purge
    if not dsn:
        raise RuntimeError(
            "DATABASE_URL_PURGE is not set: the purge tasks cannot run without it "
            "(ADR-0001; see .env.example)."
        )
    return dsn


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


def run_task(work: Callable[..., Awaitable[Any]], *args: object) -> Any:
    """`asyncio.run`, from a real worker (no loop running) or from an eager call made inside a
    request handler in the test suite (a loop is already running there, so run it on a thread
    of its own — `asyncio.run` cannot nest inside a running loop)."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(work(*args))
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, work(*args)).result()
