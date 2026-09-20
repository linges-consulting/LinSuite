"""The two numbers the availability engine reads from the business: grid and horizon.

Revision ID: 0013
Revises: 0012
Create Date: 2026-09-20

`slot_granularity_minutes` is the step slot starts are offered on, measured from local
midnight (tech-stack §19 step 4): fifteen by default, five-minute steps like every other
duration in the schedule, and never more than an hour — a grid coarser than that is not a
grid. `booking_horizon_days` bounds how far ahead anything is computed (§19, "bounded
horizon"): ninety by default, at most a year.

Both carry a server default so an instance that is already live gets the documented
behaviour without a data fix, and both are CHECK-constrained here for the same reason the
profile columns in 0008 are — the API refuses a bad value with a 422, the constraint refuses
it when the next writer is a psql session.

Grants are inherited from 0001's ALTER DEFAULT PRIVILEGES; nothing is repeated here.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "businesses",
        sa.Column("slot_granularity_minutes", sa.Integer(), server_default="15", nullable=False),
    )
    op.add_column(
        "businesses",
        sa.Column("booking_horizon_days", sa.Integer(), server_default="90", nullable=False),
    )
    op.create_check_constraint(
        "ck_businesses_slot_granularity",
        "businesses",
        "slot_granularity_minutes BETWEEN 5 AND 60 AND slot_granularity_minutes % 5 = 0",
    )
    op.create_check_constraint(
        "ck_businesses_booking_horizon",
        "businesses",
        "booking_horizon_days BETWEEN 1 AND 365",
    )


def downgrade() -> None:
    op.drop_constraint("ck_businesses_booking_horizon", "businesses", type_="check")
    op.drop_constraint("ck_businesses_slot_granularity", "businesses", type_="check")
    op.drop_column("businesses", "booking_horizon_days")
    op.drop_column("businesses", "slot_granularity_minutes")
