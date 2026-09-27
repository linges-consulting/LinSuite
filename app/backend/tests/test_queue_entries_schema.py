"""S5: `queue_entries` and `enable_walk_in_queue` (migration 0040, Phase 7 Task 1, #12).

`tests/test_schema.py` already walks `Base.metadata` for every table's constraints/indexes and
grants — this file is only what that generic sweep cannot check: that the migration actually
round-trips, and that the identity CHECK (`ck_queue_entries_identity` — "exactly one of
`customer_id`/`bare_name`", CLAUDE.md's own wording) refuses the two shapes it exists to rule
out, both-null and both-set, rather than trusting the migration SQL is correct because it reads
correctly.
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from core.db import session_scope
from tests.test_migrations import _downgrade_to, _upgrade_to

TABLE = "queue_entries"
_COLUMNS = (
    "id",
    "customer_id",
    "bare_name",
    "requested_service_id",
    "preferred_staff_id",
    "arrived_at",
    "status",
)


async def _table_present() -> bool:
    async with session_scope() as db:
        return bool(await db.scalar(text(f"SELECT to_regclass('{TABLE}') IS NOT NULL")))


async def _toggle_column_present() -> bool:
    async with session_scope() as db:
        return bool(
            await db.scalar(
                text(
                    "SELECT count(*) > 0 FROM information_schema.columns "
                    "WHERE table_name = 'businesses' AND column_name = 'enable_walk_in_queue'"
                )
            )
        )


async def _seed() -> tuple[uuid.UUID, uuid.UUID]:
    """A customer and a service to point a queue entry at, inserted fresh every call — this
    session-scoped database is never wiped between test files (the same reason
    `test_notification_email_sender_migration.py::_ensure_business_row` inserts idempotently
    rather than assuming a clean slate)."""
    async with session_scope() as db:
        customer_id = await db.scalar(
            text(
                "INSERT INTO customers (first_name, last_name) VALUES ('Queue', 'Test') "
                "RETURNING id"
            )
        )
        service_id = await db.scalar(
            text(
                "INSERT INTO services (name, duration_minutes) "
                "VALUES ('Queue Test Service ' || gen_random_uuid(), 30) RETURNING id"
            )
        )
        await db.commit()
    return customer_id, service_id


async def test_the_migration_round_trips(database):
    assert await _table_present()
    assert await _toggle_column_present()

    await _downgrade_to("0039")
    assert not await _table_present()
    assert not await _toggle_column_present()

    await _upgrade_to("head")
    assert await _table_present()
    assert await _toggle_column_present()


async def test_enable_walk_in_queue_defaults_off(database):
    async with session_scope() as db:
        await db.execute(
            text(
                "INSERT INTO businesses (id, name, timezone) VALUES (1, 'Test', 'UTC') "
                "ON CONFLICT (id) DO NOTHING"
            )
        )
        await db.commit()
        value = await db.scalar(text("SELECT enable_walk_in_queue FROM businesses WHERE id = 1"))
    assert value is False


async def test_identity_check_refuses_neither_customer_nor_bare_name(database):
    _customer_id, service_id = await _seed()
    async with session_scope() as db:
        with pytest.raises(IntegrityError):
            await db.execute(
                text("INSERT INTO queue_entries (requested_service_id) VALUES (:s)"),
                {"s": service_id},
            )
            await db.commit()


async def test_identity_check_refuses_both_customer_and_bare_name(database):
    customer_id, service_id = await _seed()
    async with session_scope() as db:
        with pytest.raises(IntegrityError):
            await db.execute(
                text(
                    "INSERT INTO queue_entries (customer_id, bare_name, requested_service_id) "
                    "VALUES (:c, 'Both Set', :s)"
                ),
                {"c": customer_id, "s": service_id},
            )
            await db.commit()


async def test_identity_check_allows_a_known_customer_with_no_bare_name(database):
    customer_id, service_id = await _seed()
    async with session_scope() as db:
        row_id = await db.scalar(
            text(
                "INSERT INTO queue_entries (customer_id, requested_service_id) "
                "VALUES (:c, :s) RETURNING id"
            ),
            {"c": customer_id, "s": service_id},
        )
        await db.commit()
    assert row_id is not None


async def test_identity_check_allows_a_bare_name_with_no_customer(database):
    _customer_id, service_id = await _seed()
    async with session_scope() as db:
        row_id = await db.scalar(
            text(
                "INSERT INTO queue_entries (bare_name, requested_service_id) "
                "VALUES ('Walk-in Jane', :s) RETURNING id"
            ),
            {"s": service_id},
        )
        await db.commit()
    assert row_id is not None


async def test_status_check_refuses_anything_but_the_four_states(database):
    _customer_id, service_id = await _seed()
    async with session_scope() as db:
        with pytest.raises(IntegrityError):
            await db.execute(
                text(
                    "INSERT INTO queue_entries (bare_name, requested_service_id, status) "
                    "VALUES ('Bad Status', :s, 'lost')"
                ),
                {"s": service_id},
            )
            await db.commit()


async def test_a_fresh_row_defaults_to_waiting(database):
    _customer_id, service_id = await _seed()
    async with session_scope() as db:
        status = await db.scalar(
            text(
                "INSERT INTO queue_entries (bare_name, requested_service_id) "
                "VALUES ('Default Status', :s) RETURNING status"
            ),
            {"s": service_id},
        )
        await db.commit()
    assert status == "waiting"


async def test_every_expected_column_is_present(database):
    async with session_scope() as db:
        rows = await db.execute(
            text("SELECT column_name FROM information_schema.columns WHERE table_name = :t"),
            {"t": TABLE},
        )
    assert {row.column_name for row in rows} == set(_COLUMNS)
