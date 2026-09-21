"""S5: the newest migration goes up, down and up again against a real database.

Migration 0019 creates the partitioned `audit_access_log`, its partitions, sequence, trigger
function and trigger. Proving the round trip is what stops "the downgrade takes it all back
down" from being a comment nobody ran — a partition or a function left behind would make the
next `upgrade` fail on a fresh `CREATE`.
"""

import asyncio
import os

from alembic import command
from alembic.config import Config
from sqlalchemy import text

from core.db import session_scope
from tests.conftest import BACKEND_DIR

TABLE = "audit_access_log"
FUNCTION = "audit_access_log_append_only"


def _alembic() -> Config:
    cfg = Config(os.path.join(BACKEND_DIR, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(BACKEND_DIR, "alembic"))
    return cfg


async def _present() -> tuple[bool, bool, int]:
    """(table exists, function exists, number of partitions)."""
    async with session_scope() as db:
        table = await db.scalar(text(f"SELECT to_regclass('{TABLE}') IS NOT NULL"))
        function = await db.scalar(
            text("SELECT count(*) > 0 FROM pg_proc WHERE proname = :f"), {"f": FUNCTION}
        )
        partitions = await db.scalar(
            text(
                "SELECT count(*) FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhparent "
                "WHERE c.relname = :t"
            ),
            {"t": TABLE},
        )
    return bool(table), bool(function), int(partitions)


async def _run(step: str) -> None:
    # `alembic/env.py` calls `asyncio.run`, which refuses a thread that already has a running
    # loop — so the migration runs on a worker thread, not this test's.
    runner = command.downgrade if step == "-1" else command.upgrade
    await asyncio.to_thread(runner, _alembic(), step)


async def test_the_newest_migration_round_trips(database):
    assert await _present() == (True, True, 2)

    await _run("-1")
    assert await _present() == (False, False, 0)

    await _run("head")
    assert await _present() == (True, True, 2)
