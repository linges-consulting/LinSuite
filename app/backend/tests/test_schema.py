"""S5: the schema the database actually has — every table, not three tickets' worth.

Three things, checked once over `Base.metadata` rather than re-copied per ticket (fix wave,
findings 13 and 14):

**Drift.** A constraint or index that exists only in a migration is invisible to SQLAlchemy's
model of the schema, with two consequences: the next `alembic revision --autogenerate`
compares the metadata against the database and helpfully emits a DROP for it, and any
`create_all` path builds the table without it. For `ex_appointment_resources_no_overlap` or
`ex_working_hours_no_overlap` that is the one failure the whole design exists to prevent.
Compared by name, in both directions. An EXCLUDE or UNIQUE constraint also creates an index
of the same name, so the two sides are unioned before comparing and collapse cleanly.

**Grants.** `0001_baseline` sets them with `ALTER DEFAULT PRIVILEGES` and no `FOR ROLE`, so
they are bound to whichever role ran that migration — which holds for every migration run
through `DATABASE_URL_MIGRATE`, and silently does not for one an operator runs by hand as
`postgres`. The runbook rule is "always migrate as the schema owner"; this is the assertion
that catches the day somebody did not. The append-only tables are the exceptions, named per
table in `APP_EXCEPTIONS`: the app role may INSERT and SELECT and nothing else (ADR-0002).
A partitioned table's children are checked too — a REVOKE on the parent does not reach a
partition created afterwards, so whatever creates one must revoke on it as well.

**The triggers.** `tg_appointments_staff_concurrency` is policy that no constraint can
express, and `audit_access_log_no_rewrite` and `audit_events_no_rewrite` are the second half
of append-only (a grant is undone by one careless `GRANT ALL`, a trigger by one `DISABLE
TRIGGER`); nothing but this says each is still attached and still enabled.
"""

from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from core.config import get_settings
from core.db import Base, session_scope

