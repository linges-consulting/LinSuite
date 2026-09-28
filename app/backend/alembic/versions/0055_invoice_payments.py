"""Manual payment ledger + checkout gate (#66).

Two new append-only tables, both children of `invoices` (#65) — nothing here ever revisits an
already-issued invoice's own money columns, which stay exactly as #65 froze them.

**`invoice_payments`**: one row per recorded payment. Append-only in the fully unconditional
shape `invoice_lines_no_rewrite` (migration 0054) already established for a table that is
itself a financial record with no further-transition exception to carve out — a payment entry
is never edited or voided in place; a correction is a new entry, a later ticket's job (#67),
not this one's. `status IN ('pending', 'received')` is what lets an insurer's externally-
approved-but-unpaid amount get its own row (`payer_type = 'insurer', status = 'pending'`)
without ever counting toward money actually collected — `ck_invoice_payments_pending_only_
insurer` makes `pending` unreachable for a client payment, which is always money in hand the
moment it's recorded. `ck_invoice_payments_payer_method_match` locks `method = 'insurer'` and
`payer_type = 'insurer'` together (each implies the other) so the two columns the acceptance
criteria ask for separately — "payer (client/insurer)" and "method" — can never drift apart,
without actually needing two independent enumerations of the same real-world fact.

**`invoice_balance_authorizations`**: the admin/owner exception, one row per authorization,
deliberately *not* `bill_override_requests`' (#64) staff-request/admin-decide two-step — the
ticket asks for "a single admin action with a reason", so this is exactly that: an admin/owner
holding `billing.manage` (Admin Mode) POSTs a reason directly, and the row exists from that one
call. No staleness guard the way #64's stale-approval check needs one: an invoice's own
`grand_total_cents` is frozen at issue and payments only ever accumulate (append-only, never
reversed here), so the outstanding balance an authorization was granted against can only ever
shrink afterward, never grow past what was reviewed — there is no "the bill moved on"
scenario to guard against. `outstanding_cents_at_authorization` is kept anyway, as the audit
trail's own record of what the admin/owner actually saw and accepted, never re-read by the
gate itself (`billing/payments.py::is_checkout_complete` only checks *whether a row exists*).

Both tables: `REVOKE UPDATE, DELETE` from `linsuite_app`, the purge-role-bypassed append-only
trigger (`..._append_only`/`..._no_rewrite`, verbatim copy of migration 0054's function body).
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
        "invoice_payments",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "invoice_id",
            sa.Uuid(),
            sa.ForeignKey("invoices.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("payer_type", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'received'")),
        sa.Column("method", sa.Text(), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("reference", sa.Text(), nullable=True),
        sa.Column(
            "collected_by",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "recorded_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("amount_cents > 0", name="ck_invoice_payments_amount"),
        sa.CheckConstraint(
            "payer_type IN ('client', 'insurer')", name="ck_invoice_payments_payer_type"
        ),
        sa.CheckConstraint("status IN ('pending', 'received')", name="ck_invoice_payments_status"),
        sa.CheckConstraint(
            "method IN ('cash', 'e_transfer', 'card', 'insurer')", name="ck_invoice_payments_method"
        ),
        sa.CheckConstraint(
            "(payer_type = 'insurer') = (method = 'insurer')",
            name="ck_invoice_payments_payer_method_match",
        ),
        sa.CheckConstraint(
            "status = 'received' OR payer_type = 'insurer'",
            name="ck_invoice_payments_pending_only_insurer",
        ),
    )
    op.create_index("ix_invoice_payments_invoice", "invoice_payments", ["invoice_id"])

    op.create_table(
        "invoice_balance_authorizations",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "invoice_id",
            sa.Uuid(),
            sa.ForeignKey("invoices.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "authorized_by",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("outstanding_cents_at_authorization", sa.Integer(), nullable=False),
        sa.Column(
            "authorized_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "outstanding_cents_at_authorization > 0",
            name="ck_invoice_balance_authorizations_outstanding",
        ),
    )
    op.create_index(
        "ix_invoice_balance_authorizations_invoice",
        "invoice_balance_authorizations",
        ["invoice_id"],
    )

    for table, fn in (
        ("invoice_payments", "invoice_payments_append_only"),
        ("invoice_balance_authorizations", "invoice_balance_authorizations_append_only"),
    ):
        op.execute(f"REVOKE UPDATE, DELETE ON {table} FROM linsuite_app")
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
        ("invoice_payments", "invoice_payments_append_only"),
        ("invoice_balance_authorizations", "invoice_balance_authorizations_append_only"),
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {table}_no_rewrite ON {table}")
        op.execute(f"DROP FUNCTION IF EXISTS {fn}()")

    op.drop_index(
        "ix_invoice_balance_authorizations_invoice", table_name="invoice_balance_authorizations"
    )
    op.drop_table("invoice_balance_authorizations")
    op.drop_index("ix_invoice_payments_invoice", table_name="invoice_payments")
    op.drop_table("invoice_payments")
