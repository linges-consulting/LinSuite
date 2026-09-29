"""S5: `linsuite_backup` (migration 0070, #89) holds SELECT and nothing else, everywhere —
including a table created after the migration ran, and including every partition of
`audit_access_log` (0021, re-`CREATE OR REPLACE`d by 0070).

Kept in its own file rather than folded into `test_schema.py`'s per-role sweep: #83 (purge-role
default-deny) touches that file's role tables in the same milestone, and this role's shape is
simple enough not to need the shared machinery there.
"""

import os

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from core.db import session_scope

BACKUP_ROLE = "linsuite_backup"
WRITE_PRIVILEGES = ("INSERT", "UPDATE", "DELETE", "TRUNCATE")


def _backup_dsn() -> str:
    # No `DATABASE_URL_BACKUP` in Settings (core/config.py) — the backup role is never used by
    # the Python app, only by `pg_dump` (infra/backup/backup.sh). The test containers create
    # the role with no password (`POSTGRES_HOST_AUTH_METHOD=trust`, tests/conftest.py), same as
    # `DATABASE_URL_PURGE`, so building the DSN from that one is simplest and needs no new
    # fixture wiring.
    return os.environ["DATABASE_URL_PURGE"].replace("linsuite_purge", BACKUP_ROLE, 1)


async def _tables(db) -> list[str]:
    return list(
        await db.scalars(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))
    )


async def test_backup_role_holds_select_and_nothing_else_on_every_table(database):
    async with session_scope() as db:

        async def may(table: str, privilege: str) -> bool:
            return await db.scalar(
                text("SELECT has_table_privilege(:r, :t, :p)"),
                {"r": BACKUP_ROLE, "t": table, "p": privilege},
            )

        for table in await _tables(db):
            assert await may(table, "SELECT") is True, f"{BACKUP_ROLE} SELECT on {table}"
            for privilege in WRITE_PRIVILEGES:
                assert await may(table, privilege) is False, (
                    f"{BACKUP_ROLE} must not hold {privilege} on {table}"
                )


async def test_backup_role_holds_select_and_nothing_else_on_every_sequence(database):
    async with session_scope() as db:
        sequences = list(
            await db.scalars(
                text("SELECT sequencename FROM pg_sequences WHERE schemaname = 'public'")
            )
        )
        assert sequences  # not vacuous — `businesses.id` alone would give it an Identity sequence
        for sequence in sequences:
            select = await db.scalar(
                text("SELECT has_sequence_privilege(:r, :s, 'SELECT')"),
                {"r": BACKUP_ROLE, "s": sequence},
            )
            update = await db.scalar(
                text("SELECT has_sequence_privilege(:r, :s, 'UPDATE')"),
                {"r": BACKUP_ROLE, "s": sequence},
            )
            assert select is True, f"{BACKUP_ROLE} SELECT on sequence {sequence}"
            assert update is False, f"{BACKUP_ROLE} must not hold UPDATE on sequence {sequence}"


async def test_backup_role_gets_select_on_a_table_created_after_the_migration(database):
    """`ALTER DEFAULT PRIVILEGES` (0070) reaches a table nobody has written a migration for
    yet — proven directly, rather than trusted, the same way `test_schema.py` proves it for
    the app and purge roles."""
    owner = create_async_engine(os.environ["DATABASE_URL_MIGRATE"])
    try:
        async with owner.begin() as conn:
            await conn.execute(text("CREATE TABLE test_future_backup_probe (id int PRIMARY KEY)"))
        try:
            async with session_scope() as db:
                granted = await db.scalar(
                    text("SELECT has_table_privilege(:r, 'test_future_backup_probe', 'SELECT')"),
                    {"r": BACKUP_ROLE},
                )
                insertable = await db.scalar(
                    text("SELECT has_table_privilege(:r, 'test_future_backup_probe', 'INSERT')"),
                    {"r": BACKUP_ROLE},
                )
            assert granted is True
            assert insertable is False
        finally:
            async with owner.begin() as conn:
                await conn.execute(text("DROP TABLE test_future_backup_probe"))
    finally:
        await owner.dispose()


async def test_backup_role_gets_select_on_a_partition_made_after_the_migration(database):
    """`ensure_access_log_partitions()` (0021, re-`CREATE OR REPLACE`d by 0070) grants this
    role SELECT on each `audit_access_log_<year>` child explicitly — the table's own default
    privileges never reach a partition made this way (0021's docstring)."""
    async with session_scope() as db:
        partitions = list(
            await db.scalars(
                text(
                    "SELECT inhrelid::regclass::text FROM pg_inherits "
                    "WHERE inhparent = 'public.audit_access_log'::regclass"
                )
            )
        )
        assert partitions  # this year's and next year's exist from migration 0021 onward
        for partition in partitions:
            granted = await db.scalar(
                text("SELECT has_table_privilege(:r, :t, 'SELECT')"),
                {"r": BACKUP_ROLE, "t": partition},
            )
            assert granted is True, f"{BACKUP_ROLE} SELECT on {partition}"


async def test_backup_role_cannot_write_connected_as_itself(database):
    """The grant check above is `has_table_privilege`; this is the same claim proven by
    actually connecting as the role and trying — belt and suspenders, the same shape
    `test_db_roles.py` uses for the app/purge split."""
    backup = create_async_engine(_backup_dsn())
    try:
        async with backup.connect() as conn:
            current_user = (await conn.execute(text("SELECT current_user"))).scalar_one()
            assert current_user == BACKUP_ROLE

            with pytest.raises(DBAPIError) as refused:
                await conn.execute(
                    text("INSERT INTO customers (first_name, last_name) VALUES ('x', 'y')")
                )
            assert getattr(refused.value.orig, "sqlstate", None) == "42501"
            await conn.rollback()

            with pytest.raises(DBAPIError) as refused_delete:
                await conn.execute(text("DELETE FROM customers"))
            assert getattr(refused_delete.value.orig, "sqlstate", None) == "42501"
            await conn.rollback()

            # And it can read — the point of the role existing at all.
            count = await conn.scalar(text("SELECT count(*) FROM customers"))
            assert count is not None
    finally:
        await backup.dispose()
