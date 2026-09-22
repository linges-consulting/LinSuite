"""Partition automation for `audit_access_log` (ADR-0002 §5, pre-flight D7).

The access log is written fail-closed, so a year with no partition is a year in which no
client profile opens. `linsuite_app` cannot `CREATE TABLE`; `ensure_access_log_partitions()`
is a `SECURITY DEFINER` function it may call, and whose effect it cannot otherwise obtain.

**S5.** As the app role the function recreates a missing next-year partition carrying the
parent's grants (a REVOKE on the parent never reaches a later child) and its append-only
trigger; a second call and a concurrent caller create nothing and raise nothing; the role
still cannot create a table, rewrite the function, or reach it through `PUBLIC`.

**S1.** Booting the app (`main.lifespan`) puts a missing next year back, and refuses to start
only when *this* year's partition is still missing afterwards.

**Celery.** Beat schedules `core.tasks.maintain_partitions`, and the task run eagerly creates
a missing partition.
"""

import asyncio
import importlib.util
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from core.config import get_settings
from core.db import get_engine, session_scope
from tests.conftest import BACKEND_DIR

APP_ROLE, PURGE_ROLE = "linsuite_app", "linsuite_purge"
FUNCTION = "public.ensure_access_log_partitions()"
THIS_YEAR = datetime.now(UTC).year
NEXT = f"audit_access_log_{THIS_YEAR + 1}"
CURRENT = f"audit_access_log_{THIS_YEAR}"


@pytest.fixture
async def owner(database) -> AsyncIterator[AsyncEngine]:
    """The schema owner — what Alembic connects as, and what the function runs as."""
    engine = create_async_engine(get_settings().database_url_migrate)
    try:
        yield engine
    finally:
        # Whatever a test did, the stack is left with both partitions and EXECUTE granted.
        async with engine.begin() as conn:
            await conn.execute(text(f"GRANT EXECUTE ON FUNCTION {FUNCTION} TO {APP_ROLE}"))
            await conn.execute(text(f"SELECT {FUNCTION}"))
        await engine.dispose()


async def _drop_next_year(owner: AsyncEngine) -> None:
    async with owner.begin() as conn:
        await conn.execute(text(f"DROP TABLE {NEXT}"))


async def _exists(name: str) -> bool:
    async with session_scope() as db:
        return bool(
            await db.scalar(
                text(
                    "SELECT count(*) FROM pg_inherits "
                    "WHERE inhparent = 'public.audit_access_log'::regclass "
                    "AND inhrelid = to_regclass(:n)"
                ),
                # Qualified: a temp table of the same name on a pooled connection would
                # otherwise shadow it.
                {"n": f"public.{name}"},
            )
        )


async def _call_as_app() -> list[str]:
    async with session_scope() as db:
        created = await db.scalar(text(f"SELECT {FUNCTION}"))
        await db.commit()
    return list(created)


# --- S5: the function --------------------------------------------------------------------


async def test_the_app_role_recreates_next_year_with_the_parents_grants_and_trigger(owner):
    await _drop_next_year(owner)
    assert not await _exists(NEXT)

    assert await _call_as_app() == [NEXT]

    assert await _exists(NEXT)
    async with session_scope() as db:

        async def may(role: str, privilege: str) -> bool:
            return await db.scalar(
                text("SELECT has_table_privilege(:r, :t, :p)"),
                {"r": role, "t": NEXT, "p": privilege},
            )

        # Exactly the parent's: the app appends and reads, the purge role reads and deletes.
        for privilege, app, purge in (
            ("SELECT", True, True),
            ("INSERT", True, False),
            ("UPDATE", False, False),
            ("DELETE", False, True),
            ("TRUNCATE", False, False),
        ):
            assert await may(APP_ROLE, privilege) is app, f"{APP_ROLE} {privilege}"
            assert await may(PURGE_ROLE, privilege) is purge, f"{PURGE_ROLE} {privilege}"

        # The parent's row trigger is cloned onto the new child, and firing.
        enabled = await db.scalar(
            text(
                "SELECT tgenabled::text FROM pg_trigger "
                "WHERE tgrelid = to_regclass(:t) AND tgname = 'audit_access_log_no_rewrite'"
            ),
            {"t": NEXT},
        )
        assert enabled == "O"

        # And the denial is real, not just a catalog answer.
        with pytest.raises(DBAPIError) as refused:
            await db.execute(text(f"DELETE FROM {NEXT}"))
        assert getattr(refused.value.orig, "sqlstate", None) == "42501"
        await db.rollback()


