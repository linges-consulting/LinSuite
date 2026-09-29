"""Per-item tax, inclusive pricing, retail tax/discounts, override distribution (M4 review T1).

- `services`, `package_definitions`: `tax_component_keys` (component codes) and
  `tax_convention`; `product_variants` gains `tax_convention` (it already had keys). Existing
  services/package definitions are backfilled with every active component, so a live draft
  bill keeps the tax it showed before (the pre-0064 rule taxed every line with every
  component). New items default to none — each item opts in.
- Override entry convention + commission treatment on `service_bills` / `bill_override_requests`.
- Issue-time snapshot columns on `invoices`, `invoice_lines`, `invoice_line_discounts`,
  `retail_invoices`, `retail_invoice_lines`, and two new append-only tables for retail line
  discounts/taxes; `retail_sale_discounts` holds a retail draft's selection.

Issued rows are never rewritten: every new column on an issued table is nullable or has a
default that is true for pre-0064 rows (no tax/discount, exclusive, no override). The new
money columns on `invoices`/`retail_invoices` are guarded by `snapshot_columns_frozen`, a
separate trigger so the existing voidable guards stay untouched.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0065"
down_revision: str | None = "0064"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CONVENTION = "IN ('inclusive', 'exclusive')"
BASIS = "IN ('reduces', 'absorbed')"
APPEND_ONLY = ("retail_invoice_line_discounts", "retail_invoice_line_taxes")
FROZEN = {
    "invoices": (
        "tax_rates_by_component",
        "tax_convention",
        "override_tax_convention",
        "override_commission_basis",
    ),
    "retail_invoices": ("discount_total_cents", "tax_total_cents", "tax_totals_by_component"),
}


def _convention(table: str, column: str, default: str | None, nullable: bool = False) -> None:
    op.add_column(
        table,
        sa.Column(
            column,
            sa.String(16),
            nullable=nullable,
            server_default=sa.text(f"'{default}'") if default else None,
        ),
    )
    values = BASIS if "basis" in column else CONVENTION
    op.create_check_constraint(
        f"ck_{table}_{column}", table, f"{column} IS NULL OR {column} {values}"
    )


def upgrade() -> None:
    # --- catalog toggles ---------------------------------------------------------------------
    for table in ("services", "package_definitions"):
        op.add_column(
            table,
            sa.Column(
                "tax_component_keys", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")
            ),
        )
        op.execute(
            f"UPDATE {table} SET tax_component_keys = (SELECT coalesce(jsonb_agg(code ORDER BY "
            "code), '[]'::jsonb) FROM tax_components WHERE active)"
        )
    for table in ("services", "package_definitions", "product_variants"):
        _convention(table, "tax_convention", "exclusive")

    # --- override entry ----------------------------------------------------------------------
    _convention("service_bills", "override_tax_convention", "inclusive")
    _convention("service_bills", "override_commission_basis", "reduces")
    _convention("bill_override_requests", "tax_convention", "inclusive")
    _convention("bill_override_requests", "commission_basis", "reduces")

    # --- issued service invoices -------------------------------------------------------------
    op.add_column(
        "invoices",
        sa.Column(
            "tax_rates_by_component", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
    )
    _convention("invoices", "tax_convention", None, nullable=True)
    _convention("invoices", "override_tax_convention", None, nullable=True)
    _convention("invoices", "override_commission_basis", None, nullable=True)
    _convention("invoice_lines", "tax_convention", "exclusive")
    op.add_column(
        "invoice_lines",
        sa.Column(
            "override_adjustment_cents", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
    )
    for column, kind in (
        ("percentage_bp", sa.Integer()),
        ("amount_cents", sa.Integer()),
        ("stackable", sa.Boolean()),
        ("resolved_amount_cents", sa.Integer()),
    ):
        op.add_column("invoice_line_discounts", sa.Column(column, kind, nullable=True))

    # --- retail ------------------------------------------------------------------------------
    for column in ("discount_total_cents", "tax_total_cents"):
        op.add_column(
            "retail_invoices",
            sa.Column(column, sa.Integer(), nullable=False, server_default=sa.text("0")),
        )
    op.add_column(
        "retail_invoices",
        sa.Column(
            "tax_totals_by_component", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
    )
    for column in ("discount_cents", "tax_cents"):
        op.add_column(
            "retail_invoice_lines",
            sa.Column(column, sa.Integer(), nullable=False, server_default=sa.text("0")),
        )
    _convention("retail_invoice_lines", "tax_convention", "exclusive")
    op.add_column(
        "retail_invoice_lines", sa.Column("commission_basis_cents", sa.Integer(), nullable=True)
    )

    op.create_table(
        "retail_sale_discounts",
        sa.Column(
            "sale_id",
            sa.Uuid(),
            sa.ForeignKey("retail_sales.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "discount_id",
            sa.Uuid(),
            sa.ForeignKey("discounts.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column(
            "applied_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    line_fk = sa.ForeignKey("retail_invoice_lines.id", ondelete="CASCADE")
    op.create_table(
        "retail_invoice_line_discounts",
        sa.Column("retail_invoice_line_id", sa.Uuid(), line_fk, primary_key=True),
        sa.Column(
            "discount_id",
            sa.Uuid(),
            sa.ForeignKey("discounts.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("discount_name", sa.Text(), nullable=False),
        sa.Column("discount_kind", sa.Text(), nullable=False),
        sa.Column("percentage_bp", sa.Integer(), nullable=True),
        sa.Column("amount_cents", sa.Integer(), nullable=True),
        sa.Column("stackable", sa.Boolean(), nullable=False),
        sa.Column("commission_basis", sa.Text(), nullable=False),
        sa.Column("resolved_amount_cents", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "discount_kind IN ('percentage', 'fixed')", name="ck_retail_invoice_line_discounts_kind"
        ),
        sa.CheckConstraint(
            f"commission_basis {BASIS}", name="ck_retail_invoice_line_discounts_commission_basis"
        ),
    )
    op.create_table(
        "retail_invoice_line_taxes",
        sa.Column(
            "retail_invoice_line_id",
            sa.Uuid(),
            sa.ForeignKey("retail_invoice_lines.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("component_code", sa.String(16), primary_key=True),
        sa.Column("rate_bp", sa.Integer(), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.CheckConstraint("rate_bp BETWEEN 0 AND 10000", name="ck_retail_invoice_line_taxes_rate"),
    )
    for table in APPEND_ONLY:
        # `retail_returns_append_only` (0063) is table-generic (`TG_TABLE_NAME`).
        op.execute(f"REVOKE UPDATE, DELETE ON {table} FROM linsuite_app")
        op.execute(
            f"""
            CREATE TRIGGER {table}_no_rewrite
            BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION retail_returns_append_only();
            """
        )

    # --- the new snapshot columns on the two voidable tables never change ---------------------
    op.execute(
        """
        CREATE OR REPLACE FUNCTION snapshot_columns_frozen() RETURNS trigger
        SET search_path = pg_catalog, pg_temp
        AS $$
        DECLARE col text;
        BEGIN
            IF current_user = 'linsuite_purge'
               OR current_user = (
                   SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid = TG_RELID
               ) THEN
                RETURN NEW;
            END IF;
            FOREACH col IN ARRAY TG_ARGV LOOP
                IF to_jsonb(NEW) -> col IS DISTINCT FROM to_jsonb(OLD) -> col THEN
                    RAISE EXCEPTION '%: % is frozen at issue (for %)',
                        TG_TABLE_NAME, col, current_user
                        USING ERRCODE = 'insufficient_privilege';
                END IF;
            END LOOP;
            RETURN NEW;
        END $$ LANGUAGE plpgsql;
        """
    )
    for table, columns in FROZEN.items():
        args = ", ".join(f"'{c}'" for c in columns)
        op.execute(
            f"""
            CREATE TRIGGER {table}_tax_snapshot_frozen
            BEFORE UPDATE ON {table}
            FOR EACH ROW EXECUTE FUNCTION snapshot_columns_frozen({args});
            """
        )


def downgrade() -> None:
    for table in FROZEN:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_tax_snapshot_frozen ON {table}")
    op.execute("DROP FUNCTION IF EXISTS snapshot_columns_frozen()")
    for table in APPEND_ONLY:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_no_rewrite ON {table}")
    op.drop_table("retail_invoice_line_taxes")
    op.drop_table("retail_invoice_line_discounts")
    op.drop_table("retail_sale_discounts")
    for column in ("discount_cents", "tax_cents", "tax_convention", "commission_basis_cents"):
        op.drop_column("retail_invoice_lines", column)
    for column in ("discount_total_cents", "tax_total_cents", "tax_totals_by_component"):
        op.drop_column("retail_invoices", column)
    for column in ("percentage_bp", "amount_cents", "stackable", "resolved_amount_cents"):
        op.drop_column("invoice_line_discounts", column)
    for column in ("tax_convention", "override_adjustment_cents"):
        op.drop_column("invoice_lines", column)
    for column in (
        "tax_rates_by_component",
        "tax_convention",
        "override_tax_convention",
        "override_commission_basis",
    ):
        op.drop_column("invoices", column)
    for column in ("tax_convention", "commission_basis"):
        op.drop_column("bill_override_requests", column)
    for column in ("override_tax_convention", "override_commission_basis"):
        op.drop_column("service_bills", column)
    for table in ("services", "package_definitions", "product_variants"):
        op.drop_column(table, "tax_convention")
    for table in ("services", "package_definitions"):
        op.drop_column(table, "tax_component_keys")
