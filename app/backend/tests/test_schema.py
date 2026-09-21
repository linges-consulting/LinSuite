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
express, and `audit_access_log_no_rewrite` is the second half of append-only (a grant is
undone by one careless `GRANT ALL`, a trigger by one `DISABLE TRIGGER`); nothing but this
says either is still attached and still enabled.
"""

from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from core.config import get_settings
from core.db import Base, session_scope

# `linsuite_app` writes everywhere except the append-only logs; `linsuite_purge` is the
# privileged role the retention-expiry job runs as, and only ever reads and deletes.
APP_ROLE, PURGE_ROLE = "linsuite_app", "linsuite_purge"
APP_PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE")
# Per-table departures from the default. Extend this, never bypass the test.
APP_EXCEPTIONS: dict[str, tuple[str, ...]] = {
    "audit_events": ("SELECT", "INSERT"),
    "audit_access_log": ("SELECT", "INSERT"),
}
PURGE_PRIVILEGES = ("SELECT", "DELETE")
# (table, trigger) pairs that must be attached and firing.
TRIGGERS = (
    ("appointments", "tg_appointments_staff_concurrency"),
    ("audit_events", "audit_events_no_rewrite"),
    ("audit_access_log", "audit_access_log_no_rewrite"),
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
                for privilege in PURGE_PRIVILEGES:
                    assert await may(PURGE_ROLE, table, privilege), (
                        f"{PURGE_ROLE} {privilege} {table}"
                    )
                # Today, on every table: the purge role only ever reads and deletes. (Task 6
                # amends this to "INSERT only on audit_events", once erasure needs to write
                # the fact of its own purge from inside the purge transaction.)
                for privilege in ("INSERT", "UPDATE"):
                    assert not await may(PURGE_ROLE, table, privilege), (
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