async def test_a_second_call_creates_nothing_and_raises_nothing(owner):
    await _call_as_app()
    assert await _call_as_app() == []


async def test_two_concurrent_callers_create_the_partition_once(owner):
    """Boot of `app` and a `beat` run at the same instant. The first holds the lock with the
    new child uncommitted; the second must wait and then find it, not fail on a duplicate."""
    await _drop_next_year(owner)
    engine = get_engine()
    async with engine.connect() as first, engine.connect() as second:
        await first.begin()
        assert list(await first.scalar(text(f"SELECT {FUNCTION}"))) == [NEXT]

        await second.begin()
        waiting = asyncio.create_task(second.scalar(text(f"SELECT {FUNCTION}")))
        await asyncio.sleep(0.5)
        assert not waiting.done(), "the second caller should be waiting on the first"

        await first.commit()
        assert list(await waiting) == []
        await second.commit()
    assert await _exists(NEXT)


async def test_the_privilege_is_the_functions_and_not_the_roles(owner):
    async with session_scope() as db:
        with pytest.raises(DBAPIError) as refused:
            await db.execute(text("CREATE TABLE public.probe (id int)"))
        assert getattr(refused.value.orig, "sqlstate", None) == "42501"
        await db.rollback()

        # Rewriting the body would be running arbitrary SQL as the owner: only the owner may.
        with pytest.raises(DBAPIError) as replaced:
            await db.execute(
                text(
                    f"CREATE OR REPLACE FUNCTION {FUNCTION} RETURNS text[] "
                    "LANGUAGE sql AS $$ SELECT '{}'::text[] $$"
                )
            )
        assert getattr(replaced.value.orig, "sqlstate", None) == "42501"
        await db.rollback()

        facts = (
            await db.execute(
                text(
                    "SELECT p.prosecdef, p.pronargs, p.proconfig, "
                    "       pg_get_userbyid(p.proowner) = pg_get_userbyid(c.relowner), "
                    "       has_function_privilege(:app, p.oid, 'EXECUTE'), "
                    "       has_function_privilege(:purge, p.oid, 'EXECUTE'), "
                    "       EXISTS (SELECT 1 FROM aclexplode(p.proacl) a "
                    "               WHERE a.grantee = 0 AND a.privilege_type = 'EXECUTE') "
                    "FROM pg_proc p, pg_class c "
                    "WHERE p.oid = to_regprocedure(:f) AND c.oid = 'audit_access_log'::regclass"
                ),
                {"app": APP_ROLE, "purge": PURGE_ROLE, "f": FUNCTION},
            )
        ).one()
    definer, nargs, config, owned_by_schema_owner, app, purge, public = facts
    assert definer and nargs == 0 and owned_by_schema_owner
    # A definer function resolving names through a caller-controlled path is the classic hole.
    assert any(setting.startswith("search_path=") for setting in config), config
    # Nor may the caller's session choose where, or in what format, the owner's table lands.
    assert "default_tablespace=" in config and "default_table_access_method=heap" in config
    assert (app, purge, public) == (True, False, False)


# --- S1: boot ----------------------------------------------------------------------------


async def test_booting_the_app_puts_a_missing_next_year_back(owner):
    from main import app, lifespan

    await _drop_next_year(owner)
    async with lifespan(app):
        assert await _exists(NEXT)


async def test_boot_logs_and_starts_when_only_next_year_could_not_be_made(owner, capsys):
    from main import app, lifespan

    await _drop_next_year(owner)
    async with owner.begin() as conn:
        await conn.execute(text(f"REVOKE EXECUTE ON FUNCTION {FUNCTION} FROM {APP_ROLE}"))

    async with lifespan(app):
        assert not await _exists(NEXT)
    # `lifespan` installs the JSON stdout handler itself, so the error is read where it lands.
    assert "could not ensure audit_access_log partitions" in capsys.readouterr().out


