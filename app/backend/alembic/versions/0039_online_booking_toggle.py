"""Booking-portal admin toggles on `businesses` (Phase 6 Task 4, #10).

Revision ID: 0039
Revises: 0038
Create Date: 2026-09-27

Three columns Tasks 1-3 never gated on, left for this task per their own scope notes:

`online_booking_enabled` (default true) — the business-wide portal switch. Tasks 1/2 only
gated per-service (`bookable_online`); this is the whole-portal gate `scheduling/public.py`'s
own docstrings named as "Task 4's job". Defaulted on so an untouched M3 deployment behaves
exactly as Tasks 1-3 already tested it (portal reachable whenever a service opts in), rather
than silently going dark on every existing test and every deployment that upgrades straight
through this migration.

`booking_daily_cap_per_ip` / `booking_daily_cap_per_email` (defaults 20 / 5) — admin-editable
counterparts of `scheduling/public.py`'s `_DAILY_CAP_PER_IP`/`_DAILY_CAP_PER_EMAIL` constants,
which are removed in the same commit that adds this migration. Same defaults as the constants
they replace, so a business that never touches the new panel section sees no behaviour change.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0039"
down_revision: str | None = "0038"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "businesses",
        sa.Column(
            "online_booking_enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False
        ),
    )
    op.add_column(
        "businesses",
        sa.Column(
            "booking_daily_cap_per_ip", sa.Integer(), server_default=sa.text("20"), nullable=False
        ),
    )
    op.add_column(
        "businesses",
        sa.Column(
            "booking_daily_cap_per_email",
            sa.Integer(),
            server_default=sa.text("5"),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "ck_businesses_booking_daily_cap_per_ip", "businesses", "booking_daily_cap_per_ip >= 1"
    )
    op.create_check_constraint(
        "ck_businesses_booking_daily_cap_per_email",
        "businesses",
        "booking_daily_cap_per_email >= 1",
    )


def downgrade() -> None:
    op.drop_constraint("ck_businesses_booking_daily_cap_per_email", "businesses", type_="check")
    op.drop_constraint("ck_businesses_booking_daily_cap_per_ip", "businesses", type_="check")
    op.drop_column("businesses", "booking_daily_cap_per_email")
    op.drop_column("businesses", "booking_daily_cap_per_ip")
    op.drop_column("businesses", "online_booking_enabled")
