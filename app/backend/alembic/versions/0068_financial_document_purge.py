"""Purge financial PDFs once their CRA retention has passed (#84; ADR-0001 amendment).

Revision ID: 0068
Revises: 0067
Create Date: 2026-09-29

`public.customer_record_guard()` (0026) gains a second DELETE branch for business-keyed rows
(`documents.customer_id IS NULL`, `key_owner = 'business'`, #55/ADR-0003): permitted for the
purge role only once the document's own `retain_until` has passed, and only while any
`linked_customer_id` is itself not held — `retention_expires_at` NULL or past, `'infinity'`
counts as held, the same predicate the customer-keyed branch already uses. The customer-keyed
branch is untouched: same query, same result, for every existing row on every table that
shares this function.

**Why the new branch is a nested `IF`, not one more clause AND'd onto the existing condition.**
This function also fires on `form_submissions` (0029) and `session_notes` (0031), neither of
which has a `key_owner`, `retain_until` or `linked_customer_id` column. PL/pgSQL resolves a
record field reference (`OLD.key_owner`) by looking it up in OLD's actual tuple descriptor
*before* the surrounding boolean is evaluated — the whole condition is planned and its inputs
gathered as one query, so a `document`-only field referenced anywhere in a combined `A AND B`
expression raises "record ... has no field ..." the instant that expression runs on a
`form_submissions` or `session_notes` row, regardless of which side of the AND would have been
false. Nesting the business-keyed check inside its own `IF ... ELSIF TG_TABLE_NAME =
'documents' THEN` means that statement — and the field lookups inside it — is only ever
*executed* when OLD truly is a `documents` row: PL/pgSQL's own control flow skips an unreached
branch's statements entirely, rather than folding them into one evaluated expression.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0068"
down_revision: str | None = "0067"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.customer_record_guard() RETURNS trigger
        SET search_path = pg_catalog, pg_temp
        AS $$
        BEGIN
            IF current_user = (
                SELECT pg_get_userbyid(relowner) FROM pg_catalog.pg_class WHERE oid = TG_RELID
            ) THEN
                RETURN CASE TG_OP WHEN 'DELETE' THEN OLD ELSE NEW END;
            END IF;
            IF TG_OP = 'DELETE' AND current_user = 'linsuite_purge' THEN
                IF OLD.customer_id IS NOT NULL THEN
                    -- Customer-keyed row (0026, unchanged): not held = no hold, or a hold
                    -- that has passed. Unlocked read, as ADR-0001 rule 7's FK-as-lock note
                    -- explains — the FK to the key row is what serialises this against an
                    -- insert, not a lock taken here.
                    IF EXISTS (
                        SELECT 1 FROM public.customers
                        WHERE id = OLD.customer_id
                          AND (retention_expires_at IS NULL OR retention_expires_at < now())
                    ) THEN
                        RETURN OLD;
                    END IF;
                ELSIF TG_TABLE_NAME = 'documents' THEN
                    -- Business-keyed row (#84; ADR-0001 amendment): its own CRA clock, plus
                    -- any linked client's hold — the database's own read decides eligibility,
                    -- never only the purge job's own selection query.
                    IF OLD.key_owner = 'business' AND OLD.retain_until < now() AND (
                        OLD.linked_customer_id IS NULL OR EXISTS (
                            SELECT 1 FROM public.customers
                            WHERE id = OLD.linked_customer_id
                              AND (retention_expires_at IS NULL OR retention_expires_at < now())
                        )
                    ) THEN
                        RETURN OLD;
                    END IF;
                END IF;
            END IF;
            RAISE EXCEPTION '%: % is not permitted for % on customer %',
                TG_TABLE_NAME, TG_OP, current_user, OLD.customer_id
                USING ERRCODE = 'insufficient_privilege';
        END $$ LANGUAGE plpgsql;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.customer_record_guard() RETURNS trigger
        SET search_path = pg_catalog, pg_temp
        AS $$
        BEGIN
            IF current_user = (
                SELECT pg_get_userbyid(relowner) FROM pg_catalog.pg_class WHERE oid = TG_RELID
            ) THEN
                RETURN CASE TG_OP WHEN 'DELETE' THEN OLD ELSE NEW END;
            END IF;
            IF TG_OP = 'DELETE' AND current_user = 'linsuite_purge' AND EXISTS (
                SELECT 1 FROM public.customers
                WHERE id = OLD.customer_id
                  AND (retention_expires_at IS NULL OR retention_expires_at < now())
            ) THEN
                RETURN OLD;
            END IF;
            RAISE EXCEPTION '%: % is not permitted for % on customer %',
                TG_TABLE_NAME, TG_OP, current_user, OLD.customer_id
                USING ERRCODE = 'insufficient_privilege';
        END $$ LANGUAGE plpgsql;
        """
    )
