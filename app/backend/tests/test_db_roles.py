"""The application engine and the purge engine are distinct roles (ADR-0001)."""

from sqlalchemy import text

from core.db import get_engine, get_purge_engine


async def _current_user(engine) -> str:
    async with engine.connect() as conn:
        return (await conn.execute(text("SELECT current_user"))).scalar_one()


async def test_engines_connect_as_separate_roles(database):
    assert await _current_user(get_engine()) == "linsuite_app"
    assert await _current_user(get_purge_engine()) == "linsuite_purge"


def test_the_purge_engine_never_holds_more_than_one_connection(database):
    """`pool_size=1` alone still lets the pool hand out a second connection under
    contention; `max_overflow=0` is what makes one the hard ceiling for this privileged
    role (M1 ledger, T1)."""
    pool = get_purge_engine().pool
    assert pool.size() == 1
    assert pool._max_overflow == 0
