"""Package/bundle purchase: frozen snapshot, credit-activation hook, reused invoice
numbering (#71).

Revision ID: 0055
Revises: 0054
Create Date: 2026-09-28

Buying a `PackageDefinition` (#60) is its own invoice, issued through the exact same
`invoices`/`business_invoice_counters` machinery #65 built — no second counter, no second
`Invoice` table. `invoices.service_bill_id` becomes nullable and gains a sibling,
`package_purchase_id`; `ck_invoices_source_xor` pins exactly one of the two to be set on every
row. See `billing/models.py`'s `## package/bundle purchase (#71)` section for the full
rationale, in particular why this is a distinct `package_purchases`/`package_purchase_credits`
pair rather than forcing a package purchase through `invoice_lines` (that table's `appointment_
id`/`staff_id`/`service_bill_line_id` are all `NOT NULL`, none of which a package purchase has).

`package_purchases` gets the same voidable-adjacent DB-enforced shape `invoices` itself has
(0054): `package_purchases_activation_guard` permits exactly one transition —
`credits_activated` `false` -> `true`, `activated_at` newly set, everything else identical —
and refuses every other UPDATE/DELETE. This is the database-level twin of the ticket's own
central rule, "a partial payment activates nothing": nothing (not even a future bug) can flip
usable credits on by any path other than that one narrow, logged transition.
`package_purchase_credits` is fully append-only, `invoice_lines`'s own precedent.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0055"
down_revision: str | None = "0054"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- the purchase snapshot, and its credited services --------------------------------------
    op.create_table(
        "package_purchases",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "package_definition_id",
            sa.Uuid(),
            sa.ForeignKey("package_definitions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "customer_id",
            sa.Uuid(),
            sa.ForeignKey("customers.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("price_cents", sa.Integer(), nullable=False),
        sa.Column("expires_after_days", sa.SmallInteger(), nullable=True),
        sa.Column("expires_at", sa.Date(), nullable=True),
        sa.Column(
            "purchased_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "credits_activated", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("price_cents >= 0", name="ck_package_purchases_price"),
        sa.CheckConstraint(
            "expires_after_days IS NULL OR expires_after_days >= 1",
            name="ck_package_purchases_expiry_days",
        ),
        sa.CheckConstraint(
            "(expires_after_days IS NULL) = (expires_at IS NULL)",
            name="ck_package_purchases_expiry_pair",
        ),
        sa.CheckConstraint(
            "(credits_activated = false AND activated_at IS NULL) OR "
            "(credits_activated = true AND activated_at IS NOT NULL)",
            name="ck_package_purchases_activation_fields",
        ),
    )

    op.create_table(
        "package_purchase_credits",
        sa.Column(
            "package_purchase_id",
            sa.Uuid(),
            sa.ForeignKey("package_purchases.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "service_id",
            sa.Uuid(),
            sa.ForeignKey("services.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("credits_total", sa.SmallInteger(), nullable=False),
        sa.Column("allocated_price_cents", sa.Integer(), nullable=False),
        sa.CheckConstraint("credits_total >= 1", name="ck_package_purchase_credits_total"),
        sa.CheckConstraint("allocated_price_cents >= 0", name="ck_package_purchase_credits_price"),
    )

    # --- `invoices` gains a second, mutually exclusive source ----------------------------------
    op.alter_column("invoices", "service_bill_id", nullable=True)
    op.add_column(
        "invoices",
        sa.Column(
            "package_purchase_id",
            sa.Uuid(),
            sa.ForeignKey("package_purchases.id", ondelete="RESTRICT"),
            nullable=True,
        ),
    )
    op.create_unique_constraint(
        "uq_invoices_package_purchase_id", "invoices", ["package_purchase_id"]
    )
    op.create_check_constraint(
        "ck_invoices_source_xor",
        "invoices",
        "(service_bill_id IS NOT NULL AND package_purchase_id IS NULL) OR "
        "(service_bill_id IS NULL AND package_purchase_id IS NOT NULL)",
    )

    # `invoices_voidable_guard` (0054) compared `service_bill_id` with plain `=`, which is safe
    # only while the column is `NOT NULL`. Replaced whole, `IS NOT DISTINCT FROM` throughout for
    # both source columns — the permitted transition (issued -> cancelled) never touches either.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION invoices_voidable_guard() RETURNS trigger
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
                RAISE EXCEPTION 'invoices: DELETE is not permitted for %', current_user
                    USING ERRCODE = 'insufficient_privilege';
            END IF;
            IF OLD.status = 'issued' AND NEW.status = 'cancelled'
               AND NEW.id = OLD.id
               AND NEW.business_id = OLD.business_id
               AND NEW.invoice_number = OLD.invoice_number
               AND NEW.service_bill_id IS NOT DISTINCT FROM OLD.service_bill_id
               AND NEW.package_purchase_id IS NOT DISTINCT FROM OLD.package_purchase_id
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

    # --- grants + guards for the two new tables -------------------------------------------------
    op.execute("REVOKE DELETE ON package_purchases FROM linsuite_app")
    op.execute("REVOKE UPDATE, DELETE ON package_purchase_credits FROM linsuite_app")

    op.execute(
        """
        CREATE OR REPLACE FUNCTION package_purchases_activation_guard() RETURNS trigger
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
                RAISE EXCEPTION 'package_purchases: DELETE is not permitted for %', current_user
                    USING ERRCODE = 'insufficient_privilege';
            END IF;
            -- The one permitted transition: credits_activated false -> true, activated_at
            -- newly set, every other column held bit-for-bit identical.
            IF OLD.credits_activated = false AND NEW.credits_activated = true
               AND NEW.id = OLD.id
               AND NEW.package_definition_id = OLD.package_definition_id
               AND NEW.customer_id = OLD.customer_id
               AND NEW.name = OLD.name
               AND NEW.price_cents = OLD.price_cents
               AND NEW.expires_after_days IS NOT DISTINCT FROM OLD.expires_after_days
               AND NEW.expires_at IS NOT DISTINCT FROM OLD.expires_at
               AND NEW.purchased_at = OLD.purchased_at
               AND NEW.created_at = OLD.created_at
               AND OLD.activated_at IS NULL
               AND NEW.activated_at IS NOT NULL
            THEN
                RETURN NEW;
            END IF;
            RAISE EXCEPTION
                'package_purchases: this update is not the permitted activation transition (for %)',
                current_user
                USING ERRCODE = 'insufficient_privilege';
        END $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER package_purchases_activation_guard
        BEFORE UPDATE OR DELETE ON package_purchases
        FOR EACH ROW EXECUTE FUNCTION package_purchases_activation_guard();
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION package_purchase_credits_append_only() RETURNS trigger
        SET search_path = pg_catalog, pg_temp
        AS $$
        BEGIN
            IF current_user = 'linsuite_purge'
               OR current_user = (
                   SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid = TG_RELID
               ) THEN
                RETURN CASE TG_OP WHEN 'DELETE' THEN OLD ELSE NEW END;
            END IF;
            RAISE EXCEPTION 'package_purchase_credits is append-only: % is not permitted for %',
                TG_OP, current_user
                USING ERRCODE = 'insufficient_privilege';
        END $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER package_purchase_credits_no_rewrite
        BEFORE UPDATE OR DELETE ON package_purchase_credits
        FOR EACH ROW EXECUTE FUNCTION package_purchase_credits_append_only();
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS package_purchase_credits_no_rewrite ON package_purchase_credits"
    )
    op.execute("DROP FUNCTION IF EXISTS package_purchase_credits_append_only()")
    op.execute("DROP TRIGGER IF EXISTS package_purchases_activation_guard ON package_purchases")
    op.execute("DROP FUNCTION IF EXISTS package_purchases_activation_guard()")

    # Restore the pre-#71 `invoices_voidable_guard` (0054's own version, `=` comparisons).
    op.execute(
        """
        CREATE OR REPLACE FUNCTION invoices_voidable_guard() RETURNS trigger
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
                RAISE EXCEPTION 'invoices: DELETE is not permitted for %', current_user
                    USING ERRCODE = 'insufficient_privilege';
            END IF;
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

    op.drop_constraint("ck_invoices_source_xor", "invoices", type_="check")
    op.drop_constraint("uq_invoices_package_purchase_id", "invoices", type_="unique")
    op.drop_column("invoices", "package_purchase_id")
    op.alter_column("invoices", "service_bill_id", nullable=False)

    op.drop_table("package_purchase_credits")
    op.drop_table("package_purchases")
