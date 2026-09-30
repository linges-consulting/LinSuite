"""S5: the newest migration goes up, down and up again against a real database.

Migration 0019 creates the partitioned `audit_access_log`, its partitions, sequence, trigger
function and trigger. Proving the round trip is what stops "the downgrade takes it all back
down" from being a comment nobody ran — a partition or a function left behind would make the
next `upgrade` fail on a fresh `CREATE`.
"""

import asyncio
import os
import uuid

from alembic import command
from alembic.config import Config
from sqlalchemy import text

from core.db import session_scope
from tests.conftest import BACKEND_DIR
from tests.test_bill_review import as_admin, complete_a_visit, make_tax_component
from tests.test_invoice_issue import (  # noqa: F401 — autouse fixture
    INVOICES,
    claimed_instance,
    issue_url,
)

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
        services = await db.scalar(text("SELECT to_regclass('form_template_services') IS NOT NULL"))
    return bool(columns), bool(check), bool(services)


async def test_migration_0030_round_trips(database):
    """0030 adds `form_templates.applies_to_all`/`valid_for_months` (with its CHECK) and the
    `form_template_services` mapping table; the downgrade takes all three back out."""
    assert await _form_compliance() == (True, True, True)

    await _downgrade_to("0029")
    assert await _form_compliance() == (False, False, False)

    await _upgrade_to("head")
    assert await _form_compliance() == (True, True, True)


async def _report_exports_shape() -> tuple[bool, int]:
    """(`report_exports` exists, count of the legacy `from_date`/`to_date`/`staff_id`
    columns it should no longer have)."""
    async with session_scope() as db:
        table = await db.scalar(text("SELECT to_regclass('report_exports') IS NOT NULL"))
        legacy = await db.scalar(
            text(
                "SELECT count(*) FROM information_schema.columns WHERE table_name = "
                "'report_exports' AND column_name IN ('from_date', 'to_date', 'staff_id')"
            )
        )
    return bool(table), int(legacy)


async def test_migration_0069_migrates_existing_commission_export_rows_and_round_trips(database):
    """0069 (#86) renames `commission_exports` to `report_exports`, replacing its
    `from_date`/`to_date`/`staff_id` columns with `kind`/`params`/`expires_at`. `down_revision`
    is 0066, so downgrading this far and no further reaches the table in its *old* shape —
    proven against a row inserted there directly: the upgrade must migrate it to
    `kind='commission'`, its dates and staff id moved into `params`, and
    `expires_at = created_at + 7 days`; the downgrade must reconstruct the original three
    columns from `params` well enough for the upgrade to run again."""
    assert await _report_exports_shape() == (True, 0)

    await _downgrade_to("0066")
    assert await _report_exports_shape() == (False, 0)

    async with session_scope() as db:
        user_id = await db.scalar(
            text(
                "INSERT INTO users (email, password_hash, role_id) VALUES "
                "('migration-0069@test.example', 'x', "
                "(SELECT id FROM roles WHERE name = 'Administrator')) RETURNING id"
            )
        )
        staff_id = uuid.uuid4()
        export_id = await db.scalar(
            text(
                "INSERT INTO commission_exports "
                "(requested_by, from_date, to_date, staff_id, status, content) VALUES "
                "(:u, '2026-01-01', '2026-01-31', :s, 'ready', 'a,b\n1,2\n') RETURNING id"
            ),
            {"u": user_id, "s": staff_id},
        )
        await db.commit()

    await _upgrade_to("head")
    assert await _report_exports_shape() == (True, 0)

    async with session_scope() as db:
        row = (
            await db.execute(
                text(
                    "SELECT kind, params, status, content, "
                    "expires_at = created_at + interval '7 days' AS expiry_matches "
                    "FROM report_exports WHERE id = :id"
                ),
                {"id": export_id},
            )
        ).one()
    assert row.kind == "commission"
    assert row.params == {
        "from_date": "2026-01-01",
        "to_date": "2026-01-31",
        "staff_id": str(staff_id),
    }
    assert (row.status, row.content, row.expiry_matches) == ("ready", "a,b\n1,2\n", True)

    await _downgrade_to("0066")
    async with session_scope() as db:
        reconstructed = (
            await db.execute(
                text("SELECT from_date, to_date, staff_id FROM commission_exports WHERE id = :id"),
                {"id": export_id},
            )
        ).one()
        await db.execute(text("DELETE FROM commission_exports WHERE id = :id"), {"id": export_id})
        await db.execute(text("DELETE FROM users WHERE id = :id"), {"id": user_id})
        await db.commit()
    assert (str(reconstructed.from_date), str(reconstructed.to_date), reconstructed.staff_id) == (
        "2026-01-01",
        "2026-01-31",
        staff_id,
    )

    await _upgrade_to("head")
    assert await _report_exports_shape() == (True, 0)


