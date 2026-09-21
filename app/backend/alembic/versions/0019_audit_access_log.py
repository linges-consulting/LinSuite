"""The PHI access log: who opened whose record (ADR-0002 §1, §5).

Revision ID: 0019
Revises: 0018
Create Date: 2026-09-21

A second audit table, not more columns on `audit_events`: that one is the write/auth trail,
this one records *reads* — a profile opened, later a note or a form PDF — and is the
highest-volume table in the system, so it is `PARTITION BY RANGE (occurred_at)` yearly from
the first row. Postgres requires the partition key in the primary key, hence `(id,
occurred_at)`; and on 16 a partitioned table cannot carry an identity column, so `id` draws
from an explicit sequence instead.

Partitions for this year and next are created here, so a fresh install is never caught by
January. Keeping the year after that in existence is Task 5's job (a nightly job and a
`SECURITY DEFINER` function, since `linsuite_app` cannot `CREATE TABLE`). No DEFAULT
partition: it would make a missed year silent, and the real partition could then never be
created over the rows the default already holds.

Append-only for the application role by grant *and* by trigger, exactly as `audit_events`:
the two are undone by different mistakes. 0001's default privileges reach each partition as
it is created, so the REVOKE is repeated on every child — a REVOKE on the parent alone
would leave a later partition writable. `linsuite_purge` keeps its DELETE: the access log
inherits the ten-year retention horizon and its expiry is the purge role's job (ADR-0001).
"""

from collections.abc import Sequence
from datetime import date

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "audit_access_log"
SEQUENCE = f"{TABLE}_id_seq"


def _create_partition(year: int) -> None:
    op.execute(
        f"CREATE TABLE {TABLE}_{year} PARTITION OF {TABLE} "
        f"FOR VALUES FROM ('{year}-01-01') TO ('{year + 1}-01-01')"
    )
    op.execute(f"REVOKE UPDATE, DELETE ON {TABLE}_{year} FROM linsuite_app")


def upgrade() -> None:
    op.execute(f"CREATE SEQUENCE {SEQUENCE} AS bigint")
    op.create_table(
        TABLE,
        sa.Column(
            "id",
            sa.BigInteger(),
            server_default=sa.text(f"nextval('{SEQUENCE}')"),
            nullable=False,
        ),
        sa.Column(
            "occurred_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        # No foreign keys, for the reason 0004 gives: the trail outlives the accounts and —
        # after a purge — the customers it describes, and a SET NULL would be an UPDATE the
        # trigger refuses.
        sa.Column("actor_user_id", sa.Uuid(), nullable=False),
        # The role's *name* at the moment of access. Roles rename; the log must not.
        sa.Column("actor_role", sa.String(64), nullable=False),
        sa.Column("customer_id", sa.Uuid(), nullable=False),
        sa.Column("resource_type", sa.String(64), nullable=False),
        sa.Column("resource_id", sa.String(64), nullable=False),
        sa.Column("action", sa.String(16), nullable=False),
        # Nullable: the ASGI test transport has no client address, and that is fine.
        sa.Column("ip", postgresql.INET(), nullable=True),
        sa.PrimaryKeyConstraint("id", "occurred_at"),
        postgresql_partition_by="RANGE (occurred_at)",
    )
    op.execute(f"ALTER SEQUENCE {SEQUENCE} OWNED BY {TABLE}.id")
    # "Who opened this client's record" and "what did this account open": the two questions
    # the report and an investigation ask (ADR-0002 §6).
    op.create_index(f"ix_{TABLE}_customer", TABLE, ["customer_id", "occurred_at"])
    op.create_index(f"ix_{TABLE}_actor", TABLE, ["actor_user_id", "occurred_at"])
    op.execute(f"REVOKE UPDATE, DELETE ON {TABLE} FROM linsuite_app")

    year = date.today().year
    _create_partition(year)
    _create_partition(year + 1)

    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION {TABLE}_append_only() RETURNS trigger AS $$
        BEGIN
            -- The purge role runs retention expiry and is the only authority allowed to
            -- remove history (ADR-0001). The schema owner is let through so migrations and
            -- an operator's recovery are not blocked; the application never connects as it.
            IF current_user = 'linsuite_purge'
               OR current_user = (
                   SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid = TG_RELID
               ) THEN
                RETURN CASE TG_OP WHEN 'DELETE' THEN OLD ELSE NEW END;
            END IF;
            RAISE EXCEPTION '{TABLE} is append-only: % is not permitted for %',
                TG_OP, current_user
                USING ERRCODE = 'insufficient_privilege';
        END $$ LANGUAGE plpgsql;
        """
    )
    # A row trigger on the parent is cloned onto every partition, present and future.
    op.execute(
        f"""
        CREATE TRIGGER {TABLE}_no_rewrite
        BEFORE UPDATE OR DELETE ON {TABLE}
        FOR EACH ROW EXECUTE FUNCTION {TABLE}_append_only();
        """
    )


def downgrade() -> None:
    op.execute(f"DROP TRIGGER IF EXISTS {TABLE}_no_rewrite ON {TABLE}")
    op.execute(f"DROP FUNCTION IF EXISTS {TABLE}_append_only()")
    # Partitions go with the parent; the sequence is owned by the column and goes with it too.
    op.drop_table(TABLE)
