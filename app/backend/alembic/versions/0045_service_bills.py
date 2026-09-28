"""`service_bills` + `service_bill_lines` (M4 #59: draft service bill on completion).

Revision ID: 0045
Revises: 0043
Create Date: 2026-09-28

Grants are inherited from 0001's `ALTER DEFAULT PRIVILEGES`, the same as `queue_entries`
(0040): a draft bill is ordinary app-role DML, not yet a compliance record — it only becomes
one once #65 issues it, and that ticket is what adds the immutability grants+trigger pair
(CLAUDE.md "Document storage and immutability") to whatever it lands on, following the
document/audit-log precedent. Nothing here is immutable yet.

Numbered 0045, not 0044: the M4 progress ledger (`.superpowers/sdd/m4/progress.md`) already
records #60's worktree having claimed 0044 (unmerged as of this writing) — picking the next
free number *visible right now* rather than reusing a known-taken one. Other Wave 1 agents
working blind to this ledger entry may still collide; per `m4.md`'s parallel-merge hazard
note, that's the orchestrator's renumbering job at merge time, not this ticket's.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0045"
down_revision: str | None = "0043"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "service_bills",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("customer_id", sa.Uuid(), sa.ForeignKey("customers.id"), nullable=False),
        sa.Column("booking_group_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(16), server_default=sa.text("'draft'"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("status IN ('draft', 'issued')", name="ck_service_bills_status"),
    )
    op.create_index(
        "ix_service_bills_group_status", "service_bills", ["booking_group_id", "status"]
    )
    op.create_table(
        "service_bill_lines",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "bill_id",
            sa.Uuid(),
            sa.ForeignKey("service_bills.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "appointment_id",
            sa.Uuid(),
            sa.ForeignKey("appointments.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("service_id", sa.Uuid(), sa.ForeignKey("services.id"), nullable=False),
        sa.Column("staff_id", sa.Uuid(), sa.ForeignKey("staff.id"), nullable=False),
        sa.Column("price_cents", sa.Integer(), nullable=False),
        sa.Column("commission_rate_bp", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("price_cents >= 0", name="ck_service_bill_lines_price"),
        sa.CheckConstraint(
            "commission_rate_bp BETWEEN 0 AND 10000", name="ck_service_bill_lines_commission_bp"
        ),
    )


def downgrade() -> None:
    op.drop_table("service_bill_lines")
    op.drop_index("ix_service_bills_group_status", table_name="service_bills")
    op.drop_table("service_bills")