# `linsuite_app` writes everywhere except the append-only logs; `linsuite_purge` is the
# privileged role the retention-expiry job runs as: it reads, deletes, and records its purges.
APP_ROLE, PURGE_ROLE = "linsuite_app", "linsuite_purge"
APP_PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE")
# Per-table departures from the default. Extend this, never bypass the test.
APP_EXCEPTIONS: dict[str, tuple[str, ...]] = {
    "session_notes": ("SELECT", "INSERT", "UPDATE"),
    "audit_events": ("SELECT", "INSERT"),
    "audit_access_log": ("SELECT", "INSERT"),
    # A key is written once and never rewritten; only the purge role destroys one (0024).
    "customer_document_keys": ("SELECT", "INSERT"),
    # The one business-owned key (#55/ADR-0003): written once, never rewritten, and — unlike
    # a customer key — never destroyed by anyone in v1 either (0044).
    "business_document_keys": ("SELECT", "INSERT"),
    # The record that an erasure was honoured: the app stamps `purged_at`, never deletes it.
    "erasure_requests": ("SELECT", "INSERT", "UPDATE"),
    # Sealed documents are immutable; only the purge role removes one (0026).
    "documents": ("SELECT", "INSERT"),
    # A published form version is frozen; nobody but the owner removes one (0027).
    "form_template_versions": ("SELECT", "INSERT"),
    # The app stamps `revoked_at`/`consumed_at` (trigger-guarded); it never deletes a link.
    "form_links": ("SELECT", "INSERT", "UPDATE"),
    # A filled-in form is immutable; only the purge role removes one, when not held (0029).
    "form_submissions": ("SELECT", "INSERT"),
    # The stock movement ledger is append-only; nothing purges it in v1, but the trigger
    # still lets the purge role through, same shape as `audit_events` (#61, 0050).
    "stock_movements": ("SELECT", "INSERT"),
    # Voidable (#65, 0054): UPDATE stays granted — it is what reaches the one permitted
    # cancel transition `invoices_voidable_guard` checks for — but DELETE never does.
    "invoices": ("SELECT", "INSERT", "UPDATE"),
    # Frozen invoice snapshot rows: append-only, same shape as `stock_movements` (#65, 0054).
    "invoice_lines": ("SELECT", "INSERT"),
    "invoice_line_discounts": ("SELECT", "INSERT"),
    "invoice_line_taxes": ("SELECT", "INSERT"),
    # Voidable-adjacent (#71, 0055): UPDATE stays granted for the one permitted activation
    # transition `package_purchases_activation_guard` checks for; DELETE never does.
    "package_purchases": ("SELECT", "INSERT", "UPDATE"),
    # Frozen credit-grant rows: append-only, same shape as `invoice_lines` (#71, 0055).
    "package_purchase_credits": ("SELECT", "INSERT"),
    # Voidable (#75, 0056) — the same shape as `invoices`: UPDATE stays granted for the one
    # permitted cancel transition, DELETE never does.
    "retail_invoices": ("SELECT", "INSERT", "UPDATE"),
    # Frozen retail invoice snapshot rows: append-only, same shape as `invoice_lines` (#75, 0056).
    "retail_invoice_lines": ("SELECT", "INSERT"),
}
# The purge role reads and deletes everywhere and writes nowhere — except the fact of its own
# purge, which ADR-0001 §6 puts in the purge transaction (0024, pre-flight D11).
PURGE_PRIVILEGES = ("SELECT", "DELETE")
PURGE_EXCEPTIONS: dict[str, tuple[str, ...]] = {
    "audit_events": ("SELECT", "INSERT", "DELETE"),
    # Never destroyed by the purge role either (0044) — there is no crypto-shred of the
    # business's own key in v1.
    "business_document_keys": ("SELECT",),
}
# (table, trigger) pairs that must be attached and firing.
TRIGGERS = (
    ("session_notes", "session_notes_guard"),
    ("session_notes", "session_notes_delete_guard"),
    ("appointments", "tg_appointments_staff_concurrency"),
    ("audit_events", "audit_events_no_rewrite"),
    ("audit_access_log", "audit_access_log_no_rewrite"),
    ("customer_document_keys", "customer_document_keys_guard"),
    ("business_document_keys", "business_document_keys_guard"),
    ("documents", "documents_guard"),
    ("form_template_versions", "form_template_versions_append_only"),
    ("form_links", "form_links_guard"),
    ("form_submissions", "form_submissions_guard"),
    ("stock_movements", "stock_movements_no_rewrite"),
    ("invoices", "invoices_voidable_guard"),
    ("invoice_lines", "invoice_lines_no_rewrite"),
    ("invoice_line_discounts", "invoice_line_discounts_no_rewrite"),
    ("invoice_line_taxes", "invoice_line_taxes_no_rewrite"),
    ("package_purchases", "package_purchases_activation_guard"),
    ("package_purchase_credits", "package_purchase_credits_no_rewrite"),
    ("retail_invoices", "retail_invoices_voidable_guard"),
    ("retail_invoice_lines", "retail_invoice_lines_no_rewrite"),
)


def tables() -> list[str]:
    return sorted(Base.metadata.tables)


# Postgres's own auto-names: `<table>_pkey` for a primary key and `<table>_<column>_key` for a
# column-level `unique=True`. Neither side of this comparison can see them — SQLAlchemy leaves
# such constraints unnamed in the metadata — so they are dropped from both. Everything else is
# named deliberately, which is what makes the comparison worth making.
_AUTO_NAMED = ("_pkey", "_key")


def _ours(names: set[str]) -> set[str]:
    return {name for name in names if not name.endswith(_AUTO_NAMED)}


async def test_every_table_declares_the_constraints_and_indexes_the_database_has(database):
    async with session_scope() as db:
        for table in tables():
            constraints = set(
                await db.scalars(
                    text(
                        "SELECT conname FROM pg_constraint "
                        "WHERE conrelid = cast(:t AS regclass) AND contype IN ('c', 'u', 'x')"
                    ),
                    {"t": table},
                )
            )
            indexes = set(
                await db.scalars(
                    text("SELECT indexname FROM pg_indexes WHERE tablename = :t"), {"t": table}
                )
            )
            actual = _ours(constraints | indexes)

            metadata = Base.metadata.tables[table]
            declared = _ours(
                {c.name for c in metadata.constraints if c.name}
                | {i.name for i in metadata.indexes if i.name}
            )

            assert actual == declared, (
                f"{table}: only in the database {sorted(actual - declared)}, "
                f"only in the model {sorted(declared - actual)}"
            )


