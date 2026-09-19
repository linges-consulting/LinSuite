"""Staff records: credentials, commission rates, colours — and accounts with no password.

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-18

Three things happen here, and they belong together.

**`users.password_hash` becomes nullable.** An administrator can now create an account, and
the account is born with no password at all rather than with a temporary one somebody has to
be told. Null means "no password has ever been set"; `auth/login.py` refuses such an account
with its own code and never hands the column to Argon2.

**The `staff` table.** One row per account, enforced by a unique `user_id`. The three CHECK
constraints are the rules that end up somewhere they cannot be re-validated: a commission
rate is snapshotted onto an invoice line, a concurrency limit is read by a booking trigger,
and a practitioner's designation and licence number are printed on a treatment receipt an
insurer will reject without them (tech-stack §21). Pydantic refuses a bad value at the
boundary; these refuse it when the next writer is a migration, an import or a psql session.

**The backfill.** Every account that predates this table gets a staff record, so "the staff
member behind this account" is never a question with no answer. The name starts as the
address's local part — the only name an instance set up by the wizard has — and the colours
are dealt round the palette in account order. `WHERE NOT EXISTS` makes it idempotent, which
is also what lets `tests/test_staff.py` run it against an account created after the fact.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Spelled out rather than imported from `scheduling/palette.py`: a migration is a record of
# what the database did on the day it ran, and it must keep saying that after somebody
# reorders the palette in a later release.
PALETTE_KEYS = (
    "blue",
    "teal",
    "rose",
    "amber",
    "violet",
    "green",
    "cyan",
    "pink",
    "lime",
    "indigo",
    "orange",
    "slate",
)

BACKFILL = f"""
INSERT INTO staff (user_id, first_name, last_name, display_name, colour)
SELECT u.id,
       split_part(u.email, '@', 1),
       '',
       split_part(u.email, '@', 1),
       (ARRAY[{", ".join(f"'{key}'" for key in PALETTE_KEYS)}])
         [(u.seat - 1) % {len(PALETTE_KEYS)} + 1]
FROM (
    SELECT id, email, row_number() OVER (ORDER BY created_at, id) AS seat FROM users
) AS u
WHERE NOT EXISTS (SELECT 1 FROM staff s WHERE s.user_id = u.id)
"""


def upgrade() -> None:
    op.alter_column("users", "password_hash", existing_type=sa.String(255), nullable=True)

    op.create_table(
        "staff",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("first_name", sa.String(100), nullable=False),
        sa.Column("last_name", sa.String(100), server_default="", nullable=False),
        sa.Column("display_name", sa.String(200), nullable=False),
        sa.Column("is_practitioner", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("designation", sa.String(64)),
        sa.Column("licence_number", sa.String(64)),
        sa.Column("commission_rate_services_bp", sa.Integer(), server_default="0", nullable=False),
        sa.Column("commission_rate_retail_bp", sa.Integer(), server_default="0", nullable=False),
        sa.Column("colour", sa.String(16), nullable=False),
        sa.Column("max_concurrent_appointments", sa.Integer(), server_default="1", nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "commission_rate_services_bp BETWEEN 0 AND 10000 "
            "AND commission_rate_retail_bp BETWEEN 0 AND 10000",
            name="ck_staff_commission_basis_points",
        ),
        sa.CheckConstraint(
            "max_concurrent_appointments >= 1", name="ck_staff_max_concurrent_appointments"
        ),
        sa.CheckConstraint(
            "NOT is_practitioner OR ("
            "designation IS NOT NULL AND btrim(designation) <> '' "
            "AND licence_number IS NOT NULL AND btrim(licence_number) <> '')",
            name="ck_staff_practitioner_credentials",
        ),
    )
    # The calendar reads the roster on every render, in this order.
    op.create_index("ix_staff_active_sort", "staff", ["active", "sort_order", "display_name"])

    op.execute(BACKFILL)


def downgrade() -> None:
    op.drop_index("ix_staff_active_sort", table_name="staff")
    op.drop_table("staff")
    # Only safe because nothing shipped before this migration could leave a null behind.
    op.execute("DELETE FROM users WHERE password_hash IS NULL")
    op.alter_column("users", "password_hash", existing_type=sa.String(255), nullable=False)
