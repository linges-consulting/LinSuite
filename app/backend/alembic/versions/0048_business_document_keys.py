"""The business-owned document key tier (#55; ADR-0003).

Revision ID: 0048
Revises: 0047
Create Date: 2026-09-28

Additive, per ADR-0003: the existing customer-keyed tier of `documents` (ADR-0001 rule 7,
migration 0026) is untouched by this migration, no existing row is rewritten, and no
expand/contract is needed.

`documents` gains:

- `customer_id` becomes nullable — a business-keyed document has none.
- `key_owner` ('customer' | 'business'), CHECK'd against the tier `customer_id` must agree
  with: customer-keyed rows keep it required, business-keyed rows keep it NULL. Text with a
  two-value CHECK, not a native enum — the same enforcement, no `ALTER TYPE` the day a third
  tier exists.
- `linked_customer_id`, a business-keyed document's own reference to `customers.id` directly
  (never `customer_document_keys`) — the FK that must not exist for a financial document, so
  a customer's own crypto-shred never touches one that happens to be for them. CHECK'd to
  NULL on every customer-keyed row.
- `retain_until`, the CRA six-year clock, tracked independently of
  `customers.retention_expires_at`; nothing purges by it yet.

`business_document_keys`: one row (`business_id` is always 1, `businesses.id`'s own value),
the wrapped key `billing/keys.py` reads. `linsuite_app` keeps SELECT and INSERT only, and
`linsuite_purge` loses its default DELETE too — unlike `customer_document_keys`, there is no
purge-eligible branch: this key is never destroyed in v1. `business_document_keys_guard`
refuses UPDATE and DELETE to every role but the table owner, unconditionally.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0048"
down_revision: str | None = "0047"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("documents", "customer_id", nullable=True)
    op.add_column(
        "documents",
        sa.Column("key_owner", sa.Text(), nullable=False, server_default=sa.text("'customer'")),
    )
    op.add_column(
        "documents",
        sa.Column("linked_customer_id", sa.Uuid(), sa.ForeignKey("customers.id"), nullable=True),
    )
    op.add_column("documents", sa.Column("retain_until", sa.DateTime(timezone=True), nullable=True))
    op.create_check_constraint(
        "ck_documents_key_owner", "documents", "key_owner IN ('customer', 'business')"
    )
    op.create_check_constraint(
        "ck_documents_owner_customer_id",
        "documents",
        "(key_owner = 'customer' AND customer_id IS NOT NULL) OR "
        "(key_owner = 'business' AND customer_id IS NULL)",
    )
    op.create_check_constraint(
        "ck_documents_owner_linked_customer_id",
        "documents",
        "key_owner = 'business' OR linked_customer_id IS NULL",
    )

    op.create_table(
        "business_document_keys",
        sa.Column("business_id", sa.Integer(), sa.ForeignKey("businesses.id"), primary_key=True),
        sa.Column("wrapped_key", sa.Text(), nullable=False),
        sa.Column(
            "master_key_version", sa.SmallInteger(), server_default=sa.text("1"), nullable=False
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.execute("REVOKE UPDATE, DELETE ON business_document_keys FROM linsuite_app")
    op.execute("REVOKE DELETE ON business_document_keys FROM linsuite_purge")

    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.business_document_keys_guard() RETURNS trigger
        SET search_path = pg_catalog, pg_temp
        AS $$
        BEGIN
            IF current_user = (
                SELECT pg_get_userbyid(relowner) FROM pg_catalog.pg_class WHERE oid = TG_RELID
            ) THEN
                RETURN CASE TG_OP WHEN 'DELETE' THEN OLD ELSE NEW END;
            END IF;
            RAISE EXCEPTION 'business_document_keys: % is not permitted for %',
                TG_OP, current_user
                USING ERRCODE = 'insufficient_privilege';
        END $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER business_document_keys_guard
        BEFORE UPDATE OR DELETE ON business_document_keys
        FOR EACH ROW EXECUTE FUNCTION public.business_document_keys_guard();
        """
    )


def downgrade() -> None:
    op.drop_table("business_document_keys")  # takes the trigger with it
    op.execute("DROP FUNCTION IF EXISTS public.business_document_keys_guard()")
    op.drop_constraint("ck_documents_owner_linked_customer_id", "documents")
    op.drop_constraint("ck_documents_owner_customer_id", "documents")
    op.drop_constraint("ck_documents_key_owner", "documents")
    op.drop_column("documents", "retain_until")
    op.drop_column("documents", "linked_customer_id")
    op.drop_column("documents", "key_owner")
    op.alter_column("documents", "customer_id", nullable=False)
