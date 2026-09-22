"""Erasure requests and suppression (Task 7, #42; ADR-0001 §3, pre-flight D5a/D5b).

Revision ID: 0025
Revises: 0024
Create Date: 2026-09-21

- `customers.suppressed_at`: set by an erasure request; lists, search and booking skip it.
- `erasure_requests`: the request and the record that it was honoured. The app role keeps
  SELECT, INSERT and UPDATE (it stamps `purged_at`) but loses DELETE — the evidence that a
  request was honoured is not the application's to remove. Nothing else changes for either
  role: the purge role reads and deletes as everywhere, and still updates nothing.
- The Administrator role holds the new `customers.erase` capability (as 0022 did for
  `audit.view`); the seeded Staff role does not.
- `REVOKE CREATE ON SCHEMA public FROM PUBLIC`, explicitly. PostgreSQL 15+ already ships
  that way, but the staff-concurrency trigger's pinned `pg_catalog, public, pg_temp` is only
  safe while nobody but the owner can create in `public`, so it is stated rather than
  inherited from whichever image the database was initialised by. The downgrade leaves it:
  un-hardening a schema is never what a rollback of this feature means.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("customers", sa.Column("suppressed_at", sa.DateTime(timezone=True)))
    op.create_table(
        "erasure_requests",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("customer_id", sa.Uuid(), sa.ForeignKey("customers.id"), nullable=False),
        sa.Column(
            "requested_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("requested_by_user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("note", sa.Text()),
        sa.Column("held_until", sa.DateTime(timezone=True)),
        sa.Column("held_reason", sa.Text()),
        sa.Column("purged_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_erasure_requests_customer_id", "erasure_requests", ["customer_id"])
    op.execute("REVOKE DELETE ON erasure_requests FROM linsuite_app")
    op.execute(
        "INSERT INTO role_capabilities (role_id, capability) "
        "SELECT id, 'customers.erase' FROM roles WHERE name = 'Administrator' AND is_system "
        "ON CONFLICT DO NOTHING"
    )
    op.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")


def downgrade() -> None:
    op.execute("DELETE FROM role_capabilities WHERE capability = 'customers.erase'")
    op.drop_table("erasure_requests")
    op.drop_column("customers", "suppressed_at")
