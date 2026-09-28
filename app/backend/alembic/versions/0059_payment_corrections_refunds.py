"""Payment correction + admin-approved refund (#67).

- `invoice_payments` gains `corrects_payment_id` (unique self-FK) + `correction_reason`: a
  correction is a new row superseding the one it names; the original stays untouched (the
  0058 append-only trigger still refuses any UPDATE/DELETE). A correction may be 0 cents.
- `invoice_refunds`: append-only, admin/owner-approved money returned to the client. The cap
  is enforced in `billing/payments.py` under a row lock on the invoice lineage.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0059"
down_revision: str | None = "0058"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "invoice_payments",
        sa.Column(
            "corrects_payment_id",
            sa.Uuid(),
            sa.ForeignKey("invoice_payments.id", ondelete="CASCADE"),
            nullable=True,
            unique=True,
        ),
    )
    op.add_column("invoice_payments", sa.Column("correction_reason", sa.Text(), nullable=True))
    op.drop_constraint("ck_invoice_payments_amount", "invoice_payments", type_="check")
    op.create_check_constraint(
        "ck_invoice_payments_amount",
        "invoice_payments",
        "amount_cents > 0 OR (corrects_payment_id IS NOT NULL AND amount_cents = 0)",
    )
    op.create_check_constraint(
        "ck_invoice_payments_correction_reason",
        "invoice_payments",
        "(corrects_payment_id IS NULL) = (correction_reason IS NULL)",
    )

    op.create_table(
        "invoice_refunds",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "invoice_id",
            sa.Uuid(),
            sa.ForeignKey("invoices.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "approved_by",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "refunded_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("amount_cents > 0", name="ck_invoice_refunds_amount"),
    )
    op.create_index("ix_invoice_refunds_invoice", "invoice_refunds", ["invoice_id"])
    op.execute("REVOKE UPDATE, DELETE ON invoice_refunds FROM linsuite_app")
    op.execute(
        """
        CREATE OR REPLACE FUNCTION invoice_refunds_append_only() RETURNS trigger
        SET search_path = pg_catalog, pg_temp
        AS $$
        BEGIN
            IF current_user = 'linsuite_purge'
               OR current_user = (
                   SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid = TG_RELID
               ) THEN
                RETURN CASE TG_OP WHEN 'DELETE' THEN OLD ELSE NEW END;
            END IF;
            RAISE EXCEPTION 'invoice_refunds is append-only: % is not permitted for %',
                TG_OP, current_user
                USING ERRCODE = 'insufficient_privilege';
        END $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER invoice_refunds_no_rewrite
        BEFORE UPDATE OR DELETE ON invoice_refunds
        FOR EACH ROW EXECUTE FUNCTION invoice_refunds_append_only();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS invoice_refunds_no_rewrite ON invoice_refunds")
    op.execute("DROP FUNCTION IF EXISTS invoice_refunds_append_only()")
    op.drop_index("ix_invoice_refunds_invoice", table_name="invoice_refunds")
    op.drop_table("invoice_refunds")
    op.drop_constraint("ck_invoice_payments_correction_reason", "invoice_payments", type_="check")
    op.drop_constraint("ck_invoice_payments_amount", "invoice_payments", type_="check")
    op.create_check_constraint("ck_invoice_payments_amount", "invoice_payments", "amount_cents > 0")
    op.drop_column("invoice_payments", "correction_reason")
    op.drop_column("invoice_payments", "corrects_payment_id")
