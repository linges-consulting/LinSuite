"""S5: the newest migration goes up, down and up again against a real database.

Migration 0018 is a `CREATE OR REPLACE` of the staff-concurrency trigger function, so its
downgrade is the only thing standing between an operator stepping back one revision and a
half-replaced rule. Proving the round trip — and that the body actually changes both ways —
is what stops "the downgrade restores 0017 verbatim" from being a comment nobody ran.
"""

import asyncio
import os

from alembic import command
from alembic.config import Config
from sqlalchemy import text

from core.db import session_scope
from tests.conftest import BACKEND_DIR

# Only 0018's body mentions `TG_OP`: it is the scope guard that stops a status change being
# re-checked against a limit the row already satisfied.
SCOPE_GUARD = "TG_OP"


def _alembic() -> Config:
    cfg = Config(os.path.join(BACKEND_DIR, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(BACKEND_DIR, "alembic"))
    return cfg


async def _trigger_body() -> str:
    async with session_scope() as db:
        return await db.scalar(
            text("SELECT pg_get_functiondef('appointments_enforce_staff_concurrency'::regproc)")
        )


async def _run(step: str) -> None:
    # `alembic/env.py` calls `asyncio.run`, which refuses a thread that already has a running
    # loop — so the migration runs on a worker thread, not this test's.
    runner = command.downgrade if step == "-1" else command.upgrade
    await asyncio.to_thread(runner, _alembic(), step)


async def test_the_newest_migration_round_trips(database):
    assert SCOPE_GUARD in await _trigger_body()

    await _run("-1")
    assert SCOPE_GUARD not in await _trigger_body()

    await _run("head")
    assert SCOPE_GUARD in await _trigger_body()