async def _rate_columns_present() -> tuple[bool, bool, bool]:
    """(`tax_component_rates` has `rate_ppm`, `invoice_line_taxes` has `rate_ppm`,
    `retail_invoice_line_taxes` has `rate_ppm`) — `False` for a column means the table still
    has the pre-#119 `rate_bp` instead."""
    async with session_scope() as db:
        present = []
        for table in ("tax_component_rates", "invoice_line_taxes", "retail_invoice_line_taxes"):
            present.append(
                bool(
                    await db.scalar(
                        text(
                            "SELECT count(*) > 0 FROM information_schema.columns "
                            "WHERE table_name = :t AND column_name = 'rate_ppm'"
                        ),
                        {"t": table},
                    )
                )
            )
        return tuple(present)


async def test_migration_0076_converts_rates_ppm_and_preserves_an_issued_invoice(client):
    """#119: every stored tax rate moves from basis points to parts per million, ×100. Proven
    against a real, already-issued invoice (through the ordinary `client` HTTP flow, the same
    one every other billing S1 test uses) rather than a hand-built row: downgrading to 0075
    puts `tax_component_rates`/`invoice_line_taxes` back in their pre-#119 shape (`rate_bp`,
    divided by 100 — exact, since every value here originated as a whole basis point ×100);
    re-running 0076's own `upgrade()` must multiply them back ×100 and leave the invoice's
    cents amounts — never touched by either direction — bit for bit identical."""
    assert await _rate_columns_present() == (True, True, True)

    await as_admin(client)
    tax = await make_tax_component(client, code="gst", rate_ppm=50_000)  # a clean 5%, 500 bp
    bill_id, _ = await complete_a_visit(client, price_cents=10_000, tax_component_keys=["GST"])
    issued = await client.post(issue_url(bill_id), json={})
    assert issued.status_code == 201, issued.text
    invoice_id = issued.json()["id"]

    async def snapshot() -> dict:
        resp = await client.get(f"{INVOICES}/{invoice_id}")
        assert resp.status_code == 200, resp.text
        return resp.json()

    before = await snapshot()
    assert before["tax_rates_by_component"] == {"GST": 50_000}
    assert before["lines"][0]["taxes"] == [
        {"component_code": "GST", "rate_ppm": 50_000, "amount_cents": 500}
    ]

    await _downgrade_to("0075")
    assert await _rate_columns_present() == (False, False, False)

    async with session_scope() as db:
        component_rate_bp = await db.scalar(
            text("SELECT rate_bp FROM tax_component_rates WHERE component_id = :id"),
            {"id": tax["id"]},
        )
        line_rate_bp = await db.scalar(
            text(
                "SELECT t.rate_bp FROM invoice_line_taxes t "
                "JOIN invoice_lines l ON l.id = t.invoice_line_id "
                "WHERE l.invoice_id = :i"
            ),
            {"i": invoice_id},
        )
    # Exact: 50_000 ppm was always `500 bp * 100`, so dividing back by 100 loses nothing.
    assert (component_rate_bp, line_rate_bp) == (500, 500)

    await _upgrade_to("head")
    assert await _rate_columns_present() == (True, True, True)

    async with session_scope() as db:
        component_rate_ppm = await db.scalar(
            text("SELECT rate_ppm FROM tax_component_rates WHERE component_id = :id"),
            {"id": tax["id"]},
        )
    assert component_rate_ppm == 50_000

    after = await snapshot()
    assert after == before  # cents and rates alike, bit for bit identical to before the round trip
