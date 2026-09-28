"""Retail sale: draft -> atomic stock-deducting issue (#75).

Revision ID: 0056
Revises: 0055
Create Date: 2026-09-28

A retail sale of `ProductVariant`s is always its own invoice, never combined with a service
bill/invoice (CLAUDE.md "Domain rules"). Four new tables, mirroring the two existing pairs:
`retail_sales`/`retail_sale_lines` are the draft/mutable shape `service_bills`/
`service_bill_lines` already establish (plain app-role DML, no grant changes — a draft cart is
built and edited freely until issue); `retail_invoices`/`retail_invoice_lines` are the
issued/frozen shape `invoices`/`invoice_lines` already establish (voidable + append-only
grants/triggers). See `billing/models.py`'s `## retail sale` section for the full design.

**Numbering reuses `business_invoice_counters`** — no new counter table. A service invoice and
a retail invoice for the same business draw from the one row, so their numbers never collide,
even though each keeps its own `uq_*_business_number` defense-in-depth constraint within its
own table (the same role `uq_invoices_business_number` already plays for `invoices`).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0056"
down_revision: str | None = "0055"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- the draft: a cart, freely mutable until issue -----------------------------------------
    op.create_table(
        "retail_sales",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        # Nullable — the opposite of `service_bills.customer_id`: a walk-up sale may have no
        # customer record at all.
        sa.Column(
            "customer_id",
            sa.Uuid(),
            sa.ForeignKey("customers.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("status", sa.String(16), nullable=False, server_default=sa.text("'draft'")),
        sa.Column(
            "sold_by_staff_id",
            sa.Uuid(),
            sa.ForeignKey("staff.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "payment_collector_staff_id",
            sa.Uuid(),
            sa.ForeignKey("staff.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("status IN ('draft', 'issued')", name="ck_retail_sales_status"),
    )

    op.create_table(
        "retail_sale_lines",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "sale_id",
            sa.Uuid(),
            sa.ForeignKey("retail_sales.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # No `ondelete` — a variant is never hard-deleted (`stock_movements.variant_id`'s own
        # precedent, 0050).
        sa.Column("variant_id", sa.Uuid(), sa.ForeignKey("product_variants.id"), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("unit_price_cents", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("quantity >= 1", name="ck_retail_sale_lines_quantity"),
        sa.CheckConstraint("unit_price_cents >= 0", name="ck_retail_sale_lines_price"),
    )
    op.create_index("ix_retail_sale_lines_sale", "retail_sale_lines", ["sale_id"])

    # --- the issued, immutable retail invoice -------------------------------------------------
    op.create_table(
        "retail_invoices",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("business_id", sa.Integer(), sa.ForeignKey("businesses.id"), nullable=False),
        sa.Column("invoice_number", sa.Integer(), nullable=False),
        sa.Column(
            "retail_sale_id",
            sa.Uuid(),
            sa.ForeignKey("retail_sales.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "customer_id",
            sa.Uuid(),
            sa.ForeignKey("customers.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'issued'")),
        sa.Column("subtotal_cents", sa.Integer(), nullable=False),
        sa.Column("grand_total_cents", sa.Integer(), nullable=False),
        sa.Column(
            "sold_by_staff_id",
            sa.Uuid(),
            sa.ForeignKey("staff.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "payment_collector_staff_id",
            sa.Uuid(),
            sa.ForeignKey("staff.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "issued_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "issued_by", sa.Uuid(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "replaces_invoice_id", sa.Uuid(), sa.ForeignKey("retail_invoices.id"), nullable=True
        ),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "cancelled_by", sa.Uuid(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
        ),
        sa.Column("cancel_reason", sa.Text(), nullable=True),
        sa.UniqueConstraint(
            "business_id", "invoice_number", name="uq_retail_invoices_business_number"
        ),
        sa.UniqueConstraint("retail_sale_id", name="uq_retail_invoices_retail_sale_id"),
        sa.CheckConstraint("status IN ('issued', 'cancelled')", name="ck_retail_invoices_status"),
        sa.CheckConstraint(
            "(status = 'issued' AND cancelled_at IS NULL AND cancelled_by IS NULL "
            "AND cancel_reason IS NULL) OR "
            "(status = 'cancelled' AND cancelled_at IS NOT NULL AND cancelled_by IS NOT NULL "
            "AND cancel_reason IS NOT NULL)",
            name="ck_retail_invoices_cancel_fields",
        ),
    )
    op.create_index("ix_retail_invoices_customer", "retail_invoices", ["customer_id"])

    op.create_table(
        "retail_invoice_lines",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "invoice_id",
            sa.Uuid(),
            sa.ForeignKey("retail_invoices.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "retail_sale_line_id",
            sa.Uuid(),
            sa.ForeignKey("retail_sale_lines.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("variant_id", sa.Uuid(), sa.ForeignKey("product_variants.id"), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("unit_price_cents", sa.Integer(), nullable=False),
        sa.Column("line_total_cents", sa.Integer(), nullable=False),
        sa.Column(
            "staff_id", sa.Uuid(), sa.ForeignKey("staff.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column("commission_rate_bp", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("quantity >= 1", name="ck_retail_invoice_lines_quantity"),
        sa.CheckConstraint("unit_price_cents >= 0", name="ck_retail_invoice_lines_price"),
        sa.CheckConstraint("line_total_cents >= 0", name="ck_retail_invoice_lines_line_total"),
        sa.CheckConstraint(
            "commission_rate_bp BETWEEN 0 AND 10000", name="ck_retail_invoice_lines_commission_bp"
        ),
        sa.UniqueConstraint(
            "retail_sale_line_id", name="uq_retail_invoice_lines_retail_sale_line_id"
        ),
    )
    op.create_index("ix_retail_invoice_lines_invoice", "retail_invoice_lines", ["invoice_id"])

    # --- grants + triggers: voidable (retail_invoices), append-only (retail_invoice_lines) ----
    # `retail_sales`/`retail_sale_lines` keep the default app-role grant (0001) — a draft is
    # freely mutable until issue, the same as `service_bills`/`service_bill_lines`.
    op.execute("REVOKE DELETE ON retail_invoices FROM linsuite_app")
    op.execute("REVOKE UPDATE, DELETE ON retail_invoice_lines FROM linsuite_app")

    op.execute(
        """
        CREATE OR REPLACE FUNCTION retail_invoices_voidable_guard() RETURNS trigger
        SET search_path = pg_catalog, pg_temp
        AS $$
        BEGIN
            IF current_user = 'linsuite_purge'
               OR current_user = (
                   SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid = TG_RELID
               ) THEN
                RETURN CASE TG_OP WHEN 'DELETE' THEN OLD ELSE NEW END;
            END IF;
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'retail_invoices: DELETE is not permitted for %', current_user
                    USING ERRCODE = 'insufficient_privilege';
            END IF;
            IF OLD.status = 'issued' AND NEW.status = 'cancelled'
               AND NEW.id = OLD.id
               AND NEW.business_id = OLD.business_id
               AND NEW.invoice_number = OLD.invoice_number
               AND NEW.retail_sale_id = OLD.retail_sale_id
               AND NEW.customer_id IS NOT DISTINCT FROM OLD.customer_id
               AND NEW.subtotal_cents = OLD.subtotal_cents
               AND NEW.grand_total_cents = OLD.grand_total_cents
               AND NEW.sold_by_staff_id = OLD.sold_by_staff_id
               AND NEW.payment_collector_staff_id
                   IS NOT DISTINCT FROM OLD.payment_collector_staff_id
               AND NEW.issued_at = OLD.issued_at
               AND NEW.issued_by = OLD.issued_by
               AND NEW.created_at = OLD.created_at
               AND NEW.replaces_invoice_id IS NOT DISTINCT FROM OLD.replaces_invoice_id
               AND NEW.cancelled_at IS NOT NULL
               AND NEW.cancelled_by IS NOT NULL
               AND NEW.cancel_reason IS NOT NULL
            THEN
                RETURN NEW;
            END IF;
            RAISE EXCEPTION
                'retail_invoices: this update is not the permitted voidable transition (for %)',
                current_user
                USING ERRCODE = 'insufficient_privilege';
        END $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER retail_invoices_voidable_guard
        BEFORE UPDATE OR DELETE ON retail_invoices
        FOR EACH ROW EXECUTE FUNCTION retail_invoices_voidable_guard();
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION retail_invoice_lines_append_only() RETURNS trigger
        SET search_path = pg_catalog, pg_temp
        AS $$
        BEGIN
            IF current_user = 'linsuite_purge'
               OR current_user = (
                   SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid = TG_RELID
               ) THEN
                RETURN CASE TG_OP WHEN 'DELETE' THEN OLD ELSE NEW END;
            END IF;
            RAISE EXCEPTION 'retail_invoice_lines is append-only: % is not permitted for %',
                TG_OP, current_user
                USING ERRCODE = 'insufficient_privilege';
        END $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER retail_invoice_lines_no_rewrite
        BEFORE UPDATE OR DELETE ON retail_invoice_lines
        FOR EACH ROW EXECUTE FUNCTION retail_invoice_lines_append_only();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS retail_invoice_lines_no_rewrite ON retail_invoice_lines")
    op.execute("DROP FUNCTION IF EXISTS retail_invoice_lines_append_only()")
    op.execute("DROP TRIGGER IF EXISTS retail_invoices_voidable_guard ON retail_invoices")
    op.execute("DROP FUNCTION IF EXISTS retail_invoices_voidable_guard()")

    op.drop_index("ix_retail_invoice_lines_invoice", table_name="retail_invoice_lines")
    op.drop_table("retail_invoice_lines")
    op.drop_index("ix_retail_invoices_customer", table_name="retail_invoices")
    op.drop_table("retail_invoices")
    op.drop_index("ix_retail_sale_lines_sale", table_name="retail_sale_lines")
    op.drop_table("retail_sale_lines")
    op.drop_table("retail_sales")
