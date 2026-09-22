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


async def _downgrade_to(revision: str) -> None:
    # `alembic/env.py` calls `asyncio.run`, which refuses a thread that already has a running
    # loop — so the migration runs on a worker thread, not this test's.
    await asyncio.to_thread(command.downgrade, _alembic(), revision)


async def _upgrade_to(revision: str) -> None:
    await asyncio.to_thread(command.upgrade, _alembic(), revision)


async def test_migration_0019_round_trips(database):
    """0019's own round trip — pinned to explicit revisions rather than `-1`/`head`, which
    are relative to whatever the newest migration happens to be. A later ticket (0020, and
    whatever comes after it) adds migrations on top without turning this test into an
    assertion about somebody else's schema."""
    assert await _present() == (True, True, 2)

    await _downgrade_to("0018")
    assert await _present() == (False, False, 0)

    await _upgrade_to("head")
    assert await _present() == (True, True, 2)


async def _partition_function_present() -> bool:
    async with session_scope() as db:
        return bool(
            await db.scalar(
                text("SELECT to_regprocedure('public.ensure_access_log_partitions()') IS NOT NULL")
            )
        )


async def test_migration_0021_round_trips(database):
    """0021 adds only the function: its downgrade drops it and leaves the partitions, which
    are data (0019's downgrade takes them with the parent)."""
    assert await _partition_function_present()

    await _downgrade_to("0020")
    assert not await _partition_function_present()
    assert await _present() == (True, True, 2)

    await _upgrade_to("head")
    assert await _partition_function_present()
    assert await _present() == (True, True, 2)


async def _retention_columns() -> int:
    async with session_scope() as db:
        return int(
            await db.scalar(
                text(
                    "SELECT count(*) FROM information_schema.columns WHERE column_name IN "
                    "('retention_profile', 'retention_profile_set_at', "
                    "'last_clinical_entry_at', 'retention_expires_at')"
                )
            )
        )


async def test_migration_0023_round_trips(database):
    """0023 adds the retention profile and the two customer columns; its downgrade takes all
    four (and the CHECK and index with them) back out, so the re-upgrade's CREATEs succeed."""
    assert await _retention_columns() == 4

    await _downgrade_to("0022")
    assert await _retention_columns() == 0

    await _upgrade_to("head")
    assert await _retention_columns() == 4


async def _document_keys() -> tuple[bool, bool, bool, int]:
    """(table exists, guard function exists, purge may INSERT into audit_events, older
    trigger functions with a pinned search_path)."""
    async with session_scope() as db:
        table = await db.scalar(text("SELECT to_regclass('customer_document_keys') IS NOT NULL"))
        function = await db.scalar(
            text("SELECT to_regprocedure('public.customer_document_keys_guard()') IS NOT NULL")
        )
        insert = await db.scalar(
            text("SELECT has_table_privilege('linsuite_purge', 'audit_events', 'INSERT')")
        )
        pinned = await db.scalar(
            text(
                "SELECT count(*) FROM pg_proc WHERE proconfig IS NOT NULL AND proname IN "
                "('audit_events_append_only', 'audit_access_log_append_only', "
                "'appointments_enforce_staff_concurrency')"
            )
        )
    return bool(table), bool(function), bool(insert), int(pinned)


async def test_migration_0024_round_trips(database):
    """0024 adds the key table, its guard and the purge role's one INSERT; the downgrade takes
    all three back and unpins the three M1/M2 functions it pinned, so the re-upgrade's
    CREATE, GRANT and ALTER succeed."""
    assert await _document_keys() == (True, True, True, 3)

    await _downgrade_to("0023")
    assert await _document_keys() == (False, False, False, 0)

    await _upgrade_to("head")
    assert await _document_keys() == (True, True, True, 3)


async def _erasure() -> tuple[bool, bool, int]:
    """(erasure_requests exists, customers.suppressed_at exists, roles holding
    `customers.erase`)."""
    async with session_scope() as db:
        table = await db.scalar(text("SELECT to_regclass('erasure_requests') IS NOT NULL"))
        column = await db.scalar(
            text(
                "SELECT count(*) > 0 FROM information_schema.columns "
                "WHERE table_name = 'customers' AND column_name = 'suppressed_at'"
            )
        )
        holders = await db.scalar(
            text("SELECT count(*) FROM role_capabilities WHERE capability = 'customers.erase'")
        )
    return bool(table), bool(column), int(holders)


async def test_migration_0025_round_trips(database):
    """0025 adds the request table, the suppression column and the Administrator's grant of
    `customers.erase`; the downgrade takes all three back out. The explicit `REVOKE CREATE ON
    SCHEMA public FROM PUBLIC` is deliberately left in place by the downgrade."""
    assert await _erasure() == (True, True, 1)

    await _downgrade_to("0024")
    assert await _erasure() == (False, False, 0)

    await _upgrade_to("head")
    assert await _erasure() == (True, True, 1)


async def _documents() -> tuple[bool, bool]:
    """(documents exists, customer_record_guard exists)."""
    async with session_scope() as db:
        table = await db.scalar(text("SELECT to_regclass('documents') IS NOT NULL"))
        function = await db.scalar(
            text("SELECT to_regprocedure('public.customer_record_guard()') IS NOT NULL")
        )
    return bool(table), bool(function)


