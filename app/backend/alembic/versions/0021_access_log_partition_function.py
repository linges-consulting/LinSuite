"""`ensure_access_log_partitions()`: next year's access-log partition, made ahead of time.

Revision ID: 0021
Revises: 0020
Create Date: 2026-09-21

0019 made this year's and next year's partitions; nothing made the one after. The access log
is written fail-closed, so the first January without a partition is a January in which no
client profile opens (ADR-0002 §5, pre-flight D7).

`linsuite_app` cannot `CREATE TABLE`, and must not be able to. So the privilege lives in a
function owned by the schema owner, `SECURITY DEFINER`, which the app role may *execute* but
whose effect it cannot otherwise obtain. It is called at boot (`main.lifespan`) and nightly
by beat (`core.tasks.maintain_partitions`), and once here.

What keeps a definer function from being a hole:

* **No arguments.** The years are computed inside; the caller directs nothing, and the only
  dynamic SQL is a table name built from an integer, quoted with `%I`/`%L` regardless.
* **Pinned `search_path`** (`pg_catalog, pg_temp`) and every name schema-qualified, so a
  caller cannot shadow a function or table the body resolves.
* **`timezone` pinned to UTC**, so the year and the partition bounds do not depend on the
  calling session; 0019's bounds are UTC-qualified to match. `default_tablespace` and
  `default_table_access_method` are pinned too, so the caller cannot choose where or how
  the owner's table is stored.
* **Presence means attached** (`pg_inherits`), not a name: an unattached table of the same
  name is an error, never silently taken for the partition.
* **EXECUTE revoked from `PUBLIC`**, granted to `linsuite_app` only. Only the owner may
  `CREATE OR REPLACE` it.
* **An advisory transaction lock** around check-then-create: `app` booting while beat runs
  would otherwise both see the table missing and the second fail on a duplicate.

Each child gets its grants set explicitly rather than trusting 0001's default privileges
(which bind to whoever ran 0001): a REVOKE on the parent never reaches a later child. The
parent's row trigger is cloned onto every new partition by Postgres itself.

Runway: this year and next, checked nightly — next year's partition exists from the first
night of this year, so a stopped `beat` has a full year before it matters. No DEFAULT
partition, for 0019's reason.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FUNCTION = "public.ensure_access_log_partitions()"


def upgrade() -> None:
    op.execute(
        f"""
        CREATE FUNCTION {FUNCTION} RETURNS text[]
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
                -- Present means attached, not merely named: a detached partition or an
                -- ordinary table of the same name must not be skipped as if it were one.
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
    )
    op.execute(f"REVOKE ALL ON FUNCTION {FUNCTION} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {FUNCTION} TO linsuite_app")
    op.execute(f"SELECT {FUNCTION}")


def downgrade() -> None:
    # Partitions it made are data, and stay: 0019's downgrade takes them with the parent.
    op.execute(f"DROP FUNCTION IF EXISTS {FUNCTION}")
