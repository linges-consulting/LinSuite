"""Public booking creation: `appointments.created_by_user_id` becomes nullable (Phase 6 Task 2,
#10).

Revision ID: 0037
Revises: 0036
Create Date: 2026-09-27

Every appointment until now was created by a signed-in staff member — the column has been
NOT NULL since migration 0014. The client-facing booking endpoint has no actor at all (no
session, no user row), and a booking made through it is real and confirmed regardless — so
NULL here means exactly "the client booked this themselves", never "we forgot who did it".
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0037"
down_revision: str | None = "0036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("appointments", "created_by_user_id", nullable=True)


def downgrade() -> None:
    op.alter_column("appointments", "created_by_user_id", nullable=False)
