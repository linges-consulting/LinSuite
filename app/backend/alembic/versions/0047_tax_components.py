"""Tax components and effective-dated rates (#57); the `billing.manage` capability.

Revision ID: 0044
Revises: 0043
Create Date: 2026-09-28

Two tables. `tax_components` is the stable definition (a code, a name, and an optional
province tying it to a jurisdiction — `core/models.py::Business.province`, the field its own
comment already reserves for this); `tax_component_rates` is that component's whole rate
history, never edited in place once written (`billing/models.py`'s own docstring — a rate
change is always a new row, so an invoice snapshot taken under an old rate is never rewritten
by a later one).

`btree_gist` already exists (0011) — nothing to install here. The `EXCLUDE` constraint is the
same shape `working_hours`/`time_off` use for a `staff_id`-scoped range, applied to
`component_id` and a *date* range instead: no two rates for one component may cover the same
day.

`billing.manage` reaches the Administrator role here, following 0022/0042's own precedent
("a new capability reaches it here or not at all"). Not the Staff role: configuring tax
components is business configuration in the same sense `catalog.manage` already is, never
front-desk work.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0044"
down_revision: str | None = "0043"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Mirrors `core.models.PROVINCE_CODES` — that module imports no domain module, so this
# migration spells the list out again rather than importing it, the same way 0008's own CHECK
# does for `businesses.province`.
_PROVINCE_CODES = ("AB", "BC", "MB", "NB", "NL", "NS", "NT", "NU", "ON", "PE", "QC", "SK", "YT")


def upgrade() -> None:
    op.create_table(
        "tax_components",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("code", sa.String(16), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("province", sa.String(2)),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("code", name="uq_tax_components_code"),
        sa.CheckConstraint(
            "province IS NULL OR province IN ("
            + ", ".join(f"'{p}'" for p in _PROVINCE_CODES)
            + ")",
            name="ck_tax_components_province",
        ),
    )

    op.create_table(
        "tax_component_rates",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "component_id",
            sa.Uuid(),
            sa.ForeignKey("tax_components.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("rate_bp", sa.Integer(), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("rate_bp BETWEEN 0 AND 10000", name="ck_tax_component_rates_bp"),
        sa.CheckConstraint(
            "effective_to IS NULL OR effective_to > effective_from",
            name="ck_tax_component_rates_range",
        ),
    )
    op.execute(
        "ALTER TABLE tax_component_rates ADD CONSTRAINT ex_tax_component_rates_no_overlap "
        "EXCLUDE USING gist (component_id WITH =, "
        "daterange(effective_from, effective_to) WITH &&)"
    )
    op.create_index(
        "ix_tax_component_rates_component",
        "tax_component_rates",
        ["component_id", "effective_from"],
    )

    op.execute(
        "INSERT INTO role_capabilities (role_id, capability) "
        "SELECT id, 'billing.manage' FROM roles WHERE name = 'Administrator' AND is_system "
        "ON CONFLICT DO NOTHING"
    )


def downgrade() -> None:
    op.execute("DELETE FROM role_capabilities WHERE capability = 'billing.manage'")
    op.drop_table("tax_component_rates")
    op.drop_table("tax_components")
