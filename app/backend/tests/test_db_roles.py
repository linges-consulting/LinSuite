"""The application engine and the purge engine are distinct roles (ADR-0001)."""

from sqlalchemy import text

from core.db import get_engine, get_purge_engine


async def _current_user(engine) -> str:
    async with engine.connect() as conn:
        return (await conn.execute(text("SELECT current_user"))).scalar_one()


async def test_engines_connect_as_separate_roles(database):
    assert await _current_user(get_engine()) == "linsuite_app"
    assert await _current_user(get_purge_engine()) == "linsuite_purge"