async def test_both_roles_hold_the_privileges_they_are_meant_to_on_every_table(database):
    async with session_scope() as db:

        async def may(role: str, table: str, privilege: str) -> bool:
            return await db.scalar(
                text("SELECT has_table_privilege(:r, :t, :p)"),
                {"r": role, "t": table, "p": privilege},
            )

        async def children(table: str) -> list[str]:
            return list(
                await db.scalars(
                    text(
                        "SELECT inhrelid::regclass::text FROM pg_inherits "
                        "WHERE inhparent = cast(:t AS regclass)"
                    ),
                    {"t": table},
                )
            )

        for parent in tables():
            wanted = APP_EXCEPTIONS.get(parent, APP_PRIVILEGES)
            for table in [parent, *await children(parent)]:
                for privilege in APP_PRIVILEGES:
                    granted = await may(APP_ROLE, table, privilege)
                    assert granted is (privilege in wanted), f"{APP_ROLE} {privilege} on {table}"
                purge_wanted = PURGE_EXCEPTIONS.get(parent, PURGE_PRIVILEGES)
                for privilege in APP_PRIVILEGES:
                    granted = await may(PURGE_ROLE, table, privilege)
                    assert granted is (privilege in purge_wanted), (
                        f"{PURGE_ROLE} {privilege} on {table}"
                    )
                # Never, for either: TRUNCATE is a DELETE that fires no trigger and leaves
                # no row.
                for role in (APP_ROLE, PURGE_ROLE):
                    assert not await may(role, table, "TRUNCATE"), f"{role} TRUNCATE on {table}"


@pytest.mark.parametrize(("table", "trigger"), TRIGGERS)
async def test_each_trigger_is_attached_and_enabled(database, table, trigger):
    async with session_scope() as db:
        enabled = await db.scalar(
            text(
                "SELECT tgenabled::text FROM pg_trigger "
                "WHERE tgrelid = cast(:t AS regclass) AND tgname = :n"
            ),
            {"t": table, "n": trigger},
        )
    # 'O' — fires in the origin session, which is every session this application has.
    assert enabled == "O"


async def test_audit_events_no_rewrite_refuses_an_update_even_with_the_grant_restored(database):
    """The grant and the trigger fail differently (module docstring) — this proves the
    second mechanism on its own, not the grant doing all the work. As the schema owner, in
    one transaction: GRANT UPDATE back to `linsuite_app` (revoked by 0004), `SET ROLE` into
    it for the one statement, and roll the whole thing back — so the grant is real for that
    statement and never committed."""
    owner = create_async_engine(get_settings().database_url_migrate)
    try:
        async with owner.connect() as conn:
            await conn.begin()
            try:
                await conn.execute(text("GRANT UPDATE ON audit_events TO linsuite_app"))
                # A row to update: a `FOR EACH ROW` trigger never runs against zero rows, and
                # an empty table would make this test pass for the wrong reason.
                await conn.execute(
                    text(
                        "INSERT INTO audit_events (event_type, target_type) "
                        "VALUES ('probe', 'probe')"
                    )
                )
                await conn.execute(text("SET ROLE linsuite_app"))
                with pytest.raises(DBAPIError) as refused:
                    await conn.execute(text("UPDATE audit_events SET event_type = 'x'"))
                # 42501 insufficient_privilege, raised by audit_events_append_only() — the
                # grant just above is exactly what makes that the trigger's doing.
                assert getattr(refused.value.orig, "sqlstate", None) == "42501", refused.value
            finally:
                await conn.rollback()
    finally:
        await owner.dispose()

    # The rolled-back GRANT never happened as far as any other session is concerned.
    async with session_scope() as db:
        with pytest.raises(DBAPIError) as still_refused:
            await db.execute(text("UPDATE audit_events SET event_type = 'x'"))
        await db.rollback()
    assert getattr(still_refused.value.orig, "sqlstate", None) == "42501"


