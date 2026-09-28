"""`queue_entries.appointment_id` (Phase 7 Task 5, #12).

Revision ID: 0043
Revises: 0042
Create Date: 2026-09-27

The back-reference a converted walk-in needs so `scheduling/appointments.py::
complete_appointment` can flip its queue entry from `in_service` to `done` on completion
(m3.md's own text: "in_service then done on appointment completion") — `POST /queue-entries/
{id}/start` sets it the moment the entry's `Appointment` exists. Nullable — a `waiting`/
`abandoned` entry that never converted has none — and no cascade, the same "never
hard-deleted" rule `customer_id`/`requested_service_id`/`preferred_staff_id` already follow
on this table (0040): an appointment is never hard-deleted either.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0043"
down_revision: str | None = "0042"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "queue_entries",
        sa.Column("appointment_id", sa.Uuid(), sa.ForeignKey("appointments.id"), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("queue_entries", "appointment_id")
