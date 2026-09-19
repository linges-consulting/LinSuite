"""The business profile, its brand colours, and the two branding images.

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-18

Every profile column is nullable except the three with a default. A business that has
completed setup has a name and a timezone and nothing else yet, and making an address
mandatory here would mean the migration could not run against an instance that is already
live — the address is collected on a screen, not by the wizard.

`country` and `currency_symbol` carry defaults rather than being nullable, because "no
country" and "no currency symbol" are not states a receipt can be rendered from.

Three CHECK constraints, all for values that end up somewhere they cannot be validated again:
the province is what a tax-rate table will join on, the postal code is printed on a receipt,
and the brand colours are interpolated into CSS variables on `<html>`. Pydantic refuses a bad
one at the boundary; the constraint is what refuses it when the next writer is a migration or
a psql session. That division is why `BusinessOut` does not re-validate on the way out — the
row is already trustworthy, and a read that 500s helps nobody.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PROVINCES = ("AB", "BC", "MB", "NB", "NL", "NS", "NT", "NU", "ON", "PE", "QC", "SK", "YT")

_PROFILE = (
    ("address_line1", sa.String(200)),
    ("address_line2", sa.String(200)),
    ("city", sa.String(100)),
    ("province", sa.String(2)),
    ("postal_code", sa.String(7)),
    ("phone", sa.String(32)),
    ("email", sa.String(320)),
    ("gst_hst_number", sa.String(64)),
    ("pst_qst_number", sa.String(64)),
    ("receipt_footer", sa.Text()),
)


def upgrade() -> None:
    for name, type_ in _PROFILE:
        op.add_column("businesses", sa.Column(name, type_, nullable=True))
    op.add_column(
        "businesses",
        sa.Column("country", sa.String(2), server_default=sa.text("'CA'"), nullable=False),
    )
    op.add_column(
        "businesses",
        sa.Column("currency_symbol", sa.String(8), server_default=sa.text("'$'"), nullable=False),
    )
    op.add_column(
        "businesses",
        sa.Column(
            "brand_primary", sa.String(7), server_default=sa.text("'#1d4ed8'"), nullable=False
        ),
    )
    op.add_column(
        "businesses",
        sa.Column(
            "brand_secondary", sa.String(7), server_default=sa.text("'#0f766e'"), nullable=False
        ),
    )
    op.create_check_constraint(
        "ck_businesses_province",
        "businesses",
        "province IS NULL OR province IN (" + ", ".join(f"'{p}'" for p in PROVINCES) + ")",
    )
    # Canada Post's format, in the one shape the API normalises to: `A1A 1A1`, upper case.
    op.create_check_constraint(
        "ck_businesses_postal_code",
        "businesses",
        "postal_code IS NULL OR postal_code ~ '^[A-Z][0-9][A-Z] [0-9][A-Z][0-9]$'",
    )
    op.create_check_constraint(
        "ck_businesses_brand_hex",
        "businesses",
        "brand_primary ~ '^#[0-9a-f]{6}$' AND brand_secondary ~ '^#[0-9a-f]{6}$'",
    )

    op.create_table(
        "branding_assets",
        sa.Column("kind", sa.String(16), primary_key=True),
        sa.Column("content_type", sa.String(64), nullable=False),
        sa.Column("data", sa.LargeBinary(), nullable=False),
        sa.Column("byte_length", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("kind IN ('logo', 'favicon')", name="ck_branding_assets_kind"),
    )


def downgrade() -> None:
    op.drop_table("branding_assets")
    op.drop_constraint("ck_businesses_brand_hex", "businesses", type_="check")
    op.drop_constraint("ck_businesses_postal_code", "businesses", type_="check")
    op.drop_constraint("ck_businesses_province", "businesses", type_="check")
    for column in ("brand_secondary", "brand_primary", "currency_symbol", "country"):
        op.drop_column("businesses", column)
    for name, _ in reversed(_PROFILE):
        op.drop_column("businesses", name)
