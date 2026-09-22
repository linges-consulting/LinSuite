"""Retention profile and computed retention expiry (Task 4, #39; ADR-0001).

Revision ID: 0023
Revises: 0022
Create Date: 2026-09-21

`businesses.retention_profile` — CHECK in (`regulated_health`, `general_business`), default
`regulated_health` (the retain side is the recoverable mistake) — and
`retention_profile_set_at`, NULL until an administrator has saved a choice, which is what the
Security panel's banner reads.

`customers.last_clinical_entry_at` and `customers.retention_expires_at`, both nullable
`timestamptz`, the latter indexed for the purge job's date scan. No backfill: no clinical
entries exist before Phases 8/9, and with no entry the rule's answer is NULL (not held).

Grants are inherited from 0001's `ALTER DEFAULT PRIVILEGES`; nothing is repeated here.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "businesses",
        sa.Column(
            "retention_profile",
            sa.String(32),
            server_default=sa.text("'regulated_health'"),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "ck_businesses_retention_profile",
        "businesses",
        "retention_profile IN ('regulated_health', 'general_business')",
    )
    op.add_column(
        "businesses",
        sa.Column("retention_profile_set_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.add_column(
        "customers", sa.Column("last_clinical_entry_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "customers", sa.Column("retention_expires_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_index("ix_customers_retention_expires_at", "customers", ["retention_expires_at"])


def downgrade() -> None:
    op.drop_index("ix_customers_retention_expires_at", table_name="customers")
    op.drop_column("customers", "retention_expires_at")
    op.drop_column("customers", "last_clinical_entry_at")

    op.drop_column("businesses", "retention_profile_set_at")
    op.drop_constraint("ck_businesses_retention_profile", "businesses", type_="check")
    op.drop_column("businesses", "retention_profile")
