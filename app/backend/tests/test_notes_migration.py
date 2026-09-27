"""S5: the notes migration reverses cleanly and restores grants and guards."""

from sqlalchemy import text

from core.db import session_scope
from tests.test_migrations import _downgrade_to, _upgrade_to


async def present():
    async with session_scope() as db:
        return (
            await db.execute(
                text(
                    "SELECT to_regclass('public.session_notes') IS NOT NULL, "
                    "to_regclass('public.note_templates') IS NOT NULL, "
                    "to_regprocedure('public.session_notes_guard()') IS NOT NULL"
                )
            )
        ).one()


async def test_session_notes_migration_round_trips(database):
    assert tuple(await present()) == (True, True, True)
    await _downgrade_to("0030")
    assert tuple(await present()) == (False, False, False)
    await _upgrade_to("head")
    assert tuple(await present()) == (True, True, True)
