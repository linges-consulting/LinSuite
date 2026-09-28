"""Commission ledger + report capability (#69).

Revision ID: 0055
Revises: 0054
Create Date: 2026-09-28

`commission_postings`: one append-only row per invoice line, posted in the same transaction as
invoice issue (`billing/invoices.py::issue_invoice`) — the rate already snapshotted at
completion (#59), the basis computed by `billing/commission.py`'s pure formula from
`InvoiceLineDiscount.commission_basis` (#65), never anything read live. Append-only, the exact
`stock_movements` (0050) / `invoice_lines` (0054) shape: the app role may INSERT and SELECT and
nothing else; a correction is a second row (`kind="reversal"`, a negative `amount_cents`, its
own `posted_at`), never an UPDATE — see `billing/models.py`'s `## commission posting + report
(#69)` section for the full design, including why this shape is what #67/#68/#73's refund
tickets are expected to follow.

`commission.view`: a new capability, Administrator-only, Admin Mode — `audit.view`'s own shape
(0022), seeded to the Administrator role only, never Staff.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0055"
down_revision: str | None = "0054"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "commission_postings",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "invoice_line_id",
            sa.Uuid(),
            sa.ForeignKey("invoice_lines.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "invoice_id",
            sa.Uuid(),
            sa.ForeignKey("invoices.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "staff_id", sa.Uuid(), sa.ForeignKey("staff.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column("kind", sa.String(16), nullable=False, server_default=sa.text("'earned'")),
        sa.Column("commission_rate_bp", sa.Integer(), nullable=False),
        sa.Column("basis_cents", sa.Integer(), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column(
            "reverses_posting_id",
            sa.Uuid(),
            sa.ForeignKey("commission_postings.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "posted_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("kind IN ('earned', 'reversal')", name="ck_commission_postings_kind"),
        sa.CheckConstraint(
            "commission_rate_bp BETWEEN 0 AND 10000", name="ck_commission_postings_rate_bp"
        ),
        sa.CheckConstraint("basis_cents >= 0", name="ck_commission_postings_basis"),
        sa.CheckConstraint(
            "(kind = 'earned' AND amount_cents >= 0) OR (kind = 'reversal' AND amount_cents <= 0)",
            name="ck_commission_postings_amount_sign",
        ),
    )
    op.create_index(
        "ix_commission_postings_staff_posted", "commission_postings", ["staff_id", "posted_at"]
    )
    op.create_index("ix_commission_postings_invoice", "commission_postings", ["invoice_id"])

    # --- append-only: the app role may INSERT and SELECT, nothing else -------------------------
    op.execute("REVOKE UPDATE, DELETE ON commission_postings FROM linsuite_app")
    op.execute(
        """
        CREATE OR REPLACE FUNCTION commission_postings_no_rewrite() RETURNS trigger
        SET search_path = pg_catalog, pg_temp
        AS $$
        BEGIN
            IF current_user = 'linsuite_purge'
               OR current_user = (
                   SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid = TG_RELID
               ) THEN
                RETURN CASE TG_OP WHEN 'DELETE' THEN OLD ELSE NEW END;
            END IF;
            RAISE EXCEPTION 'commission_postings is append-only: % is not permitted for %',
                TG_OP, current_user
                USING ERRCODE = 'insufficient_privilege';
        END $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER commission_postings_no_rewrite
        BEFORE UPDATE OR DELETE ON commission_postings
        FOR EACH ROW EXECUTE FUNCTION commission_postings_no_rewrite();
        """
    )

    # --- `commission.view`: Administrator-only, Admin Mode -------------------------------------
    op.execute(
        "INSERT INTO role_capabilities (role_id, capability) "
        "SELECT id, 'commission.view' FROM roles WHERE name = 'Administrator' AND is_system "
        "ON CONFLICT DO NOTHING"
    )


def downgrade() -> None:
    op.execute("DELETE FROM role_capabilities WHERE capability = 'commission.view'")
    op.execute("DROP TRIGGER IF EXISTS commission_postings_no_rewrite ON commission_postings")
    op.execute("DROP FUNCTION IF EXISTS commission_postings_no_rewrite()")
    op.drop_index("ix_commission_postings_invoice", table_name="commission_postings")
    op.drop_index("ix_commission_postings_staff_posted", table_name="commission_postings")
    op.drop_table("commission_postings")
