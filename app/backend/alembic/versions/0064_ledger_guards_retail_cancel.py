"""Ledger guards + retail balance exceptions + retail cancel/replace (M4 review T2).

- `invoice_payments` / `invoice_balance_authorizations`: a BEFORE INSERT trigger refuses a
  row whose invoice (service or retail) is not `issued`. It reads the invoice `FOR SHARE`, so an
  insert racing an uncommitted cancel waits for it and then sees `cancelled` — the database, not
  only the app's `lock_lineage`, keeps money off a cancelled invoice (R7).
- `invoice_balance_authorizations` references exactly one of `invoices` / `retail_invoices`,
  the same nullable-pair shape 0063 gave payments and refunds (R8).
- `invoice_payment_transfers` likewise, so a retail replacement carries its original's money (R15).
- `retail_sales.replaces_retail_invoice_id`: cancelling a retail invoice opens a *new* draft sale
  naming the invoice it replaces; issuing that draft links `retail_invoices.replaces_invoice_id`.
  A new draft (not the original sale reopened) keeps every frozen `retail_invoice_lines ->
  retail_sale_lines` reference intact while staff edit the replacement cart freely.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0064"
down_revision: str | None = "0063"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

GUARDED = ("invoice_payments", "invoice_balance_authorizations")
AUTH = "invoice_balance_authorizations"
TRANSFERS = "invoice_payment_transfers"


def upgrade() -> None:
    op.alter_column(AUTH, "invoice_id", nullable=True)
    op.add_column(
        AUTH,
        sa.Column(
            "retail_invoice_id",
            sa.Uuid(),
            sa.ForeignKey("retail_invoices.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )
    op.create_check_constraint(
        f"ck_{AUTH}_one_invoice", AUTH, "num_nonnulls(invoice_id, retail_invoice_id) = 1"
    )
    op.create_index(f"ix_{AUTH}_retail_invoice", AUTH, ["retail_invoice_id"])

    for end in ("from", "to"):
        op.alter_column(TRANSFERS, f"{end}_invoice_id", nullable=True)
        op.add_column(
            TRANSFERS,
            sa.Column(
                f"{end}_retail_invoice_id",
                sa.Uuid(),
                sa.ForeignKey("retail_invoices.id", ondelete="RESTRICT"),
                nullable=True,
                unique=True,
            ),
        )
    op.create_check_constraint(
        f"ck_{TRANSFERS}_one_kind",
        TRANSFERS,
        "(from_invoice_id IS NOT NULL AND to_invoice_id IS NOT NULL "
        "AND from_retail_invoice_id IS NULL AND to_retail_invoice_id IS NULL) OR "
        "(from_invoice_id IS NULL AND to_invoice_id IS NULL "
        "AND from_retail_invoice_id IS NOT NULL AND to_retail_invoice_id IS NOT NULL)",
    )

    op.add_column(
        "retail_sales",
        sa.Column(
            "replaces_retail_invoice_id",
            sa.Uuid(),
            sa.ForeignKey("retail_invoices.id", ondelete="SET NULL"),
            nullable=True,
            unique=True,
        ),
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION ledger_requires_issued_invoice() RETURNS trigger
        SET search_path = pg_catalog, pg_temp
        AS $$
        DECLARE
            invoice_status text;
        BEGIN
            IF NEW.invoice_id IS NOT NULL THEN
                SELECT status INTO invoice_status FROM public.invoices
                WHERE id = NEW.invoice_id FOR SHARE;
            ELSE
                SELECT status INTO invoice_status FROM public.retail_invoices
                WHERE id = NEW.retail_invoice_id FOR SHARE;
            END IF;
            IF invoice_status IS DISTINCT FROM 'issued' THEN
                RAISE EXCEPTION '%: the invoice is not issued (status %)',
                    TG_TABLE_NAME, invoice_status
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END $$ LANGUAGE plpgsql;
        """
    )
    for table in GUARDED:
        op.execute(
            f"""
            CREATE TRIGGER {table}_issued_only
            BEFORE INSERT ON {table}
            FOR EACH ROW EXECUTE FUNCTION ledger_requires_issued_invoice();
            """
        )


def downgrade() -> None:
    for table in GUARDED:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_issued_only ON {table}")
    op.execute("DROP FUNCTION IF EXISTS ledger_requires_issued_invoice()")
    op.drop_column("retail_sales", "replaces_retail_invoice_id")

    op.execute(f"DELETE FROM {TRANSFERS} WHERE from_retail_invoice_id IS NOT NULL")
    op.drop_constraint(f"ck_{TRANSFERS}_one_kind", TRANSFERS, type_="check")
    for end in ("from", "to"):
        op.drop_column(TRANSFERS, f"{end}_retail_invoice_id")
        op.alter_column(TRANSFERS, f"{end}_invoice_id", nullable=False)

    op.execute(f"DELETE FROM {AUTH} WHERE retail_invoice_id IS NOT NULL")
    op.drop_index(f"ix_{AUTH}_retail_invoice", table_name=AUTH)
    op.drop_constraint(f"ck_{AUTH}_one_invoice", AUTH, type_="check")
    op.drop_column(AUTH, "retail_invoice_id")
    op.alter_column(AUTH, "invoice_id", nullable=False)