# Every trigger function, and every SECURITY DEFINER one, runs with a `search_path` it did not
# choose unless it pins one — and `pg_temp` is searched first for any relation it names bare.
# Both runtime roles hold TEMP, so an unpinned guard answers whatever a temp table tells it
# (0024, fix rounds 1 and 2). Self-discovering: a future migration's unpinned trigger fails
# here without anybody having to remember to list it.
#
# It does not follow the call graph. A plain (non-trigger, non-definer) helper that a trigger
# function calls is not in this sweep, and it runs with its *own* `search_path` setting — so a
# helper that names relations bare must pin its path itself (or qualify every name). Nothing
# here would notice if it did not.
_PINNABLE = """
SELECT DISTINCT p.proname, p.proconfig
  FROM pg_proc p
  JOIN pg_namespace n ON n.oid = p.pronamespace
 WHERE n.nspname = 'public'
   AND (p.prosecdef OR EXISTS (SELECT 1 FROM pg_trigger t WHERE t.tgfoid = p.oid))
"""


def _pinned_path(config: list[str] | None) -> list[str] | None:
    for entry in config or ():
        if entry.startswith("search_path="):
            return [part.strip() for part in entry.removeprefix("search_path=").split(",")]
    return None


async def test_every_trigger_and_security_definer_function_pins_its_search_path(database):
    async with session_scope() as db:
        found = {name: _pinned_path(config) for name, config in await db.execute(text(_PINNABLE))}

    # Not vacuously: the guards this suite knows about are all in the sweep.
    assert {
        "audit_events_append_only",
        "audit_access_log_append_only",
        "customer_document_keys_guard",
        "customer_record_guard",
        "form_template_versions_append_only",
        "form_links_guard",
        "appointments_enforce_staff_concurrency",
        "ensure_access_log_partitions",
    } <= set(found)
    unpinned = {
        name: path
        for name, path in found.items()
        if path is None or path[0] != "pg_catalog" or path[-1] != "pg_temp"
    }
    assert unpinned == {}, "pin `SET search_path = pg_catalog, ..., pg_temp` on these"


async def test_nobody_but_the_owner_may_create_in_public(database):
    """0025 revokes it from PUBLIC explicitly rather than relying on the PostgreSQL 15+
    default, which a database initialised another way would not have."""
    async with session_scope() as db:
        granted = await db.scalar(text("SELECT has_schema_privilege('public', 'public', 'CREATE')"))
    assert granted is False


async def test_neither_runtime_role_may_create_in_public(database):
    """`appointments_enforce_staff_concurrency` pins `pg_catalog, public, pg_temp` (its body
    names tables bare): `public` in the path is only safe while nobody but the owner can put
    anything there."""
    async with session_scope() as db:
        for role in (APP_ROLE, PURGE_ROLE):
            assert not await db.scalar(
                text("SELECT has_schema_privilege(:r, 'public', 'CREATE')"), {"r": role}
            ), role


async def test_a_temp_pg_class_cannot_make_the_app_role_the_owner_of_audit_events(database):
    """The owner check reads `pg_class`. Shadowed by a temp table that names `linsuite_app`
    the owner, an unpinned function would let the app through — with the grant restored (in a
    rolled-back owner transaction, as above) so the refusal can only be the trigger's."""
    owner = create_async_engine(get_settings().database_url_migrate)
    try:
        async with owner.connect() as conn:
            await conn.begin()
            try:
                await conn.execute(text("GRANT UPDATE ON audit_events TO linsuite_app"))
                await conn.execute(
                    text(
                        "INSERT INTO audit_events (event_type, target_type) "
                        "VALUES ('probe', 'probe')"
                    )
                )
                await conn.execute(text("SET ROLE linsuite_app"))
                await conn.execute(text("CREATE TEMP TABLE pg_class (oid oid, relowner oid)"))
                await conn.execute(
                    text(
                        "INSERT INTO pg_class SELECT c.oid, r.oid FROM pg_catalog.pg_class c, "
                        "pg_catalog.pg_roles r WHERE c.relname = 'audit_events' "
                        "AND r.rolname = 'linsuite_app'"
                    )
                )
                with pytest.raises(DBAPIError) as refused:
                    await conn.execute(text("UPDATE audit_events SET event_type = 'x'"))
                assert getattr(refused.value.orig, "sqlstate", None) == "42501", refused.value
                assert "append-only" in str(refused.value)
            finally:
                await conn.rollback()
    finally:
        await owner.dispose()


