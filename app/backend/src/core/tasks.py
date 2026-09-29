import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from core.celery_app import celery_app
from core.config import get_settings
from core.partitions import ensure_access_log_partitions

_CLEANUP_EXPIRED_EXPORTS = text("DELETE FROM report_exports WHERE expires_at < now()")


@celery_app.task
def ping() -> str:
    """Proves the broker -> worker -> result path. Nothing more."""
    return "pong"


@celery_app.task
def maintain_partitions() -> list[str]:
    """Nightly from beat: next year's access-log partition, made ahead of time. Idempotent."""
    return asyncio.run(_maintain_partitions())


async def _maintain_partitions() -> list[str]:
    # An engine per run, as the app role: `asyncio.run` gives each run a fresh event loop,
    # and a pooled connection from last night's loop is unusable in tonight's.
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            return await ensure_access_log_partitions(conn)
    finally:
        await engine.dispose()


@celery_app.task
def cleanup_expired_exports() -> int:
    """Nightly from beat (#86): deletes `report_exports` rows past their 7-day `expires_at`.
    The app role's own DELETE grant (baseline default, 0001) — no purge engine, since these
    are working copies of a report, never records under retention. Idempotent: a row already
    deleted (by a previous run, or by an erasure cascade) just isn't matched again."""
    return asyncio.run(_cleanup_expired_exports())


async def _cleanup_expired_exports() -> int:
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            result = await conn.execute(_CLEANUP_EXPIRED_EXPORTS)
            return result.rowcount
    finally:
        await engine.dispose()
