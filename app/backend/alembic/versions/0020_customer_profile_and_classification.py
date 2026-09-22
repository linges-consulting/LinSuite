"""Profile details, contacts, notes and the VIP threshold (Task 3, #38).

Revision ID: 0020
Revises: 0019
Create Date: 2026-09-21

The profile grows in place, exactly as `customers/models.py` says it would: date of birth,
one emergency contact and one secondary contact as columns rather than a table (there is
never more than one of each, and purge is a `SET NULL` on the columns), free-text front-desk
`notes`, and `updated_at` for the edit dialog to show.

`date_of_birth` is the one column with a CHECK here — not in the future — because "before
1900" is a data-entry typo the API can catch with a better message than a constraint would
give; the database only needs to hold the invariant a constraint can express cheaply.

Classification (pre-flight D8) needs one business setting: `vip_visit_threshold`, CHECK
2–1000, default 10, on the same row `slot_granularity_minutes` and `booking_horizon_days`
live on. The classification itself is never stored — it is a read-time count of `completed`
appointments against this number.

Grants are inherited from 0001's `ALTER DEFAULT PRIVILEGES`; nothing is repeated here.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("customers", sa.Column("date_of_birth", sa.Date(), nullable=True))
    op.add_column("customers", sa.Column("emergency_contact_name", sa.String(100), nullable=True))
    op.add_column("customers", sa.Column("emergency_contact_phone", sa.String(32), nullable=True))
    op.add_column(
        "customers", sa.Column("emergency_contact_relationship", sa.String(100), nullable=True)
    )
    op.add_column("customers", sa.Column("secondary_contact_name", sa.String(100), nullable=True))
    op.add_column("customers", sa.Column("secondary_contact_phone", sa.String(32), nullable=True))
    op.add_column("customers", sa.Column("secondary_contact_email", sa.String(254), nullable=True))
    op.add_column("customers", sa.Column("notes", sa.Text(), nullable=True))
    op.add_column(
        "customers",
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_check_constraint(
        "ck_customers_dob_not_future",
        "customers",
        "date_of_birth IS NULL OR date_of_birth <= CURRENT_DATE",
    )

    op.add_column(
        "businesses",
        sa.Column("vip_visit_threshold", sa.Integer(), server_default="10", nullable=False),
    )
    op.create_check_constraint(
        "ck_businesses_vip_visit_threshold",
        "businesses",
        "vip_visit_threshold BETWEEN 2 AND 1000",
    )


def downgrade() -> None:
    op.drop_constraint("ck_businesses_vip_visit_threshold", "businesses", type_="check")
    op.drop_column("businesses", "vip_visit_threshold")

    op.drop_constraint("ck_customers_dob_not_future", "customers", type_="check")
    op.drop_column("customers", "updated_at")
    op.drop_column("customers", "notes")
    op.drop_column("customers", "secondary_contact_email")
    op.drop_column("customers", "secondary_contact_phone")
    op.drop_column("customers", "secondary_contact_name")
    op.drop_column("customers", "emergency_contact_relationship")
    op.drop_column("customers", "emergency_contact_phone")
    op.drop_column("customers", "emergency_contact_name")
    op.drop_column("customers", "date_of_birth")
