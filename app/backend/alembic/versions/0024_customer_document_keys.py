"""Per-customer document keys, and the purge role's one INSERT (Task 6, #41; ADR-0001 §5, §6).

Revision ID: 0024
Revises: 0023
Create Date: 2026-09-21

`customer_document_keys`: one row per client, the client's data-encryption key wrapped under
`DOCUMENT_MASTER_KEY` (`customers/keys.py`). Destroying the row is the crypto-shred, so:

- `linsuite_app` keeps SELECT and INSERT only. A compromised application can then neither
  read a document without the master key nor destroy one (pre-flight D6).
- `customer_document_keys_guard`, BEFORE UPDATE OR DELETE: the table owner passes (migrations,
  an operator's recovery); `linsuite_purge` may DELETE only while the client is not under a
  retention hold — `retention_expires_at IS NULL OR < now()`, and `'infinity'` is never
  `< now()`. Everything else is `insufficient_privilege`. Revoked *and* trigger-guarded, like
  `audit_events`, because a grant and a trigger are undone by different mistakes.
- The guard pins `search_path = pg_catalog, pg_temp` and qualifies every relation, as 0021
  does: both runtime roles hold TEMP, and `pg_temp` is otherwise searched first, so a temp
  `customers` or `pg_class` could answer the guard's questions for it. Same pin, via
  `ALTER FUNCTION`, for the two append-only guards from 0004 and 0019.
- The FK has no `ON DELETE CASCADE`: a cascade runs as the table owner, which the guard lets
  through, so deleting the customer would be a way round it.

No backfill: wrapping needs the master key, which the schema owner running this migration is
not handed. Existing clients get a key lazily from `customers.keys.data_key`.

`GRANT INSERT ON audit_events TO linsuite_purge` (pre-flight D11): every purge writes the fact
of its own erasure, inside the purge transaction. `audit_events_append_only()` already lets the
purge role through; it still holds no UPDATE anywhere.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

AUDIT_GUARDS = ("public.audit_events_append_only()", "public.audit_access_log_append_only()")


def upgrade() -> None:
    op.create_table(
        "customer_document_keys",
        sa.Column("customer_id", sa.Uuid(), sa.ForeignKey("customers.id"), primary_key=True),
        sa.Column("wrapped_key", sa.Text(), nullable=False),
        sa.Column(
            "master_key_version", sa.SmallInteger(), server_default=sa.text("1"), nullable=False
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.execute("REVOKE UPDATE, DELETE ON customer_document_keys FROM linsuite_app")

    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.customer_document_keys_guard() RETURNS trigger
        SET search_path = pg_catalog, pg_temp
        AS $$
        BEGIN
            IF current_user = (
                SELECT pg_get_userbyid(relowner) FROM pg_catalog.pg_class WHERE oid = TG_RELID
            ) THEN
                RETURN CASE TG_OP WHEN 'DELETE' THEN OLD ELSE NEW END;
            END IF;
            -- ponytail: reads the committed hold without locking the customer row — `FOR
            -- SHARE` needs UPDATE on customers, which the purge role must never hold. A hold
            -- committed in the same instant as the purge's DELETE can be missed; if Phase 8
            -- makes that window real, take the lock in a SECURITY DEFINER helper.
            IF TG_OP = 'DELETE' AND current_user = 'linsuite_purge' AND EXISTS (
                SELECT 1 FROM public.customers
                WHERE id = OLD.customer_id
                  AND (retention_expires_at IS NULL OR retention_expires_at < now())
            ) THEN
                RETURN OLD;
            END IF;
            RAISE EXCEPTION 'customer_document_keys: % is not permitted for % on customer %',
                TG_OP, current_user, OLD.customer_id
                USING ERRCODE = 'insufficient_privilege';
        END $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER customer_document_keys_guard
        BEFORE UPDATE OR DELETE ON customer_document_keys
        FOR EACH ROW EXECUTE FUNCTION public.customer_document_keys_guard();
        """
    )
    # The two append-only guards (0004, 0019) read `pg_class` unqualified for their owner
    # check, and `pg_temp` is searched before `pg_catalog` — a temp `pg_class` naming the caller
    # the owner walked straight through them. Their bodies name nothing else, so pinning the
    # path is the whole fix.
    for function in AUDIT_GUARDS:
        op.execute(f"ALTER FUNCTION {function} SET search_path = pg_catalog, pg_temp")

    op.execute("GRANT INSERT ON audit_events TO linsuite_purge")


def downgrade() -> None:
    for function in AUDIT_GUARDS:
        op.execute(f"ALTER FUNCTION {function} RESET search_path")
    op.execute("REVOKE INSERT ON audit_events FROM linsuite_purge")
    op.execute("DROP TRIGGER IF EXISTS customer_document_keys_guard ON customer_document_keys")
    op.execute("DROP FUNCTION IF EXISTS public.customer_document_keys_guard()")
    op.drop_table("customer_document_keys")
