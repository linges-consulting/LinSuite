"""The retail catalog: products and their variants (M4 #56).

Revision ID: 0044
Revises: 0043
Create Date: 2026-09-28

Two tables. `products` is the name a client recognises; `product_variants` is what actually
carries a SKU, a barcode, a price and a whole-unit stock count — the same split
`inventory/models.py` explains at length. `ux_products_name` and
`ux_product_variants_product_name` are the same case-insensitive functional-index shape
`ux_services_name` (0012) uses; `ux_product_variants_sku`/`_barcode` are the same shape
applied catalog-wide rather than per-product, because a barcode scan has no product context
to narrow the search.

The three CHECK constraints (`price_cents`, `quantity_on_hand`, `low_stock_threshold` all
`>= 0`) are the half of "whole-unit, never negative" the API cannot enforce once the next
writer is a migration, an import or a psql session — mirroring `ck_services_price`.

`tax_component_keys` is JSONB, empty-array default: #57's tax-components table has not
landed yet (Wave 1, no ordering against this ticket), so this stores free-form keys for that
table to validate and rate once it exists.

Grants are inherited from 0001's ALTER DEFAULT PRIVILEGES; nothing is repeated here. Neither
table is immutable or append-only (CLAUDE.md's grants+trigger pair is for issued invoices,
stock *movements*, commission postings and the like) — a product/variant row is edited in
place like `services`, `resources`, and deactivated rather than deleted.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0044"
down_revision: str | None = "0043"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "products",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ux_products_name", "products", [sa.text("lower(name)")], unique=True)
    op.create_index("ix_products_active_sort", "products", ["active", "sort_order", "name"])

    op.create_table(
        "product_variants",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "product_id",
            sa.Uuid(),
            sa.ForeignKey("products.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("sku", sa.String(64), nullable=False),
        sa.Column("barcode", sa.String(64)),
        # Integer cents. Never a numeric, never a float.
        sa.Column("price_cents", sa.Integer(), server_default="0", nullable=False),
        sa.Column("quantity_on_hand", sa.Integer(), server_default="0", nullable=False),
        sa.Column("low_stock_threshold", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "tax_component_keys",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("price_cents >= 0", name="ck_product_variants_price"),
        sa.CheckConstraint("quantity_on_hand >= 0", name="ck_product_variants_quantity"),
        sa.CheckConstraint(
            "low_stock_threshold >= 0", name="ck_product_variants_low_stock_threshold"
        ),
    )
    op.create_index(
        "ux_product_variants_product_name",
        "product_variants",
        ["product_id", sa.text("lower(name)")],
        unique=True,
    )
    op.create_index(
        "ux_product_variants_sku", "product_variants", [sa.text("lower(sku)")], unique=True
    )
    op.create_index(
        "ux_product_variants_barcode", "product_variants", [sa.text("lower(barcode)")], unique=True
    )
    op.create_index(
        "ix_product_variants_product_active_sort",
        "product_variants",
        ["product_id", "active", "sort_order", "name"],
    )


def downgrade() -> None:
    op.drop_index("ix_product_variants_product_active_sort", table_name="product_variants")
    op.drop_index("ux_product_variants_barcode", table_name="product_variants")
    op.drop_index("ux_product_variants_sku", table_name="product_variants")
    op.drop_index("ux_product_variants_product_name", table_name="product_variants")
    op.drop_table("product_variants")
    op.drop_index("ix_products_active_sort", table_name="products")
    op.drop_index("ux_products_name", table_name="products")
    op.drop_table("products")
