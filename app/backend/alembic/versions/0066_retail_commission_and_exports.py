"""Retail commission, the package-refund commission choice, async CSV exports (M4 review T5).

- `commission_postings` references exactly one of an `invoice_lines` or a `retail_invoice_lines`
  row (and its invoice) — the #76 ledger generalization (nullable FK pair + CHECK). The 0057
  append-only grant and trigger are untouched.
- `invoice_refunds.reverses_commission`: a package refund's explicit "reverse commission"
  choice (#73), persisted so a redeemed session billed *after* the refund never earns (R28).
- `commission_exports`: one queued CSV export of the commission report, built by a Celery task
  (CLAUDE.md: exports never run inline in a request).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0066"
down_revision: str | None = "0065"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

POSTINGS = "commission_postings"


def upgrade() -> None:
    op.alter_column(POSTINGS, "invoice_line_id", nullable=True)
    op.alter_column(POSTINGS, "invoice_id", nullable=True)
    op.add_column(
        POSTINGS,
        sa.Column(
            "retail_invoice_line_id",
            sa.Uuid(),
            sa.ForeignKey("retail_invoice_lines.id", ondelete="RESTRICT"),
            nullable=True,
        ),
    )
    op.add_column(
        POSTINGS,
        sa.Column(
            "retail_invoice_id",
            sa.Uuid(),
            sa.ForeignKey("retail_invoices.id", ondelete="RESTRICT"),
            nullable=True,
        ),
    )
    op.create_check_constraint(
        f"ck_{POSTINGS}_one_line",
        POSTINGS,
        "(invoice_line_id IS NOT NULL AND invoice_id IS NOT NULL "
        "AND retail_invoice_line_id IS NULL AND retail_invoice_id IS NULL) OR "
        "(invoice_line_id IS NULL AND invoice_id IS NULL "
        "AND retail_invoice_line_id IS NOT NULL AND retail_invoice_id IS NOT NULL)",
    )
    op.create_index(f"ix_{POSTINGS}_retail_invoice", POSTINGS, ["retail_invoice_id"])

    op.add_column(
        "invoice_refunds",
        sa.Column("reverses_commission", sa.Boolean(), nullable=False, server_default=sa.false()),
    )

    op.create_table(
        "commission_exports",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "requested_by",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("from_date", sa.Date(), nullable=False),
        sa.Column("to_date", sa.Date(), nullable=False),
        # A filter value, not a relationship — no FK, so a report on since-removed staff works.
        sa.Column("staff_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'ready', 'failed')", name="ck_commission_exports_status"
        ),
    )


def downgrade() -> None:
    op.drop_table("commission_exports")
    op.drop_column("invoice_refunds", "reverses_commission")
    # The purge role owns nothing here; the migrating owner bypasses the append-only trigger.
    op.execute(f"DELETE FROM {POSTINGS} WHERE retail_invoice_line_id IS NOT NULL")
    op.drop_index(f"ix_{POSTINGS}_retail_invoice", table_name=POSTINGS)
    op.drop_constraint(f"ck_{POSTINGS}_one_line", POSTINGS)
    op.drop_column(POSTINGS, "retail_invoice_id")
    op.drop_column(POSTINGS, "retail_invoice_line_id")
    op.alter_column(POSTINGS, "invoice_id", nullable=False)
    op.alter_column(POSTINGS, "invoice_line_id", nullable=False)
