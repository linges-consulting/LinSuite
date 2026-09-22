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
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

log = logging.getLogger(__name__)

_ENSURE = text("SELECT public.ensure_access_log_partitions()")


async def ensure_access_log_partitions(conn: AsyncConnection) -> list[str]:
    """Create this year's and next year's partitions if missing; the names created."""
    created = list(await conn.scalar(_ENSURE))
    if created:
        log.info("partitions: created %s", ", ".join(created))
    return created


async def ensure_on_boot(engine: AsyncEngine) -> None:
    try:
        async with engine.begin() as conn:
            await ensure_access_log_partitions(conn)
    except SQLAlchemyError:
        log.exception("partitions: could not ensure audit_access_log partitions")

    current = f"audit_access_log_{datetime.now(UTC).year}"
    async with engine.connect() as conn:
        # Attached, not merely named — and schema-qualified, so neither a detached table nor
        # a temp table of the same name answers for it.
        present = await conn.scalar(
            text(
                "SELECT EXISTS (SELECT 1 FROM pg_inherits "
                "WHERE inhparent = 'public.audit_access_log'::regclass "
                "AND inhrelid = to_regclass(:n))"
            ),
            {"n": f"public.{current}"},
        )
    if not present:
        raise RuntimeError(f"{current} is missing: the access log cannot record, refusing to start")
