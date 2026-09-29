"""Keeping `audit_access_log` partitioned ahead of time (ADR-0002 §5, pre-flight D7).

The work is migration 0021's `ensure_access_log_partitions()`, a `SECURITY DEFINER` function
the app role may execute. Two callers: `main.lifespan` on every boot, so a fresh January
deploy is safe, and `core.tasks.maintain_partitions` nightly from beat.

**Boot is strict about this year and lenient about next.** The access log is fail-closed, so
an app running without this year's partition answers every profile open with a 500 — better
it refuses to start, loudly. Next year's missing is an error in the log, not an outage: beat
retries nightly and there are months of runway.
"""

import logging
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession

log = logging.getLogger(__name__)

_ENSURE = text("SELECT public.ensure_access_log_partitions()")
# Attached, not merely named — and schema-qualified, so neither a detached table nor a temp
# table of the same name answers for it.
_ATTACHED = text(
    "SELECT EXISTS (SELECT 1 FROM pg_inherits "
    "WHERE inhparent = 'public.audit_access_log'::regclass AND inhrelid = to_regclass(:n))"
)


async def ensure_access_log_partitions(conn: AsyncConnection) -> list[str]:
    """Create this year's and next year's partitions if missing; the names created."""
    created = list(await conn.scalar(_ENSURE))
    if created:
        log.info("partitions: created %s", ", ".join(created))
    return created


async def _partition_attached(conn: AsyncConnection | AsyncSession, year: int) -> bool:
    return bool(await conn.scalar(_ATTACHED, {"n": f"public.audit_access_log_{year}"}))


async def ensure_on_boot(engine: AsyncEngine) -> None:
    try:
        async with engine.begin() as conn:
            await ensure_access_log_partitions(conn)
    except SQLAlchemyError:
        log.exception("partitions: could not ensure audit_access_log partitions")

    current = datetime.now(UTC).year
    async with engine.connect() as conn:
        present = await _partition_attached(conn, current)
    if not present:
        raise RuntimeError(
            f"audit_access_log_{current} is missing: the access log cannot record, "
            "refusing to start"
        )


async def partition_health(conn: AsyncConnection | AsyncSession) -> str:
    """`"ok"` when next UTC year's access-log partition is attached, `"next_year_missing"`
    otherwise (`/api/health`, #85). Boot (`ensure_on_boot`) stays strict about *this* year;
    this is the lenient, next-year signal an uptime monitor or a glance can read."""
    next_year = datetime.now(UTC).year + 1
    return "ok" if await _partition_attached(conn, next_year) else "next_year_missing"
