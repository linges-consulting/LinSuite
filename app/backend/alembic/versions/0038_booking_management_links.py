"""Booking-management links + cancellation policy on `businesses` (Phase 6 Task 3, #10).

Revision ID: 0038
Revises: 0037
Create Date: 2026-09-27

`booking_management_links`: mirrors `form_links`'s shape (only `sha256(token)` ever stored)
except it is **not single-use** — no `consumed_at`, no `revoked_at`, no guard trigger. A
client reopens the same link to view, then maybe reschedule, then maybe cancel; the
appointment it points at is the only clock this link has (dead once that row is no longer
`confirmed` or has already started — `scheduling/public.py`'s lookup, not a stored
`expires_at`). `ON DELETE CASCADE`, the same as `appointment_resources`: the link is nothing
without the appointment.

Two new columns on `businesses`, both read at the API in `scheduling/public.py`'s cancel and
reschedule handlers, never only hidden behind a UI toggle (#10's own acceptance criterion) —
Task 4 builds the settings-panel controls that edit them, this task only needed them to exist:
`online_cancellation_enabled` (default true) and `cancellation_cutoff_hours` (default 24).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0038"
down_revision: str | None = "0037"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "businesses",
        sa.Column(
            "online_cancellation_enabled",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
    )
    op.add_column(
        "businesses",
        sa.Column(
            "cancellation_cutoff_hours", sa.Integer(), server_default=sa.text("24"), nullable=False
        ),
    )
    op.create_check_constraint(
        "ck_businesses_cancellation_cutoff_hours", "businesses", "cancellation_cutoff_hours >= 0"
    )
    op.create_table(
        "booking_management_links",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("token_sha256", sa.LargeBinary(), nullable=False, unique=True),
        sa.Column(
            "appointment_id",
            sa.Uuid(),
            sa.ForeignKey("appointments.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "issued_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "octet_length(token_sha256) = 32", name="ck_booking_management_links_sha256"
        ),
    )


def downgrade() -> None:
    op.drop_table("booking_management_links")
    op.drop_constraint("ck_businesses_cancellation_cutoff_hours", "businesses", type_="check")
    op.drop_column("businesses", "cancellation_cutoff_hours")
    op.drop_column("businesses", "online_cancellation_enabled")
