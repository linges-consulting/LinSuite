"""Notification failures: a terminal delivery failure against a customer (Phase 12 Task 4, #11).

Revision ID: 0035
Revises: 0034
Create Date: 2026-09-27

Written only for a `PermanentDeliveryError` — a 4xx from Resend/Twilio, a bad SMTP recipient
or a rejected SMTP login — never for a transient one, which just keeps retrying under
`notifications/tasks.py`'s existing backoff. Ordinary app-role DML (no append-only trigger,
no grant exception): a failure row is diagnostic, not a compliance record, and cascades away
with the customer it describes.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0035"
down_revision: str | None = "0034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "notification_failures",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "customer_id",
            sa.Uuid(),
            sa.ForeignKey("customers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("notification_type", sa.Text(), nullable=False),
        sa.Column("recipient", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "occurred_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("channel IN ('email', 'sms')", name="ck_notification_failures_channel"),
        sa.CheckConstraint(
            "notification_type IN ('booking_confirmation', 'reminder', 'modification', "
            "'cancellation', 'form_link', 'package_notice')",
            name="ck_notification_failures_type",
        ),
    )
    op.create_index(
        "ix_notification_failures_customer_id", "notification_failures", ["customer_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_notification_failures_customer_id", table_name="notification_failures")
    op.drop_table("notification_failures")
