"""Package & bundle definitions (#60): admin-configured prepaid credit for one or more
services.

Revision ID: 0044
Revises: 0043
Create Date: 2026-09-28

Two tables, the same shape as `0012_services.py`'s `services`/`service_staff` pair.

`package_definitions` holds what an administrator sets: the price the whole definition sells
for, in integer cents, an optional per-definition expiry (`expires_after_days`, NULL by
default — never expires until an admin opts in), and `transferable` (off by default, per
CLAUDE.md). `ux_package_definitions_name` makes names unique case-insensitively, the same
functional-index shape as `ux_services_name`.

`package_definition_services` is a direct many-to-many onto `services`, composite primary
key, credits per row — a "package" is a definition with exactly one row here, a "bundle" is
one with several; nothing else about the schema distinguishes them (see `billing/models.py`).
It FKs to `services` only, never to a product: CLAUDE.md is explicit that no mixed
service/product package and no retail credit bundle may be representable, and there is no
column here a product id could go in.

New capability `billing.manage` (Administrator only, Admin Mode) gates the whole admin
surface, the same shape 0012 gave `catalog.manage`.

Grants are inherited from 0001's ALTER DEFAULT PRIVILEGES; nothing is repeated here.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0044"
down_revision: str | None = "0043"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "package_definitions",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text()),
        # Integer cents. Never a numeric, never a float.
        sa.Column("price_cents", sa.Integer(), nullable=False),
        # NULL = never expires (default).
        sa.Column("expires_after_days", sa.SmallInteger()),
        sa.Column("transferable", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("price_cents >= 0", name="ck_package_definitions_price"),
        sa.CheckConstraint(
            "expires_after_days IS NULL OR expires_after_days >= 1",
            name="ck_package_definitions_expiry",
        ),
    )
    op.create_index(
        "ux_package_definitions_name", "package_definitions", [sa.text("lower(name)")], unique=True
    )

    op.create_table(
        "package_definition_services",
        sa.Column(
            "package_definition_id",
            sa.Uuid(),
            sa.ForeignKey("package_definitions.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "service_id",
            sa.Uuid(),
            sa.ForeignKey("services.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("credits", sa.SmallInteger(), nullable=False),
        sa.CheckConstraint("credits >= 1", name="ck_package_definition_services_credits"),
    )

    op.execute(
        "INSERT INTO role_capabilities (role_id, capability) "
        "SELECT id, 'billing.manage' FROM roles WHERE name = 'Administrator' AND is_system "
        "ON CONFLICT DO NOTHING"
    )


def downgrade() -> None:
    op.execute("DELETE FROM role_capabilities WHERE capability = 'billing.manage'")
    op.drop_table("package_definition_services")
    op.drop_index("ux_package_definitions_name", table_name="package_definitions")
    op.drop_table("package_definitions")
