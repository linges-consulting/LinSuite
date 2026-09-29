"""New `linsuite_backup` role: SELECT only, everywhere, present and future (M5, #89/#82).

Revision ID: 0070
Revises: 0066
Create Date: 2026-09-29

`linsuite_backup` is a login role that can read every table and sequence and nothing else —
no INSERT, UPDATE, DELETE or TRUNCATE anywhere, enforced the same way 0001 enforces the app
and purge roles' shapes: by grant, not by application code. It is what the `backup` compose
service's `pg_dump` connects as (tech-stack §10): a stolen backup DSN can read (and therefore
back up) the whole database, but can never alter or delete anything in it.

Existing tables/sequences get an explicit `GRANT SELECT` here; `ALTER DEFAULT PRIVILEGES`
covers every table a later migration creates, so nobody has to remember this role when adding
one — the same shape 0001 already uses for the app and purge roles.

`ensure_access_log_partitions()` (0021) is the one exception: it sets each new
`audit_access_log` yearly partition's grants explicitly rather than trusting default
privileges (its own docstring explains why — a REVOKE on the parent does not reach a child
created afterwards), so this migration re-`CREATE OR REPLACE`s it, adding exactly one
`GRANT SELECT ... TO linsuite_backup` line. Nothing else in the function body changes.

NOTE FOR THE INTEGRATOR (#83, purge-role default-deny): that ticket may also
`CREATE OR REPLACE` this same function, to stop granting DELETE to `linsuite_purge` on new
partitions. Both changes are additive one-liners inside the same `FOR y IN ...` loop body —
merge by keeping both `EXECUTE format('GRANT ...')` lines, dropping whichever one of us
duplicated the unchanged parts of the function.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0070"
down_revision: str | None = "0069"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ROLE = "linsuite_backup"
FUNCTION = "public.ensure_access_log_partitions()"

# 0021's body, verbatim, plus the one added GRANT line (marked below). Kept whole rather than
# diffed because `CREATE OR REPLACE FUNCTION` always replaces the entire body.
_PARTITION_FUNCTION = f"""
CREATE OR REPLACE FUNCTION {FUNCTION} RETURNS text[]
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET timezone = 'UTC'
SET default_tablespace = ''
SET default_table_access_method = heap
AS $$
DECLARE
    this_year int := extract(year FROM now())::int;
    y int;
    child text;
    created text[] := '{{}}';
BEGIN
    PERFORM pg_advisory_xact_lock(hashtext('public.ensure_access_log_partitions'));
    FOR y IN this_year .. this_year + 1 LOOP
        child := 'audit_access_log_' || y;
        CONTINUE WHEN EXISTS (
            SELECT 1 FROM pg_inherits
            WHERE inhparent = 'public.audit_access_log'::regclass
              AND inhrelid = to_regclass('public.' || child)
        );
        IF to_regclass('public.' || child) IS NOT NULL THEN
            RAISE EXCEPTION 'public.% exists but is not a partition of audit_access_log',
                child USING ERRCODE = 'duplicate_table';
        END IF;
        EXECUTE format(
            'CREATE TABLE public.%I PARTITION OF public.audit_access_log '
            'FOR VALUES FROM (%L) TO (%L)',
            child, y || '-01-01', (y + 1) || '-01-01'
        );
        EXECUTE format(
            'REVOKE ALL ON public.%I FROM PUBLIC, linsuite_app, linsuite_purge, linsuite_backup',
            child
        );
        EXECUTE format('GRANT SELECT, INSERT ON public.%I TO linsuite_app', child);
        EXECUTE format('GRANT SELECT, DELETE ON public.%I TO linsuite_purge', child);
        -- 0070: the backup role reads every partition, including ones made after it existed.
        EXECUTE format('GRANT SELECT ON public.%I TO linsuite_backup', child);
        created := created || child;
    END LOOP;
    RETURN created;
END $$;
"""

# 0021's original body, verbatim, for downgrade — identical except it never mentions
# linsuite_backup. Kept as its own literal (not derived from the string above) so this file
# has no string-surgery to get wrong.
_PARTITION_FUNCTION_PRE_0070 = f"""
CREATE OR REPLACE FUNCTION {FUNCTION} RETURNS text[]
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET timezone = 'UTC'
SET default_tablespace = ''
SET default_table_access_method = heap
AS $$
DECLARE
    this_year int := extract(year FROM now())::int;
    y int;
    child text;
    created text[] := '{{}}';
BEGIN
    PERFORM pg_advisory_xact_lock(hashtext('public.ensure_access_log_partitions'));
    FOR y IN this_year .. this_year + 1 LOOP
        child := 'audit_access_log_' || y;
        CONTINUE WHEN EXISTS (
            SELECT 1 FROM pg_inherits
            WHERE inhparent = 'public.audit_access_log'::regclass
              AND inhrelid = to_regclass('public.' || child)
        );
        IF to_regclass('public.' || child) IS NOT NULL THEN
            RAISE EXCEPTION 'public.% exists but is not a partition of audit_access_log',
                child USING ERRCODE = 'duplicate_table';
        END IF;
        EXECUTE format(
            'CREATE TABLE public.%I PARTITION OF public.audit_access_log '
            'FOR VALUES FROM (%L) TO (%L)',
            child, y || '-01-01', (y + 1) || '-01-01'
        );
        EXECUTE format(
            'REVOKE ALL ON public.%I FROM PUBLIC, linsuite_app, linsuite_purge', child
        );
        EXECUTE format('GRANT SELECT, INSERT ON public.%I TO linsuite_app', child);
        EXECUTE format('GRANT SELECT, DELETE ON public.%I TO linsuite_purge', child);
        created := created || child;
    END LOOP;
    RETURN created;
END $$;
"""


def upgrade() -> None:
    op.execute(
        f"""
        DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{ROLE}') THEN
                CREATE ROLE {ROLE} LOGIN;
            END IF;
        END $$;
        """
    )
    op.execute(f"GRANT CONNECT ON DATABASE {_current_database()} TO {ROLE}")
    op.execute(f"GRANT USAGE ON SCHEMA public TO {ROLE}")

    # Existing objects: everything the owner has created so far, including partitions of
    # audit_access_log (they are ordinary tables in `public`, so this reaches them too).
    op.execute(f"GRANT SELECT ON ALL TABLES IN SCHEMA public TO {ROLE}")
    op.execute(f"GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO {ROLE}")

    # Everything the owner creates from here on.
    op.execute(f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO {ROLE}")
    op.execute(f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON SEQUENCES TO {ROLE}")

    op.execute(_PARTITION_FUNCTION)


def downgrade() -> None:
    op.execute(_PARTITION_FUNCTION_PRE_0070)

    op.execute(f"ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE SELECT ON TABLES FROM {ROLE}")
    op.execute(f"ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE SELECT ON SEQUENCES FROM {ROLE}")
    op.execute(f"REVOKE SELECT ON ALL TABLES IN SCHEMA public FROM {ROLE}")
    op.execute(f"REVOKE SELECT ON ALL SEQUENCES IN SCHEMA public FROM {ROLE}")
    op.execute(f"REVOKE ALL ON SCHEMA public FROM {ROLE}")
    op.execute(f"REVOKE CONNECT ON DATABASE {_current_database()} FROM {ROLE}")
    # The role itself is kept, same as 0001 — dropping a login role is an operator decision.


def _current_database() -> str:
    return op.get_bind().execute(sa.text("SELECT current_database()")).scalar_one()
