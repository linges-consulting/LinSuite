"""The record of an availability override on the appointment it was made for.

Revision ID: 0015
Revises: 0014
Create Date: 2026-09-20

Tech-stack §22: shift end, time off, closures and the booking horizon are *advisory* — a
human may book past them, and the override is recorded. `overridden_rules` is the list of
those rule names the booking (or the last move) broke, null when it broke none, so the
calendar can mark the card; `override_reason` is what the person confirming typed, if
anything. Who authorized it is in the audit log (`appointment.availability_overridden`),
which is the record; these two columns are the calendar's copy of the *what*.

Nothing here for rooms or equipment: those conflicts are physical and have no override to
record. Grants are inherited from 0001's ALTER DEFAULT PRIVILEGES.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("appointments", sa.Column("overridden_rules", JSONB(), nullable=True))
    op.add_column("appointments", sa.Column("override_reason", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("appointments", "override_reason")
    op.drop_column("appointments", "overridden_rules")
