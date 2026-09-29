"""Bill review authority (#64): `bill_override_requests` + two business-setting toggles.

Revision ID: 0052
Revises: 0051
Create Date: 2026-09-28

Picked 0052 as the next free number after `0051_service_bill_discounts.py` (#63, already
merged into `feat/m4-billing-inventory` at the commit this worktree branched from). #62
(low-stock alerts) is a sibling M4 Wave 3 ticket running concurrently in its own worktree and
may also claim a number near here — this file does not coordinate with it directly, per the
orchestrator's own collision-handling note (m4.md): whichever of #62/#64 merges second gets
renumbered at integration.

`bill_override_requests`: staff-request review path (a). See `billing/models.py::
BillOverrideRequest`'s own docstring for the full design — the unified "ad hoc discount or
price override is one absolute total" shape, and the stale-approval guard
(`bill_revision_as_of` pinning `ServiceBill.updated_at`). Ordinary app-role DML, grants
inherited from 0001's `ALTER DEFAULT PRIVILEGES` — operational, not yet a compliance record,
the same call `0049_service_bills.py`/`0051_service_bill_discounts.py` already made; nothing
about a draft bill is immutable until #65 issues it.

`ServiceBill.manual_override_cents`/`manual_override_reason`: where an approved staff request
(a) or a direct inline admin edit (b, no table of its own — `billing/bill_authority.py`'s
Redis-held short window) lands. Nullable, no CHECK on sign since it is a final tax-inclusive
total, not a per-line discount — negative is meaningless in practice but not this migration's
job to refuse; `bill_authority.py`'s own request-model validation (`ge=0`) is what actually
stops one.

Two business toggles, following `enable_walk_in_queue`'s exact shape (0040) except both
default **true**: `enable_bill_override_requests` gates *new* staff-request submissions,
`enable_inline_admin_bill_edit` gates the inline-authentication endpoint. Neither is checked
by `bill_review.py`'s existing #63 routes or by the decision endpoint on an already-pending
request — an admin/owner's own ordinary billing access, and existing request history, are
never removed by turning a staff-facing convenience path off (#54's "Override configuration"
implementation decision, verbatim).

No new capability. `billing.view` (0051) already gates the staff-facing submit/list routes;
`billing.manage` (0044, Administrator-only, `requires_admin_mode`) already gates review/
decision and is what an inline-authenticating account must hold — reused exactly as `billing.
manage`'s own docstring reserved it, rather than a near-duplicate key.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0052"
down_revision: str | None = "0051"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "businesses",
        sa.Column(
            "enable_bill_override_requests",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
    )
    op.add_column(
        "businesses",
        sa.Column(
            "enable_inline_admin_bill_edit",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
    )

    op.add_column("service_bills", sa.Column("manual_override_cents", sa.Integer(), nullable=True))
    op.add_column("service_bills", sa.Column("manual_override_reason", sa.Text(), nullable=True))

    op.create_table(
        "bill_override_requests",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "bill_id",
            sa.Uuid(),
            sa.ForeignKey("service_bills.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "requested_by",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("requested_total_cents", sa.Integer(), nullable=False),
        sa.Column("bill_revision_as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "requested_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("status", sa.String(16), server_default=sa.text("'pending'"), nullable=False),
        sa.Column(
            "decided_by", sa.Uuid(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
        ),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_note", sa.Text(), nullable=True),
        sa.Column("decided_total_cents", sa.Integer(), nullable=True),
        sa.CheckConstraint(
            "kind IN ('discount', 'price_override')", name="ck_bill_override_requests_kind"
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected')",
            name="ck_bill_override_requests_status",
        ),
        sa.CheckConstraint(
            "requested_total_cents >= 0", name="ck_bill_override_requests_requested_total"
        ),
        sa.CheckConstraint(
            "decided_total_cents IS NULL OR decided_total_cents >= 0",
            name="ck_bill_override_requests_decided_total",
        ),
    )
    op.create_index(
        "ix_bill_override_requests_bill_status", "bill_override_requests", ["bill_id", "status"]
    )


def downgrade() -> None:
    op.drop_index("ix_bill_override_requests_bill_status", table_name="bill_override_requests")
    op.drop_table("bill_override_requests")
    op.drop_column("service_bills", "manual_override_reason")
    op.drop_column("service_bills", "manual_override_cents")
    op.drop_column("businesses", "enable_inline_admin_bill_edit")
    op.drop_column("businesses", "enable_bill_override_requests")
