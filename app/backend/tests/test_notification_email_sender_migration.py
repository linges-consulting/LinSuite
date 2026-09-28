"""S5: the notification email sender migration (0033) round-trips, and its CHECK holds."""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from core.db import session_scope
from tests.test_migrations import _downgrade_to, _upgrade_to

_COLUMNS = (
    "email_sender",
    "resend_api_key_encrypted",
    "resend_from_address",
    "resend_domain_verified_at",
    "smtp_host",
    "smtp_port",
    "smtp_username",
    "smtp_password_encrypted",
    "smtp_from_address",
    "smtp_verified_at",
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


async def _ensure_business_row() -> None:
    # No migration seeds `businesses` — the setup wizard does. A CHECK-constraint test needs
    # a real row to UPDATE (an UPDATE on zero rows never violates anything), and must not
    # assume some other test's wizard run has already created one.
    async with session_scope() as db:
        await db.execute(
            text(
                "INSERT INTO businesses (id, name, timezone) VALUES (1, 'Test', 'UTC') "
                "ON CONFLICT (id) DO NOTHING"
            )
        )
        await db.commit()


async def test_the_migration_round_trips(database):
    assert await _columns_present()

    await _downgrade_to("0032")
    assert not await _columns_present()

    await _upgrade_to("head")
    assert await _columns_present()


async def test_email_sender_check_constraint_refuses_anything_but_resend_or_smtp(database):
    await _ensure_business_row()
    async with session_scope() as db:
        with pytest.raises(IntegrityError):
            await db.execute(text("UPDATE businesses SET email_sender = 'twilio' WHERE id = 1"))
            await db.commit()


async def test_email_sender_check_constraint_allows_the_two_real_senders_and_null(database):
    await _ensure_business_row()
    async with session_scope() as db:
        for value in ("resend", "smtp", None):
            await db.execute(
                text("UPDATE businesses SET email_sender = :v WHERE id = 1"), {"v": value}
            )
        await db.commit()
