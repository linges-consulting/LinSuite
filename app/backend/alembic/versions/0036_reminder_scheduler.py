"""Reminder scheduler: intervals on `businesses` + the send-guard table (Phase 12 Task 5, #11).

Revision ID: 0036
Revises: 0035
Create Date: 2026-09-27

`reminder_intervals_hours` (JSONB, like `Appointment.overridden_rules` and
`AuditEvent.event_metadata` — a plain list of small integers doesn't need a real array type):
hours-before-appointment offsets, defaulted to `[24, 2]` so an untouched deployment still gets
a day-before and a two-hours-before reminder before Task 6's settings panel exists to edit it.

`appointment_reminders`: the unique constraint on `(appointment_id, offset_hours)` is the
actual double-send guard (`notifications/reminders.py::send_due_reminders`'s
`INSERT ... ON CONFLICT DO NOTHING RETURNING`) — ordinary app-role DML, cascading with the
appointment it describes, since it is an idempotency marker and nothing durable on its own.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0036"
down_revision: str | None = "0035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "businesses",
        sa.Column(
            "reminder_intervals_hours",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[24, 2]'::jsonb"),
            nullable=False,
        ),
    )
    op.create_table(
        "appointment_reminders",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "appointment_id",
            sa.Uuid(),
            sa.ForeignKey("appointments.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("offset_hours", sa.Integer(), nullable=False),
        sa.Column(
            "sent_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint(
            "appointment_id", "offset_hours", name="uq_appointment_reminders_appointment_offset"
        ),
    )
    op.create_index(
        "ix_appointment_reminders_appointment_id", "appointment_reminders", ["appointment_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_appointment_reminders_appointment_id", table_name="appointment_reminders")
    op.drop_table("appointment_reminders")
    op.drop_column("businesses", "reminder_intervals_hours")
