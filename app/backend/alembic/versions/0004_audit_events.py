"""The append-only audit log (ADR-0002, CLAUDE.md "Retention, erasure, audit").

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-18

0001's ALTER DEFAULT PRIVILEGES hands `linsuite_app` full DML on every table the owner
creates, so this table has to take two of those privileges back explicitly. It is revoked
*and* trigger-blocked, because the two fail differently: a grant is undone by one careless
`GRANT ALL ON ALL TABLES`, and a trigger is undone by one `ALTER TABLE ... DISABLE
TRIGGER`. An audit log that can be quietly rewritten is not evidence of anything.

`linsuite_purge` keeps its DELETE: retention expiry is the one authority allowed to remove
history, and it does not connect as the app (ADR-0001).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "audit_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        # Not a foreign key: the trail outlives the accounts it describes, and ON DELETE
        # SET NULL would make deleting a user an UPDATE that the trigger below refuses.
        sa.Column("actor_user_id", sa.Uuid(), nullable=True),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("target_type", sa.String(64), nullable=False),
        sa.Column("target_id", sa.String(64), nullable=True),
        sa.Column(
            "metadata",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "occurred_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    # "What happened to this account" and "what happened in this window" are the two
    # questions an investigation actually asks.
    op.create_index("ix_audit_events_actor", "audit_events", ["actor_user_id", "occurred_at"])
    op.create_index("ix_audit_events_occurred_at", "audit_events", ["occurred_at"])

    op.execute("REVOKE UPDATE, DELETE ON audit_events FROM linsuite_app")

    op.execute(
        """
        CREATE OR REPLACE FUNCTION audit_events_append_only() RETURNS trigger AS $$
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
            RAISE EXCEPTION 'audit_events is append-only: % is not permitted for %',
                TG_OP, current_user
                USING ERRCODE = 'insufficient_privilege';
        END $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER audit_events_no_rewrite
        BEFORE UPDATE OR DELETE ON audit_events
        FOR EACH ROW EXECUTE FUNCTION audit_events_append_only();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS audit_events_no_rewrite ON audit_events")
    op.execute("DROP FUNCTION IF EXISTS audit_events_append_only()")
    op.drop_table("audit_events")