async def test_migration_0026_round_trips(database):
    """0026 adds `documents` and the shared guard; the downgrade drops both, so the
    re-upgrade's CREATEs succeed."""
    assert await _documents() == (True, True)

    await _downgrade_to("0025")
    assert await _documents() == (False, False)

    await _upgrade_to("head")
    assert await _documents() == (True, True)


async def _form_templates() -> tuple[bool, bool, bool, int]:
    """(form_templates exists, form_template_versions exists, its append-only function exists,
    roles holding `forms.manage`)."""
    async with session_scope() as db:
        templates = await db.scalar(text("SELECT to_regclass('form_templates') IS NOT NULL"))
        versions = await db.scalar(text("SELECT to_regclass('form_template_versions') IS NOT NULL"))
        function = await db.scalar(
            text(
                "SELECT to_regprocedure('public.form_template_versions_append_only()') IS NOT NULL"
            )
        )
        holders = await db.scalar(
            text("SELECT count(*) FROM role_capabilities WHERE capability = 'forms.manage'")
        )
    return bool(templates), bool(versions), bool(function), int(holders)


async def test_migration_0027_round_trips(database):
    """0027 adds both template tables, the append-only trigger function and the
    Administrator's `forms.manage`; the downgrade takes all of it back out."""
    assert await _form_templates() == (True, True, True, 1)

    await _downgrade_to("0026")
    assert await _form_templates() == (False, False, False, 0)

    await _upgrade_to("head")
    assert await _form_templates() == (True, True, True, 1)


async def _form_links() -> tuple[bool, bool, int]:
    async with session_scope() as db:
        table = await db.scalar(text("SELECT to_regclass('form_links') IS NOT NULL"))
        function = await db.scalar(
            text("SELECT count(*) > 0 FROM pg_proc WHERE proname = 'form_links_guard'")
        )
        holders = await db.scalar(
            text("SELECT count(*) FROM role_capabilities WHERE capability = 'forms.issue'")
        )
    return bool(table), bool(function), int(holders)


async def test_migration_0028_round_trips(database):
    """0028 adds `form_links` with its guard trigger and gives both seeded roles
    `forms.issue`; the downgrade takes all of it back out, to the explicit revision."""
    assert await _form_links() == (True, True, 2)

    await _downgrade_to("0027")
    assert await _form_links() == (False, False, 0)

    await _upgrade_to("head")
    assert await _form_links() == (True, True, 2)


async def _form_submissions() -> tuple[bool, bool, int]:
    async with session_scope() as db:
        table = await db.scalar(text("SELECT to_regclass('form_submissions') IS NOT NULL"))
        trigger = await db.scalar(
            text("SELECT count(*) > 0 FROM pg_trigger WHERE tgname = 'form_submissions_guard'")
        )
        holders = await db.scalar(
            text("SELECT count(*) FROM role_capabilities WHERE capability = 'forms.view'")
        )
    return bool(table), bool(trigger), int(holders)


async def test_migration_0029_round_trips(database):
    """0029 adds `form_submissions` with its guard trigger (0026's shared function, which the
    downgrade leaves alone) and gives both seeded roles `forms.view`; the downgrade takes the
    table and the grant back out, to the explicit revision."""
    assert await _form_submissions() == (True, True, 2)

    await _downgrade_to("0028")
    assert await _form_submissions() == (False, False, 0)
    async with session_scope() as db:
        assert await db.scalar(
            text("SELECT to_regprocedure('public.customer_record_guard()') IS NOT NULL")
        )

    # "head" rather than "0029": a later ticket (0030) adds a migration on top, and leaving
    # the database at 0029 here would fail the next test to assume it starts at head.
    await _upgrade_to("head")
    assert await _form_submissions() == (True, True, 2)


async def _form_compliance() -> tuple[bool, bool, bool]:
    """(the two new `form_templates` columns exist, its new CHECK exists, `form_template_services`
    exists)."""
    async with session_scope() as db:
        columns = await db.scalar(
            text(
                "SELECT count(*) = 2 FROM information_schema.columns WHERE table_name = "
                "'form_templates' AND column_name IN ('applies_to_all', 'valid_for_months')"
            )
        )
        check = await db.scalar(
            text(
                "SELECT count(*) > 0 FROM pg_constraint WHERE conname = "
                "'ck_form_templates_valid_for_months'"
            )
        )
        services = await db.scalar(
            text("SELECT to_regclass('form_template_services') IS NOT NULL")
        )
    return bool(columns), bool(check), bool(services)


async def test_migration_0030_round_trips(database):
    """0030 adds `form_templates.applies_to_all`/`valid_for_months` (with its CHECK) and the
    `form_template_services` mapping table; the downgrade takes all three back out."""
    assert await _form_compliance() == (True, True, True)

    await _downgrade_to("0029")
    assert await _form_compliance() == (False, False, False)

    await _upgrade_to("head")
    assert await _form_compliance() == (True, True, True)
