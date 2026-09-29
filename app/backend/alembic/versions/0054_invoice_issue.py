"""Invoice issue: gapless numbering, frozen snapshot, voidable immutability (#65).

Revision ID: 0054
Revises: 0053
Create Date: 2026-09-28

Turns a reviewed, approved draft `ServiceBill` into an issued, immutable `Invoice` — a
wholly new table pair, not a `service_bills.status` flip with a parallel snapshot table.
CLAUDE.md's own document-class split ("service bill" is draft/mutable, "invoice" is
voidable/issued-once) already argued for this in `billing/models.py::ServiceBill`'s own
docstring; keeping the draft table exactly as #59/#63/#64 left it means none of their code,
tests or grants change here.

**Gapless numbering**: `business_invoice_counters` is a per-business counter row, not a
`SEQUENCE` — a `SEQUENCE` burns a value on every rolled-back transaction, which is exactly
the gap this ticket must not have. `billing/invoice_numbering.py::allocate_invoice_number`
locks the row (`SELECT ... FOR UPDATE`) and increments it in the *same* transaction as the
invoice's own insert; a rollback anywhere in that transaction rolls the increment back with
it, so no number is ever burned, and the row lock is what serializes two concurrent issue
attempts on the same business (CLAUDE.md "Concurrency: enforce in the DB, not app locks").

**Snapshot tables**: `invoice_lines` (one per `service_bill_line`, price/discount/tax already
resolved and frozen), `invoice_line_discounts` (which predefined discounts applied to that
line and their commission-basis choice, frozen), `invoice_line_taxes` (which tax component/
rate applied and what it contributed, frozen). None of the three ever re-join `services`,
`discounts` or `tax_components` for money — `billing/invoices.py` reads only these rows.

**Immutability, the *voidable* shape** (CLAUDE.md "Document storage and immutability"):
narrower than `business_document_keys_guard` (0048, unconditional) or
`stock_movements_no_rewrite` (0050, unconditional) — both of which refuse every UPDATE. An
invoice must allow exactly one further transition: `status` flips `'issued'` -> `'cancelled'`
with `cancelled_at`/`cancelled_by`/`cancel_reason` newly set, every money/snapshot/number
column held bit-for-bit identical. `invoices_voidable_guard` checks that shape explicitly,
row by row, and refuses anything else (including the app role's own UPDATE grant, which stays
in place *because* that one transition must remain reachable — unlike the two unconditional
precedents, DELETE is still fully revoked). `invoice_lines`/`invoice_line_discounts`/
`invoice_line_taxes` get the unconditional shape instead: cancelling an invoice never touches
its frozen child rows, so there is no narrow exception to carve out for them.

No cancel/void *route* is built in this ticket (not in #65's acceptance criteria) — the
schema and trigger are ready for whichever later ticket adds one.

`service_bills` gains one nullable column, `override_applied_revision`: the checkpoint that
makes "stale approval" decidable at issue time. `bill_authority.py`'s two override write sites
(approved staff-request decision, inline admin edit) now stamp it with the exact same
`datetime.now(UTC)` value they stamp `updated_at` with; `bill_review.py::apply_discounts`
clears it alongside the override fields it already clears. At issue time,
`bill.manual_override_cents is not None and bill.override_applied_revision != bill.updated_at`
is "stale" — the bill moved on (typically: a sibling appointment completed into it) after the
override was authorized, without the override being redecided. If `manual_override_cents` is
set while `override_applied_revision` is `None`, that is the other refusal case the ticket
asks for ("required admin authorization... hasn't actually happened") — given the only two
writers of `manual_override_cents` also always set this column in the same statement, that
state should be unreachable in practice; the check exists anyway, as a belt-and-braces read of
"was this actually authorized," not an inference from a currently-correct write path.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0054"
down_revision: str | None = "0053"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "service_bills",
        sa.Column("override_applied_revision", sa.DateTime(timezone=True), nullable=True),
    )

    # --- gapless per-business invoice counter --------------------------------------------------
    op.create_table(
        "business_invoice_counters",
        sa.Column("business_id", sa.Integer(), sa.ForeignKey("businesses.id"), primary_key=True),
        sa.Column("next_number", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.CheckConstraint("next_number >= 1", name="ck_business_invoice_counters_next_number"),
    )

    # --- the issued invoice itself ------------------------------------------------------------
    op.create_table(
        "invoices",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("business_id", sa.Integer(), sa.ForeignKey("businesses.id"), nullable=False),
        sa.Column("invoice_number", sa.Integer(), nullable=False),
        sa.Column(
            "service_bill_id",
            sa.Uuid(),
            sa.ForeignKey("service_bills.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "customer_id",
            sa.Uuid(),
            sa.ForeignKey("customers.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'issued'")),
        # The ordinarily-computed numbers, frozen — always present.
        sa.Column("computed_subtotal_cents", sa.Integer(), nullable=False),
        sa.Column("computed_discount_total_cents", sa.Integer(), nullable=False),
        sa.Column("computed_tax_total_cents", sa.Integer(), nullable=False),
        sa.Column("computed_grand_total_cents", sa.Integer(), nullable=False),
        # `JSONB`, not `JSON` — `json` has no equality operator in Postgres, and
        # `invoices_voidable_guard` below needs `NEW.tax_totals_by_component =
        # OLD.tax_totals_by_component` to work.
        sa.Column("tax_totals_by_component", postgresql.JSONB(), nullable=False),
        # An admin/owner-authorized override (#64), frozen alongside — never replacing —
        # the computed numbers above, the same "reported, not conflated" shape `BillOut`
        # already established.
        sa.Column("override_applied_cents", sa.Integer(), nullable=True),
        sa.Column("override_reason", sa.Text(), nullable=True),
        # The one number actually billed: `override_applied_cents` if this invoice was issued
        # under one, else `computed_grand_total_cents`.
        sa.Column("grand_total_cents", sa.Integer(), nullable=False),
        sa.Column(
            "issued_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "issued_by", sa.Uuid(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        # Voidable: cancel sets status + links a replacement (the replacement invoice's own
        # `replaces_invoice_id`, set at its own insert — never a second column here).
        sa.Column("replaces_invoice_id", sa.Uuid(), sa.ForeignKey("invoices.id"), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "cancelled_by",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("cancel_reason", sa.Text(), nullable=True),
        sa.UniqueConstraint("business_id", "invoice_number", name="uq_invoices_business_number"),
        sa.UniqueConstraint("service_bill_id", name="uq_invoices_service_bill_id"),
        sa.CheckConstraint("status IN ('issued', 'cancelled')", name="ck_invoices_status"),
        sa.CheckConstraint(
            "(status = 'issued' AND cancelled_at IS NULL AND cancelled_by IS NULL "
            "AND cancel_reason IS NULL) OR "
            "(status = 'cancelled' AND cancelled_at IS NOT NULL AND cancelled_by IS NOT NULL "
            "AND cancel_reason IS NOT NULL)",
            name="ck_invoices_cancel_fields",
        ),
        sa.CheckConstraint(
            "(override_applied_cents IS NULL) = (override_reason IS NULL)",
            name="ck_invoices_override_fields",
        ),
    )
    op.create_index("ix_invoices_customer", "invoices", ["customer_id"])

    op.create_table(
        "invoice_lines",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "invoice_id",
            sa.Uuid(),
            sa.ForeignKey("invoices.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "service_bill_line_id",
            sa.Uuid(),
            sa.ForeignKey("service_bill_lines.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "appointment_id",
            sa.Uuid(),
            sa.ForeignKey("appointments.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        # Display/reference only, per the module docstring — never re-joined for money.
        sa.Column(
            "service_id",
            sa.Uuid(),
            sa.ForeignKey("services.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "staff_id", sa.Uuid(), sa.ForeignKey("staff.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column("price_cents", sa.Integer(), nullable=False),
        sa.Column("commission_rate_bp", sa.Integer(), nullable=False),
        sa.Column("discounted_cents", sa.Integer(), nullable=False),
        sa.Column("pretax_cents", sa.Integer(), nullable=False),
        sa.Column("tax_cents", sa.Integer(), nullable=False),
        sa.Column("line_total_cents", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("price_cents >= 0", name="ck_invoice_lines_price"),
        sa.CheckConstraint(
            "commission_rate_bp BETWEEN 0 AND 10000", name="ck_invoice_lines_commission_bp"
        ),
        sa.UniqueConstraint("service_bill_line_id", name="uq_invoice_lines_service_bill_line_id"),
    )
    op.create_index("ix_invoice_lines_invoice", "invoice_lines", ["invoice_id"])

    op.create_table(
        "invoice_line_discounts",
        sa.Column(
            "invoice_line_id",
            sa.Uuid(),
            sa.ForeignKey("invoice_lines.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "discount_id",
            sa.Uuid(),
            sa.ForeignKey("discounts.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("discount_name", sa.Text(), nullable=False),
        sa.Column("discount_kind", sa.Text(), nullable=False),
        sa.Column("commission_basis", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "discount_kind IN ('percentage', 'fixed')", name="ck_invoice_line_discounts_kind"
        ),
        sa.CheckConstraint(
            "commission_basis IN ('reduces', 'absorbed')",
            name="ck_invoice_line_discounts_commission_basis",
        ),
    )

    op.create_table(
        "invoice_line_taxes",
        sa.Column(
            "invoice_line_id",
            sa.Uuid(),
            sa.ForeignKey("invoice_lines.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        # Frozen text, not an FK to `tax_components.id` — independent of the live row on
        # purpose (module docstring: never re-joined).
        sa.Column("component_code", sa.String(16), primary_key=True),
        sa.Column("rate_bp", sa.Integer(), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.CheckConstraint("rate_bp BETWEEN 0 AND 10000", name="ck_invoice_line_taxes_rate_bp"),
    )

    # --- grants + the voidable-shape trigger --------------------------------------------------
    op.execute("REVOKE DELETE ON invoices FROM linsuite_app")
    op.execute("REVOKE UPDATE, DELETE ON invoice_lines FROM linsuite_app")
    op.execute("REVOKE UPDATE, DELETE ON invoice_line_discounts FROM linsuite_app")
    op.execute("REVOKE UPDATE, DELETE ON invoice_line_taxes FROM linsuite_app")

    op.execute(
        """
        CREATE OR REPLACE FUNCTION invoices_voidable_guard() RETURNS trigger
        SET search_path = pg_catalog, pg_temp
        AS $$
        BEGIN
            -- The purge role bypasses too (no job purges `invoices` in v1 — nothing in this
            -- wave builds one against `retain_until`'s equivalent here — but the test suite
            -- resets state via `get_purge_engine()`, the same `stock_movements` precedent).
            IF current_user = 'linsuite_purge'
               OR current_user = (
                   SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid = TG_RELID
               ) THEN
                RETURN CASE TG_OP WHEN 'DELETE' THEN OLD ELSE NEW END;
            END IF;
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'invoices: DELETE is not permitted for %', current_user
                    USING ERRCODE = 'insufficient_privilege';
            END IF;
            -- The one permitted transition: issued -> cancelled, cancel fields newly set,
            -- every money/snapshot/number/identity column held bit-for-bit identical.
            IF OLD.status = 'issued' AND NEW.status = 'cancelled'
               AND NEW.id = OLD.id
               AND NEW.business_id = OLD.business_id
               AND NEW.invoice_number = OLD.invoice_number
               AND NEW.service_bill_id = OLD.service_bill_id
               AND NEW.customer_id = OLD.customer_id
               AND NEW.computed_subtotal_cents = OLD.computed_subtotal_cents
               AND NEW.computed_discount_total_cents = OLD.computed_discount_total_cents
               AND NEW.computed_tax_total_cents = OLD.computed_tax_total_cents
               AND NEW.computed_grand_total_cents = OLD.computed_grand_total_cents
               AND NEW.tax_totals_by_component = OLD.tax_totals_by_component
               AND NEW.override_applied_cents IS NOT DISTINCT FROM OLD.override_applied_cents
               AND NEW.override_reason IS NOT DISTINCT FROM OLD.override_reason
               AND NEW.grand_total_cents = OLD.grand_total_cents
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
                'invoices: this update is not the permitted voidable transition (for %)',
                current_user
                USING ERRCODE = 'insufficient_privilege';
        END $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER invoices_voidable_guard
        BEFORE UPDATE OR DELETE ON invoices
        FOR EACH ROW EXECUTE FUNCTION invoices_voidable_guard();
        """
    )

    for table, fn in (
        ("invoice_lines", "invoice_lines_append_only"),
        ("invoice_line_discounts", "invoice_line_discounts_append_only"),
        ("invoice_line_taxes", "invoice_line_taxes_append_only"),
    ):
        op.execute(
            f"""
            CREATE OR REPLACE FUNCTION {fn}() RETURNS trigger
            SET search_path = pg_catalog, pg_temp
            AS $$
            BEGIN
                IF current_user = 'linsuite_purge'
                   OR current_user = (
                       SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid = TG_RELID
                   ) THEN
                    RETURN CASE TG_OP WHEN 'DELETE' THEN OLD ELSE NEW END;
                END IF;
                RAISE EXCEPTION '{table} is append-only: % is not permitted for %',
                    TG_OP, current_user
                    USING ERRCODE = 'insufficient_privilege';
            END $$ LANGUAGE plpgsql;
            """
        )
        op.execute(
            f"""
            CREATE TRIGGER {table}_no_rewrite
            BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION {fn}();
            """
        )


def downgrade() -> None:
    for table, fn in (
        ("invoice_lines", "invoice_lines_append_only"),
        ("invoice_line_discounts", "invoice_line_discounts_append_only"),
        ("invoice_line_taxes", "invoice_line_taxes_append_only"),
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {table}_no_rewrite ON {table}")
        op.execute(f"DROP FUNCTION IF EXISTS {fn}()")
    op.execute("DROP TRIGGER IF EXISTS invoices_voidable_guard ON invoices")
    op.execute("DROP FUNCTION IF EXISTS invoices_voidable_guard()")

    op.drop_table("invoice_line_taxes")
    op.drop_table("invoice_line_discounts")
    op.drop_index("ix_invoice_lines_invoice", table_name="invoice_lines")
    op.drop_table("invoice_lines")
    op.drop_index("ix_invoices_customer", table_name="invoices")
    op.drop_table("invoices")
    op.drop_table("business_invoice_counters")
    op.drop_column("service_bills", "override_applied_revision")
