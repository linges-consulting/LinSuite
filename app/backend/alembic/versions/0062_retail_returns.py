"""Retail returns + retail money on the one payment ledger (#76).

- `invoice_payments` / `invoice_refunds` each reference exactly one of `invoices` or
  `retail_invoices` (owner decision before Wave 7: one ledger, not parallel tables). The
  0058/0060 append-only grants and triggers are untouched.
- `retail_returns` (one return action; optional link to the refund it made) and
  `retail_return_lines` (quantity back per `retail_invoice_lines` row, and whether it was
  restocked). Both append-only by grant and trigger.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0062"
down_revision: str | None = "0061"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LEDGER = ("invoice_payments", "invoice_refunds")
RETURNS = ("retail_returns", "retail_return_lines")


def upgrade() -> None:
    for table in LEDGER:
        op.alter_column(table, "invoice_id", nullable=True)
        op.add_column(
            table,
            sa.Column(
                "retail_invoice_id",
                sa.Uuid(),
                sa.ForeignKey("retail_invoices.id", ondelete="CASCADE"),
                nullable=True,
            ),
        )
        op.create_check_constraint(
            f"ck_{table}_one_invoice", table, "num_nonnulls(invoice_id, retail_invoice_id) = 1"
        )
        op.create_index(f"ix_{table}_retail_invoice", table, ["retail_invoice_id"])

    op.create_table(
        "retail_returns",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "retail_invoice_id",
            sa.Uuid(),
            sa.ForeignKey("retail_invoices.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "refund_id",
            sa.Uuid(),
            sa.ForeignKey("invoice_refunds.id", ondelete="RESTRICT"),
            nullable=True,
            unique=True,
        ),
        sa.Column(
            "returned_by", sa.Uuid(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column(
            "returned_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_retail_returns_retail_invoice", "retail_returns", ["retail_invoice_id"])
    op.create_table(
        "retail_return_lines",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "return_id",
            sa.Uuid(),
            sa.ForeignKey("retail_returns.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "retail_invoice_line_id",
            sa.Uuid(),
            sa.ForeignKey("retail_invoice_lines.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("restocked", sa.Boolean(), nullable=False),
        sa.CheckConstraint("quantity >= 1", name="ck_retail_return_lines_quantity"),
    )
    op.create_index(
        "ix_retail_return_lines_invoice_line", "retail_return_lines", ["retail_invoice_line_id"]
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION retail_returns_append_only() RETURNS trigger
        SET search_path = pg_catalog, pg_temp
        AS $$
        BEGIN
            IF current_user = 'linsuite_purge'
               OR current_user = (
                   SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid = TG_RELID
               ) THEN
                RETURN CASE TG_OP WHEN 'DELETE' THEN OLD ELSE NEW END;
            END IF;
            RAISE EXCEPTION '% is append-only: % is not permitted for %',
                TG_TABLE_NAME, TG_OP, current_user
                USING ERRCODE = 'insufficient_privilege';
        END $$ LANGUAGE plpgsql;
        """
    )
    for table in RETURNS:
        op.execute(f"REVOKE UPDATE, DELETE ON {table} FROM linsuite_app")
        op.execute(
            f"""
            CREATE TRIGGER {table}_no_rewrite
            BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION retail_returns_append_only();
            """
        )


def downgrade() -> None:
    for table in RETURNS:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_no_rewrite ON {table}")
    op.execute("DROP FUNCTION IF EXISTS retail_returns_append_only()")
    op.drop_table("retail_return_lines")
    op.drop_table("retail_returns")
    for table in LEDGER:
        op.execute(f"DELETE FROM {table} WHERE retail_invoice_id IS NOT NULL")
        op.drop_index(f"ix_{table}_retail_invoice", table_name=table)
        op.drop_constraint(f"ck_{table}_one_invoice", table, type_="check")
        op.drop_column(table, "retail_invoice_id")
        op.alter_column(table, "invoice_id", nullable=False)