async def test_a_temp_pg_class_cannot_let_the_app_role_rewrite_the_access_log(database):
    """The same attack on `audit_access_log_append_only()`: a partitioned table, so the row is
    inserted through the parent and the UPDATE is tried through it too."""
    owner = create_async_engine(get_settings().database_url_migrate)
    try:
        async with owner.connect() as conn:
            await conn.begin()
            try:
                await conn.execute(text("GRANT UPDATE ON audit_access_log TO linsuite_app"))
                await conn.execute(
                    text(
                        "INSERT INTO audit_access_log (actor_user_id, actor_role, customer_id, "
                        "resource_type, resource_id, action) VALUES (gen_random_uuid(), "
                        "'Staff', gen_random_uuid(), 'probe', 'probe', 'view')"
                    )
                )
                await conn.execute(text("SET ROLE linsuite_app"))
                await conn.execute(text("CREATE TEMP TABLE pg_class (oid oid, relowner oid)"))
                await conn.execute(
                    text(
                        "INSERT INTO pg_class SELECT c.oid, r.oid FROM pg_catalog.pg_class c, "
                        "pg_catalog.pg_roles r WHERE c.relname LIKE 'audit_access_log%' "
                        "AND r.rolname = 'linsuite_app'"
                    )
                )
                with pytest.raises(DBAPIError) as refused:
                    await conn.execute(text("UPDATE audit_access_log SET action = 'x'"))
                assert getattr(refused.value.orig, "sqlstate", None) == "42501", refused.value
                assert "append-only" in str(refused.value)
            finally:
                await conn.rollback()
    finally:
        await owner.dispose()


# --- the purge role is reachable only from the purge tasks (Task 7, pre-flight D5a) ---------
#
# The request path must never gain the purge role's powers. The strongest form of that is that
# the web process never even builds a purge engine: only the Celery tasks do. So every source
# file that names the purge DSN or an engine built on it is listed here, with its reason.
PURGE_REACHERS = {
    "core/config.py": "declares the `DATABASE_URL_PURGE` setting",
    "core/db.py": "builds the purge engines",
    "core/celery_app.py": "the worker refuses to boot without the purge DSN",
    "customers/tasks.py": "the purge tasks — the one production caller",
}
PURGE_TOKENS = ("database_url_purge", "get_purge_engine", "get_task_engines", "purge_dsn")


def test_only_the_purge_tasks_reach_the_purge_role():
    found = {
        str(path.relative_to(SRC))
        for path in SRC.rglob("*.py")
        if any(token in path.read_text() for token in PURGE_TOKENS)
    }
    assert found == set(PURGE_REACHERS), f"purge role reached from {sorted(found)}"
    # And no module that mounts routes is among them.
    for relative in found:
        assert "APIRouter(" not in (SRC / relative).read_text(), relative


# --- the 403 invariant, self-discovering (fix wave, finding 20) ------------------------------
#
# `tests/test_rbac.py` walks the 403s the API actually emits and proves each names its kind.
# That test can only cover what somebody remembered to enumerate, and the invariant it
# protects — every 403 carries a `code` — is broken by *writing a new one*, not by exercising
# an old one. So: the source may build a 403 in exactly three places, and every one of them
# attaches a code.

SRC = Path(__file__).resolve().parent.parent / "src"
# `core/errors.py` is `Forbidden`, which every raised 403 goes through; `main.py` is its
# handler and the one 403 built by hand (the upload middleware runs outside the handler).
ALLOWED_403 = {"core/errors.py", "main.py"}


def test_nothing_else_in_the_source_builds_a_403():
    found = {
        str(path.relative_to(SRC))
        for path in SRC.rglob("*.py")
        if "status_code=403" in path.read_text()
    }
    assert found == ALLOWED_403, (
        f"a 403 outside {sorted(ALLOWED_403)}: {sorted(found - ALLOWED_403)} — raise "
        "core.errors.Forbidden(code, detail) instead, so the browser can tell the kinds apart"
    )
