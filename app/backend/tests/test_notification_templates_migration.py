"""S5: the notification templates migration reverses cleanly and re-seeds on re-upgrade."""

from sqlalchemy import text

from core.db import session_scope
from notifications.models import CHANNELS, NOTIFICATION_TYPES
from tests.test_migrations import _downgrade_to, _upgrade_to


async def _present() -> tuple[bool, int]:
    """(table exists, row count)."""
    async with session_scope() as db:
        table = await db.scalar(
            text("SELECT to_regclass('public.notification_templates') IS NOT NULL")
        )
        if not table:
            return False, 0
        count = await db.scalar(text("SELECT count(*) FROM notification_templates"))
    return True, int(count)


async def test_notification_templates_migration_round_trips(database):
    # One email + one sms row per notification type, seeded by the migration.
    assert await _present() == (True, len(NOTIFICATION_TYPES) * len(CHANNELS))

    await _downgrade_to("0031")
    assert await _present() == (False, 0)

    await _upgrade_to("head")
    assert await _present() == (True, len(NOTIFICATION_TYPES) * len(CHANNELS))


async def test_every_type_channel_pair_is_seeded_and_sms_rows_have_no_subject(database):
    async with session_scope() as db:
        rows = (
            await db.execute(
                text(
                    "SELECT notification_type, channel, subject_template "
                    "FROM notification_templates"
                )
            )
        ).all()

    pairs = {(row.notification_type, row.channel) for row in rows}
    assert pairs == {(t, c) for t in NOTIFICATION_TYPES for c in CHANNELS}
    for row in rows:
        if row.channel == "sms":
            assert row.subject_template is None
        else:
            assert row.subject_template
