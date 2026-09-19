"""Spaces and equipment: the physical things a service is delivered in and with.

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-18

One table, `resources`, told apart by `kind`. The exclusion constraint that refuses two
appointments claiming the same one at once (tech-stack §15, §20) arrives with the booking
ticket — this migration only creates the resource itself.

`ux_resources_kind_name` is what makes a name unique per kind, case-insensitively: "Room 1"
and "room 1" collide within `space`, but the same name is free to equipment. A functional
index rather than a generated lowercase column, because nothing else ever needs to read the
lowercase form.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "resources",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("colour", sa.String(16)),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("kind IN ('space', 'equipment')", name="ck_resources_kind"),
    )
    op.create_index(
        "ux_resources_kind_name", "resources", ["kind", sa.text("lower(name)")], unique=True
    )
    # The picker this ticket serves (`GET /admin/resources?kind=`) reads in this order.
    op.create_index(
        "ix_resources_kind_active_sort", "resources", ["kind", "active", "sort_order", "name"]
    )


def downgrade() -> None:
    op.drop_index("ix_resources_kind_active_sort", table_name="resources")
    op.drop_index("ux_resources_kind_name", table_name="resources")
    op.drop_table("resources")
