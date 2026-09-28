"""S5: the notification SMS sender migration (0034) round-trips."""

from sqlalchemy import text

from core.db import session_scope
from tests.test_migrations import _downgrade_to, _upgrade_to

_COLUMNS = (
    "sms_enabled",
    "twilio_account_sid",
    "twilio_auth_token_encrypted",
    "twilio_from_number",
)


async def _columns_present() -> bool:
    async with session_scope() as db:
        rows = await db.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'businesses' AND column_name = ANY(:cols)"
            ),
            {"cols": list(_COLUMNS)},
        )
    return {row.column_name for row in rows} == set(_COLUMNS)


async def _sms_enabled_default() -> bool:
    async with session_scope() as db:
        await db.execute(
            text(
                "INSERT INTO businesses (id, name, timezone) VALUES (1, 'Test', 'UTC') "
                "ON CONFLICT (id) DO NOTHING"
            )
        )
        await db.commit()
        return await db.scalar(text("SELECT sms_enabled FROM businesses WHERE id = 1"))


async def test_the_migration_round_trips(database):
    assert await _columns_present()

    await _downgrade_to("0033")
    assert not await _columns_present()

    await _upgrade_to("head")
    assert await _columns_present()


async def test_sms_enabled_defaults_to_false(database):
    assert await _sms_enabled_default() is False
