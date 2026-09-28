"""Cancel & replace an issued invoice (#68).

Cancelling reopens the original's `ServiceBill` as the replacement draft; issuing that draft
again produces a second `Invoice` for the same bill, with `replaces_invoice_id` naming the
cancelled one. Three uniqueness rules change to allow exactly that and nothing more:

- `invoices.service_bill_id`: unique only among *live* (`status = 'issued'`) invoices — a bill
  still has at most one live invoice at a time, cancelled predecessors are retained beside it.
- `invoices.replaces_invoice_id`: unique — an original is replaced at most once (one lineage).
- `invoice_lines.service_bill_line_id`: unique per invoice rather than globally, since the
  replacement freezes the same bill lines again.

`invoices_voidable_guard` is untouched: the replacement link is written at the replacement's
own INSERT, and cancel is the issued -> cancelled transition it already permits.

`invoice_payment_transfers`: one append-only row per replacement, moving the original's
ledger sums (received, received-from-insurer, pending insurer) onto it — the payment rows
themselves are never edited. `billing/payments.py::balances()` adds/subtracts these.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0059"
down_revision: str | None = "0058"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE, FN = "invoice_payment_transfers", "invoice_payment_transfers_append_only"


def upgrade() -> None:
    op.drop_constraint("uq_invoices_service_bill_id", "invoices", type_="unique")
    op.create_index(
        "ux_invoices_service_bill_live",
        "invoices",
        ["service_bill_id"],
        unique=True,
        postgresql_where=sa.text("status = 'issued'"),
    )
    op.create_unique_constraint(
        "uq_invoices_replaces_invoice_id", "invoices", ["replaces_invoice_id"]
    )
    op.drop_constraint("uq_invoice_lines_service_bill_line_id", "invoice_lines", type_="unique")
    op.create_unique_constraint(
        "uq_invoice_lines_invoice_bill_line",
        "invoice_lines",
        ["invoice_id", "service_bill_line_id"],
    )

    op.create_table(
        TABLE,
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "from_invoice_id",
            sa.Uuid(),
            sa.ForeignKey("invoices.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "to_invoice_id",
            sa.Uuid(),
            sa.ForeignKey("invoices.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("received_cents", sa.Integer(), nullable=False),
        sa.Column("received_insurer_cents", sa.Integer(), nullable=False),
        sa.Column("pending_insurer_cents", sa.Integer(), nullable=False),
        sa.Column(
            "transferred_by",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "transferred_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("from_invoice_id", name="uq_invoice_payment_transfers_from"),
        sa.UniqueConstraint("to_invoice_id", name="uq_invoice_payment_transfers_to"),
        sa.CheckConstraint(
            "received_cents >= 0 AND received_insurer_cents >= 0 AND pending_insurer_cents >= 0",
            name="ck_invoice_payment_transfers_amounts",
        ),
    )

    op.execute(f"REVOKE UPDATE, DELETE ON {TABLE} FROM linsuite_app")
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION {FN}() RETURNS trigger
        SET search_path = pg_catalog, pg_temp
        AS $$
        BEGIN
            IF current_user = 'linsuite_purge'
               OR current_user = (
                   SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid = TG_RELID
               ) THEN
                RETURN CASE TG_OP WHEN 'DELETE' THEN OLD ELSE NEW END;
            END IF;
            RAISE EXCEPTION '{TABLE} is append-only: % is not permitted for %',
                TG_OP, current_user
                USING ERRCODE = 'insufficient_privilege';
        END $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER {TABLE}_no_rewrite
        BEFORE UPDATE OR DELETE ON {TABLE}
        FOR EACH ROW EXECUTE FUNCTION {FN}();
        """
    )


def downgrade() -> None:
    op.execute(f"DROP TRIGGER IF EXISTS {TABLE}_no_rewrite ON {TABLE}")
    op.execute(f"DROP FUNCTION IF EXISTS {FN}()")
    op.drop_table(TABLE)
    op.drop_constraint("uq_invoice_lines_invoice_bill_line", "invoice_lines", type_="unique")
    op.create_unique_constraint(
        "uq_invoice_lines_service_bill_line_id", "invoice_lines", ["service_bill_line_id"]
    )
    op.drop_constraint("uq_invoices_replaces_invoice_id", "invoices", type_="unique")
    op.drop_index("ux_invoices_service_bill_live", table_name="invoices")
    op.create_unique_constraint("uq_invoices_service_bill_id", "invoices", ["service_bill_id"])