async def test_a_same_named_table_that_is_not_a_partition_is_not_this_years_partition(owner):
    """Presence means *attached*, not a name: a detached partition or an impostor in `public`
    must neither be skipped by the function nor satisfy the boot check."""
    from main import app, lifespan

    async with owner.begin() as conn:
        await conn.execute(text(f"ALTER TABLE audit_access_log DETACH PARTITION {CURRENT}"))
        await conn.execute(text(f"ALTER TABLE {CURRENT} RENAME TO {CURRENT}_aside"))
        await conn.execute(text(f"CREATE TABLE public.{CURRENT} (id int)"))
    try:
        with pytest.raises(DBAPIError, match="not a partition"):
            await _call_as_app()
        with pytest.raises(RuntimeError, match=CURRENT):
            async with lifespan(app):
                pass
    finally:
        async with owner.begin() as conn:
            await conn.execute(text(f"DROP TABLE public.{CURRENT}"))
            await conn.execute(text(f"ALTER TABLE {CURRENT}_aside RENAME TO {CURRENT}"))
            await conn.execute(
                text(
                    f"ALTER TABLE audit_access_log ATTACH PARTITION {CURRENT} "
                    f"FOR VALUES FROM ('{THIS_YEAR}-01-01 UTC') TO ('{THIS_YEAR + 1}-01-01 UTC')"
                )
            )


# --- Bounds: UTC whatever the session's zone ---------------------------------------------


def _migration_0019():
    path = f"{BACKEND_DIR}/alembic/versions/0019_audit_access_log.py"
    spec = importlib.util.spec_from_file_location("migration_0019", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _utc_bound(year: int) -> str:
    return f"FOR VALUES FROM ('{year}-01-01 00:00:00+00') TO ('{year + 1}-01-01 00:00:00+00')"


@pytest.mark.parametrize("zone", ["America/Vancouver", "Asia/Kolkata"])
async def test_0019_and_the_function_agree_on_utc_bounds_in_any_session_zone(owner, zone):
    """0019 ran in the migrating session's zone; the function pins UTC. On a non-UTC server a
    zone-less bound makes them overlap (negative offset: next year is never created) or leave
    a gap (positive: profile opens fail around New Year). All in one rolled-back transaction."""
    migration = _migration_0019()
    async with owner.connect() as conn:
        await conn.begin()
        try:
            await conn.execute(text(f"DROP TABLE {CURRENT}, {NEXT}"))
            await conn.execute(text(f"SET LOCAL TIME ZONE '{zone}'"))
            for statement in migration.partition_ddl(THIS_YEAR):
                await conn.execute(text(statement))
            assert list(await conn.scalar(text(f"SELECT {FUNCTION}"))) == [NEXT]

            # `pg_get_expr` renders in the session zone: read the bounds back in UTC.
            await conn.execute(text("SET LOCAL TIME ZONE 'UTC'"))
            bounds = dict(
                (
                    await conn.execute(
                        text(
                            "SELECT c.relname, pg_get_expr(c.relpartbound, c.oid) "
                            "FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid "
                            "WHERE i.inhparent = 'public.audit_access_log'::regclass"
                        )
                    )
                ).all()
            )
        finally:
            await conn.rollback()
    assert bounds == {CURRENT: _utc_bound(THIS_YEAR), NEXT: _utc_bound(THIS_YEAR + 1)}


# --- Celery ------------------------------------------------------------------------------


def test_beat_schedules_maintain_partitions_nightly():
    from core.celery_app import celery_app

    tasks = {entry["task"] for entry in celery_app.conf.beat_schedule.values()}
    assert "core.tasks.maintain_partitions" in tasks
    assert celery_app.conf.timezone == "UTC"


async def test_the_task_run_eagerly_creates_a_missing_partition(owner):
    from core.tasks import maintain_partitions

    await _drop_next_year(owner)
    # The task owns its own event loop (`asyncio.run`), as it does in the worker.
    result = await asyncio.to_thread(maintain_partitions.delay)
    assert result.get() == [NEXT]
    assert await _exists(NEXT)
