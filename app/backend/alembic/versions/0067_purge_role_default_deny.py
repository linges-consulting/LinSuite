"""Purge role default-deny (#83; ADR-0001 amendment).

Revision ID: 0067
Revises: 0066
Create Date: 2026-09-29

0001's baseline handed `linsuite_purge` `SELECT, DELETE` on every table, present and future,
via `ALTER DEFAULT PRIVILEGES` — reasonable when the only tables that existed were the ones
the retention job actually purges, and every ledger/audit trigger built since then copied the
same "the purge role is let through unconditionally" bypass as a convenience for test
teardown (`stock_movements`'s own docstring, 0050, says so directly). The hole: a leaked purge
credential, or a mistakenly restored grant, could delete an invoice, a stock movement, a
commission posting, a package purchase or redemption, a retail sale or return, or either audit
log — exactly the records ADR-0001's own trigger design was supposed to make erasure-proof
outside the four tables retention actually governs.

The purge role's authority is narrowed to exactly four tables — `documents`,
`form_submissions`, `session_notes`, `customer_document_keys` — the ones `customers/tasks.py::
_shred` actually deletes from (ADR-0001 rules 7-8). Four moves, each undone by the
downgrade in reverse:

1. **Future tables get `SELECT` only.** `ALTER DEFAULT PRIVILEGES ... REVOKE DELETE` leaves
   the existing `GRANT SELECT` default untouched, so a migration that forgets to think about
   the purge role at all still ships safe by default (pre-flight's own "closed unless
   deliberately granted" framing).
2. **Every existing table loses `DELETE`, then the four regain it explicitly.** A blanket
   `REVOKE DELETE ... FROM linsuite_purge` over every `pg_tables` row in `public` — parent and
   partition alike, so `audit_access_log`'s existing yearly children are swept too, not just
   the parent a REVOKE on it alone would leave untouched (0019's own docstring already flags
   this asymmetry) — followed by one explicit `GRANT DELETE` on the four. Simpler and harder
   to get subtly wrong than trying to enumerate "every table but these four" in the REVOKE
   itself.
3. **`ensure_access_log_partitions()` (0021) stops re-granting `DELETE`.** Its per-partition
   grant becomes `GRANT SELECT ON ... TO linsuite_purge`, so a partition it creates for a
   January nobody has reached yet is never a table the purge role can delete from — the same
   default-deny promise made in (1), extended to the one path that grants directly rather than
   through the default-privileges mechanism. `CREATE OR REPLACE FUNCTION` must repeat every
   clause (`SECURITY DEFINER`, all four `SET`s) verbatim: omitting one does not preserve it, it
   drops it.
4. **Every ledger/audit trigger function is redefined without the purge-role bypass.** Fourteen
   share the plain "TABLE is append-only" shape (`audit_events_append_only` through
   `retail_invoice_lines_append_only`); `retail_returns_append_only` is the `TG_TABLE_NAME`-
   generic one four tables share; `invoices_voidable_guard`, `retail_invoices_voidable_guard`
   and `package_purchases_activation_guard` are the voidable/activation guards with a real
   permitted-transition check besides; `package_credit_redemptions_guard` also gates INSERT
   against overspend and a voided purchase; `snapshot_columns_frozen` guards the tax-snapshot
   columns 0065 added to the two voidable tables. Every one keeps its schema-owner bypass
   (migrations, an operator's own recovery) and loses only the `current_user = 'linsuite_purge'
   OR` half of the check — each body is otherwise byte-for-byte its current definition, so
   nothing about *what* is permitted changes, only *who* the trigger lets through un-checked.

`documents`, `form_submissions`, `session_notes` and `customer_document_keys` are untouched:
their own guards (`customer_record_guard`, 0026; `customer_document_keys_guard`, 0024) already
condition the purge role's DELETE on the client's retention hold rather than letting it through
unconditionally, and that is exactly the shape every other trigger above is being brought to
resemble (minus the purge role's access at all, since these four are the only tables it should
still reach).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0067"
down_revision: str | None = "0066"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PURGE_TABLES = ("documents", "form_submissions", "session_notes", "customer_document_keys")

# --- the fourteen plain "TABLE is append-only" guards -------------------------------------------
_APPEND_ONLY = {
    "audit_events_append_only": "audit_events",
    "audit_access_log_append_only": "audit_access_log",
    "stock_movements_append_only": "stock_movements",
    "invoice_lines_append_only": "invoice_lines",
    "invoice_line_discounts_append_only": "invoice_line_discounts",
    "invoice_line_taxes_append_only": "invoice_line_taxes",
    "package_purchase_credits_append_only": "package_purchase_credits",
    "commission_postings_no_rewrite": "commission_postings",
    "invoice_payments_append_only": "invoice_payments",
    "invoice_balance_authorizations_append_only": "invoice_balance_authorizations",
    "invoice_payment_transfers_append_only": "invoice_payment_transfers",
    "invoice_refunds_append_only": "invoice_refunds",
    "package_credit_voids_append_only": "package_credit_voids",
    "retail_invoice_lines_append_only": "retail_invoice_lines",
}


def _append_only_guard(function: str, table: str, *, purge_bypass: bool) -> str:
    bypass = "current_user = 'linsuite_purge'\n           OR " if purge_bypass else ""
    return f"""
    CREATE OR REPLACE FUNCTION {function}() RETURNS trigger
    SET search_path = pg_catalog, pg_temp
    AS $$
    BEGIN
        IF {bypass}current_user = (
           SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid = TG_RELID
        ) THEN
            RETURN CASE TG_OP WHEN 'DELETE' THEN OLD ELSE NEW END;
        END IF;
        RAISE EXCEPTION '{table} is append-only: % is not permitted for %',
            TG_OP, current_user
            USING ERRCODE = 'insufficient_privilege';
    END $$ LANGUAGE plpgsql;
    """


def _retail_returns_append_only(*, purge_bypass: bool) -> str:
    bypass = "current_user = 'linsuite_purge'\n           OR " if purge_bypass else ""
    return f"""
    CREATE OR REPLACE FUNCTION retail_returns_append_only() RETURNS trigger
    SET search_path = pg_catalog, pg_temp
    AS $$
    BEGIN
        IF {bypass}current_user = (
           SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid = TG_RELID
        ) THEN
            RETURN CASE TG_OP WHEN 'DELETE' THEN OLD ELSE NEW END;
        END IF;
        RAISE EXCEPTION '% is append-only: % is not permitted for %',
            TG_TABLE_NAME, TG_OP, current_user
            USING ERRCODE = 'insufficient_privilege';
    END $$ LANGUAGE plpgsql;
    """


def _invoices_voidable_guard(*, purge_bypass: bool) -> str:
    bypass = "current_user = 'linsuite_purge'\n           OR " if purge_bypass else ""
    return f"""
    CREATE OR REPLACE FUNCTION invoices_voidable_guard() RETURNS trigger
    SET search_path = pg_catalog, pg_temp
    AS $$
    BEGIN
        IF {bypass}current_user = (
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


def _retail_invoices_voidable_guard(*, purge_bypass: bool) -> str:
    bypass = "current_user = 'linsuite_purge'\n           OR " if purge_bypass else ""
    return f"""
    CREATE OR REPLACE FUNCTION retail_invoices_voidable_guard() RETURNS trigger
    SET search_path = pg_catalog, pg_temp
    AS $$
    BEGIN
        IF {bypass}current_user = (
           SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid = TG_RELID
        ) THEN
            RETURN CASE TG_OP WHEN 'DELETE' THEN OLD ELSE NEW END;
        END IF;
        IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'retail_invoices: DELETE is not permitted for %', current_user
                USING ERRCODE = 'insufficient_privilege';
        END IF;
        IF OLD.status = 'issued' AND NEW.status = 'cancelled'
           AND NEW.id = OLD.id
           AND NEW.business_id = OLD.business_id
           AND NEW.invoice_number = OLD.invoice_number
           AND NEW.retail_sale_id = OLD.retail_sale_id
           AND NEW.customer_id IS NOT DISTINCT FROM OLD.customer_id
           AND NEW.subtotal_cents = OLD.subtotal_cents
           AND NEW.grand_total_cents = OLD.grand_total_cents
           AND NEW.sold_by_staff_id = OLD.sold_by_staff_id
           AND NEW.payment_collector_staff_id
               IS NOT DISTINCT FROM OLD.payment_collector_staff_id
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
            'retail_invoices: this update is not the permitted voidable transition (for %)',
            current_user
            USING ERRCODE = 'insufficient_privilege';
    END $$ LANGUAGE plpgsql;
    """


def _package_purchases_activation_guard(*, purge_bypass: bool) -> str:
    bypass = "current_user = 'linsuite_purge'\n           OR " if purge_bypass else ""
    return f"""
    CREATE OR REPLACE FUNCTION package_purchases_activation_guard() RETURNS trigger
    SET search_path = pg_catalog, pg_temp
    AS $$
    BEGIN
        IF {bypass}current_user = (
           SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid = TG_RELID
        ) THEN
            RETURN CASE TG_OP WHEN 'DELETE' THEN OLD ELSE NEW END;
        END IF;
        IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'package_purchases: DELETE is not permitted for %', current_user
                USING ERRCODE = 'insufficient_privilege';
        END IF;
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


def _package_credit_redemptions_guard(*, purge_bypass: bool) -> str:
    bypass = "current_user = 'linsuite_purge'\n           OR " if purge_bypass else ""
    return f"""
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
            IF EXISTS (
                SELECT 1 FROM public.package_credit_voids v
                 WHERE v.package_purchase_id = NEW.package_purchase_id
            ) THEN
                RAISE EXCEPTION 'package purchase % credits are voided', NEW.package_purchase_id
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END IF;
        IF {bypass}current_user = (
           SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid = TG_RELID
        ) THEN
            RETURN CASE TG_OP WHEN 'DELETE' THEN OLD ELSE NEW END;
        END IF;
        RAISE EXCEPTION 'package_credit_redemptions is append-only: % is not permitted for %',
            TG_OP, current_user
            USING ERRCODE = 'insufficient_privilege';
    END $$ LANGUAGE plpgsql;
    """


def _snapshot_columns_frozen(*, purge_bypass: bool) -> str:
    bypass = "current_user = 'linsuite_purge'\n           OR " if purge_bypass else ""
    return f"""
    CREATE OR REPLACE FUNCTION snapshot_columns_frozen() RETURNS trigger
    SET search_path = pg_catalog, pg_temp
    AS $$
    DECLARE col text;
    BEGIN
        IF {bypass}current_user = (
           SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid = TG_RELID
        ) THEN
            RETURN NEW;
        END IF;
        FOREACH col IN ARRAY TG_ARGV LOOP
            IF to_jsonb(NEW) -> col IS DISTINCT FROM to_jsonb(OLD) -> col THEN
                RAISE EXCEPTION '%: % is frozen at issue (for %)',
                    TG_TABLE_NAME, col, current_user
                    USING ERRCODE = 'insufficient_privilege';
            END IF;
        END LOOP;
        RETURN NEW;
    END $$ LANGUAGE plpgsql;
    """


# 0021's partition-creating function, differing from the original by exactly the purge grant
# on its own last EXECUTE — every clause (SECURITY DEFINER, all four SETs) must be repeated in
# full: CREATE OR REPLACE does not preserve what it is not told to keep.
_ENSURE_ACCESS_LOG_PARTITIONS = """
CREATE OR REPLACE FUNCTION public.ensure_access_log_partitions() RETURNS text[]
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET timezone = 'UTC'
SET default_tablespace = ''
SET default_table_access_method = heap
AS $$
DECLARE
    this_year int := extract(year FROM now())::int;
    y int;
    child text;
    created text[] := '{}';
BEGIN
    PERFORM pg_advisory_xact_lock(hashtext('public.ensure_access_log_partitions'));
    FOR y IN this_year .. this_year + 1 LOOP
        child := 'audit_access_log_' || y;
        CONTINUE WHEN EXISTS (
            SELECT 1 FROM pg_inherits
            WHERE inhparent = 'public.audit_access_log'::regclass
              AND inhrelid = to_regclass('public.' || child)
        );
        IF to_regclass('public.' || child) IS NOT NULL THEN
            RAISE EXCEPTION 'public.% exists but is not a partition of audit_access_log',
                child USING ERRCODE = 'duplicate_table';
        END IF;
        EXECUTE format(
            'CREATE TABLE public.%I PARTITION OF public.audit_access_log '
            'FOR VALUES FROM (%L) TO (%L)',
            child, y || '-01-01', (y + 1) || '-01-01'
        );
        EXECUTE format(
            'REVOKE ALL ON public.%I FROM PUBLIC, linsuite_app, linsuite_purge', child
        );
        EXECUTE format('GRANT SELECT, INSERT ON public.%I TO linsuite_app', child);
        EXECUTE format('GRANT SELECT ON public.%I TO linsuite_purge', child);
        created := created || child;
    END LOOP;
    RETURN created;
END $$;
"""

# The pre-#83 version (0021, verbatim): the one difference is the last EXECUTE's own grant.
_ENSURE_ACCESS_LOG_PARTITIONS_PRE_83 = _ENSURE_ACCESS_LOG_PARTITIONS.replace(
    "EXECUTE format('GRANT SELECT ON public.%I TO linsuite_purge', child);",
    "EXECUTE format('GRANT SELECT, DELETE ON public.%I TO linsuite_purge', child);",
)

_SWEEP_EVERY_TABLE = """
DO $$
DECLARE
    tbl text;
BEGIN
    FOR tbl IN SELECT tablename FROM pg_tables WHERE schemaname = 'public' LOOP
        EXECUTE format('{verb} DELETE ON public.%I {prep} linsuite_purge', tbl);
    END LOOP;
END $$;
"""


def upgrade() -> None:
    # --- 1. future tables: SELECT only, by default ----------------------------------------------
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE DELETE ON TABLES FROM linsuite_purge"
    )

    # --- 2. blank the slate, then grant DELETE back on exactly the four ------------------------
    # A REVOKE swept over every `pg_tables` row in `public` catches parents and partitions
    # alike (`audit_access_log`'s own two existing years included) in one pass, which is
    # simpler and harder to get subtly wrong than trying to enumerate "every table but these
    # four" directly.
    op.execute(_SWEEP_EVERY_TABLE.format(verb="REVOKE", prep="FROM"))
    op.execute(f"GRANT DELETE ON {', '.join(PURGE_TABLES)} TO linsuite_purge")

    # --- 3. a partition made after today never carries DELETE either ---------------------------
    op.execute(_ENSURE_ACCESS_LOG_PARTITIONS)

    # --- 4. every ledger/audit trigger loses the purge-role bypass; the owner's stays ----------
    for function, table in _APPEND_ONLY.items():
        op.execute(_append_only_guard(function, table, purge_bypass=False))
    op.execute(_retail_returns_append_only(purge_bypass=False))
    op.execute(_invoices_voidable_guard(purge_bypass=False))
    op.execute(_retail_invoices_voidable_guard(purge_bypass=False))
    op.execute(_package_purchases_activation_guard(purge_bypass=False))
    op.execute(_package_credit_redemptions_guard(purge_bypass=False))
    op.execute(_snapshot_columns_frozen(purge_bypass=False))


def downgrade() -> None:
    # --- 4. restore the purge-role bypass on every one of the same functions -------------------
    op.execute(_snapshot_columns_frozen(purge_bypass=True))
    op.execute(_package_credit_redemptions_guard(purge_bypass=True))
    op.execute(_package_purchases_activation_guard(purge_bypass=True))
    op.execute(_retail_invoices_voidable_guard(purge_bypass=True))
    op.execute(_invoices_voidable_guard(purge_bypass=True))
    op.execute(_retail_returns_append_only(purge_bypass=True))
    for function, table in _APPEND_ONLY.items():
        op.execute(_append_only_guard(function, table, purge_bypass=True))

    # --- 3. restore 0021's own grant on a newly made partition ----------------------------------
    op.execute(_ENSURE_ACCESS_LOG_PARTITIONS_PRE_83)

    # --- 2. restore DELETE everywhere (the explicit grant on the four is already covered) ------
    op.execute(_SWEEP_EVERY_TABLE.format(verb="GRANT", prep="TO"))

    # --- 1. restore the old default -------------------------------------------------------------
    op.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT DELETE ON TABLES TO linsuite_purge")
