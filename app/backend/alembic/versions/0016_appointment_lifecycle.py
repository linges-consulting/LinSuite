"""The rest of the status lifecycle (Task 18): when each terminal state was reached, and why
a cancellation happened.

Revision ID: 0016
Revises: 0015
Create Date: 2026-09-20

`status` and its CHECK already exist (migration 0014); this adds the timestamps the ruling
calls for. **Completion is the event later phases key off** — treatment receipts, package
credit deduction, commission — so `completed_at` is stamped by exactly one code path
(`scheduling/appointments.py`'s `complete_appointment`), never inferred from `updated_at`.
`cancelled_at`/`cancel_reason` and `no_show_at` are the same idea for the other two terminal
states. All three are nullable: an appointment sits in exactly one of `confirmed` and these
four columns until it leaves it, and never goes back (no reopen in M1).

Nothing enforces "exactly one of these is set" at the database — the four status values and
the three columns would need a CHECK spelling out nine combinations for one typo class this
ticket's own code already rules out by construction. Grants are inherited from 0001's ALTER
DEFAULT PRIVILEGES.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("appointments", sa.Column("completed_at", sa.DateTime(timezone=True)))
    op.add_column("appointments", sa.Column("cancelled_at", sa.DateTime(timezone=True)))
    op.add_column("appointments", sa.Column("cancel_reason", sa.Text()))
    op.add_column("appointments", sa.Column("no_show_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    op.drop_column("appointments", "no_show_at")
    op.drop_column("appointments", "cancel_reason")
    op.drop_column("appointments", "cancelled_at")
    op.drop_column("appointments", "completed_at")
