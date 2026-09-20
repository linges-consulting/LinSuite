"""The service catalog: durations, buffers, prices, eligible staff and resource requirements.

Revision ID: 0012
Revises: 0011
Create Date: 2026-09-20

Three tables.

`services` holds what the availability engine slides across a day (tech-stack §19) — the
duration and the two buffers — and the price in **integer cents** (§21). The CHECK constraints
are the half of the rule the API cannot enforce when the next writer is a psql session or an
import: five-minute steps, a duration of at least one step, buffers that are never negative,
and a price that is never below zero.

`service_staff` is a direct many-to-many onto `staff`, with a composite primary key and no
category table between them. `service_requirements` says what delivering a service needs:
`resource_id IS NULL` is "any active resource of this kind", a set one is that exact room or
device. That the two agree about `kind`, and that the resource is still active, are rules the
API enforces — see `scheduling/models.py` for why they are not a constraint here.

`ux_services_name` makes names unique case-insensitively, the same functional-index shape as
`ux_resources_kind_name` in 0010.

Grants are inherited from 0001's ALTER DEFAULT PRIVILEGES; nothing is repeated here.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "services",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("duration_minutes", sa.Integer(), nullable=False),
        sa.Column("buffer_before_minutes", sa.Integer(), server_default="0", nullable=False),
        sa.Column("buffer_after_minutes", sa.Integer(), server_default="0", nullable=False),
        # Integer cents. Never a numeric, never a float.
        sa.Column("price_cents", sa.Integer(), server_default="0", nullable=False),
        sa.Column("bookable_online", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "duration_minutes >= 5 AND duration_minutes % 5 = 0", name="ck_services_duration"
        ),
        sa.CheckConstraint(
            "buffer_before_minutes >= 0 AND buffer_before_minutes % 5 = 0 "
            "AND buffer_after_minutes >= 0 AND buffer_after_minutes % 5 = 0",
            name="ck_services_buffers",
        ),
        sa.CheckConstraint("price_cents >= 0", name="ck_services_price"),
    )
    op.create_index("ux_services_name", "services", [sa.text("lower(name)")], unique=True)

    op.create_table(
        "service_staff",
        sa.Column(
            "service_id",
            sa.Uuid(),
            sa.ForeignKey("services.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "staff_id", sa.Uuid(), sa.ForeignKey("staff.id", ondelete="CASCADE"), primary_key=True
        ),
    )

    op.create_table(
        "service_requirements",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "service_id",
            sa.Uuid(),
            sa.ForeignKey("services.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(16), nullable=False),
        # NULL is "any active resource of this kind" — the majority case.
        sa.Column("resource_id", sa.Uuid(), sa.ForeignKey("resources.id", ondelete="CASCADE")),
        sa.CheckConstraint("kind IN ('space', 'equipment')", name="ck_service_requirements_kind"),
    )
    op.create_index("ix_service_requirements_service", "service_requirements", ["service_id"])


def downgrade() -> None:
    op.drop_index("ix_service_requirements_service", table_name="service_requirements")
    op.drop_table("service_requirements")
    op.drop_table("service_staff")
    op.drop_index("ux_services_name", table_name="services")
    op.drop_table("services")
