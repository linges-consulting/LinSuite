"""Tax pre-fill (#118, spec #113 "Tax pre-fill"): `businesses.tax_confirmed_at` and
`tax_components.origin`.

Revision ID: 0075
Revises: 0074
Create Date: 2026-09-29

Two columns for one feature, in one migration: `tax_confirmed_at` is the owner's "Looks right"
on the Tax step (`billing/tax_routes.py`); `origin` (`'manual'` default, `'prefill'` set only by
`billing/tax_prefill.py`) is what lets `settings/onboarding_routes.py::_tax_done` and the Tax
panel's own prompt logic tell a pre-filled component apart from one an owner typed by hand,
without guessing from timing. Every row that exists before this migration is `'manual'` — the
correct answer, since pre-fill did not exist yet to have created any of them.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0075"
down_revision: str | None = "0074"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("businesses", sa.Column("tax_confirmed_at", sa.DateTime(timezone=True)))
    op.add_column(
        "tax_components",
        sa.Column("origin", sa.String(16), nullable=False, server_default="manual"),
    )
    op.create_check_constraint(
        "ck_tax_components_origin", "tax_components", "origin IN ('manual', 'prefill')"
    )


def downgrade() -> None:
    op.drop_constraint("ck_tax_components_origin", "tax_components", type_="check")
    op.drop_column("tax_components", "origin")
    op.drop_column("businesses", "tax_confirmed_at")
