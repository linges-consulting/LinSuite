"""Package credit redemption at appointment completion (#72).

Revision ID: 0059
Revises: 0058
Create Date: 2026-09-28

`package_credit_redemptions`: the append-only redemption ledger — one row per credit spent,
written inside `complete_appointment`'s own transaction (`billing/redemption.py`). Remaining
credits are never a stored counter: `credits_total` (frozen, #71) minus the rows here.

**Race safety, enforced in the database** (CLAUDE.md "enforce in the DB, not in app locks").
Each redemption takes the next `sequence` (1-based) for its `(package_purchase_id,
service_id)` credit; `uq_package_credit_redemptions_sequence` makes the same credit unspendable
twice, and the insert guard refuses a `sequence` past `credits_total` or against a purchase
whose credits are not activated (#71: fully paid). So two racing completions for the last
credit can never both land, even without the app's own `FOR UPDATE` on the purchase row
(which is only there to turn the loser into a clean 409 instead of a unique violation).

Append-only in the `commission_postings` (0057) shape: app role INSERT/SELECT only; the trigger
lets the purge role and the owner through.

`service_bill_lines.prepaid_cents` / `invoice_lines.prepaid_cents`: the frozen per-session
value a redeemed visit is settled by — never a new charge to collect (`billing/payments.py::
balances` subtracts it).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0059"
down_revision: str | None = "0058"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "package_credit_redemptions",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("package_purchase_id", sa.Uuid(), nullable=False),
        sa.Column("service_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.SmallInteger(), nullable=False),
        sa.Column(
            "appointment_id",
            sa.Uuid(),
            sa.ForeignKey("appointments.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("value_cents", sa.Integer(), nullable=False),
        sa.Column(
            "redeemed_by",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "redeemed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["package_purchase_id", "service_id"],
            [
                "package_purchase_credits.package_purchase_id",
                "package_purchase_credits.service_id",
            ],
            name="fk_package_credit_redemptions_credit",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("sequence >= 1", name="ck_package_credit_redemptions_sequence"),
        sa.CheckConstraint("value_cents >= 0", name="ck_package_credit_redemptions_value"),
        sa.UniqueConstraint(
            "package_purchase_id",
            "service_id",
            "sequence",
            name="uq_package_credit_redemptions_sequence",
        ),
        sa.UniqueConstraint("appointment_id", name="uq_package_credit_redemptions_appointment"),
    )

    op.execute("REVOKE UPDATE, DELETE ON package_credit_redemptions FROM linsuite_app")
    op.execute(
        """
        CREATE OR REPLACE FUNCTION package_credit_redemptions_guard() RETURNS trigger
        SET search_path = pg_catalog, pg_temp
        AS $$
        DECLARE
            total smallint;
            activated boolean;
        BEGIN
            IF TG_OP = 'INSERT' THEN
                SELECT c.credits_total, p.credits_activated INTO total, activated
                  FROM public.package_purchase_credits c
                  JOIN public.package_purchases p ON p.id = c.package_purchase_id
                 WHERE c.package_purchase_id = NEW.package_purchase_id
                   AND c.service_id = NEW.service_id;
                IF NOT activated THEN
                    RAISE EXCEPTION 'package purchase % is not activated', NEW.package_purchase_id
                        USING ERRCODE = 'check_violation';
                END IF;
                IF NEW.sequence > total THEN
                    RAISE EXCEPTION 'no credit left to redeem (% of %)', NEW.sequence, total
                        USING ERRCODE = 'check_violation';
                END IF;
                RETURN NEW;
            END IF;
            IF current_user = 'linsuite_purge'
               OR current_user = (
                   SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid = TG_RELID
               ) THEN
                RETURN CASE TG_OP WHEN 'DELETE' THEN OLD ELSE NEW END;
            END IF;
            RAISE EXCEPTION 'package_credit_redemptions is append-only: % is not permitted for %',
                TG_OP, current_user
                USING ERRCODE = 'insufficient_privilege';
        END $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER package_credit_redemptions_guard
        BEFORE INSERT OR UPDATE OR DELETE ON package_credit_redemptions
        FOR EACH ROW EXECUTE FUNCTION package_credit_redemptions_guard();
        """
    )

    op.add_column(
        "service_bill_lines",
        sa.Column("prepaid_cents", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.create_check_constraint(
        "ck_service_bill_lines_prepaid",
        "service_bill_lines",
        "prepaid_cents >= 0 AND prepaid_cents <= price_cents",
    )
    op.add_column(
        "invoice_lines",
        sa.Column("prepaid_cents", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.create_check_constraint(
        "ck_invoice_lines_prepaid",
        "invoice_lines",
        "prepaid_cents >= 0 AND prepaid_cents <= line_total_cents",
    )


def downgrade() -> None:
    op.drop_constraint("ck_invoice_lines_prepaid", "invoice_lines")
    op.drop_column("invoice_lines", "prepaid_cents")
    op.drop_constraint("ck_service_bill_lines_prepaid", "service_bill_lines")
    op.drop_column("service_bill_lines", "prepaid_cents")
    op.execute(
        "DROP TRIGGER IF EXISTS package_credit_redemptions_guard ON package_credit_redemptions"
    )
    op.execute("DROP FUNCTION IF EXISTS package_credit_redemptions_guard()")
    op.drop_table("package_credit_redemptions")
