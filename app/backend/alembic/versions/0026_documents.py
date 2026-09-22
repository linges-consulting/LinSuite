"""The document store's table and the shared customer-record guard (Task 1, #44; ADR-0001 rule 7).

Revision ID: 0026
Revises: 0025
Create Date: 2026-09-22

`documents`: sealed document bytes (`core/documents.py`), immutable.

- `linsuite_app` keeps SELECT and INSERT only; `linsuite_purge` keeps its default SELECT and
  DELETE. Revoked *and* trigger-guarded, like `customer_document_keys`.
- `public.customer_record_guard()`, shared by every table holding rows sealed under a
  client's DEK (`form_submissions` joins it in Task 4): the table owner passes; the purge role
  may DELETE only while the row's client is not held — `retention_expires_at IS NULL OR <
  now()`, and `'infinity'` never is; everything else is `insufficient_privilege` naming the
  table. `search_path = pg_catalog, pg_temp`, every relation qualified, as 0024's guard.
- `customer_id` references `customer_document_keys(customer_id)`, no cascade (ADR-0001 rule
  7): a document insert takes `FOR KEY SHARE` on the key row, so the purge's DELETE of that key
  serialises against it, and the purge has to delete the documents first (rule 8). A cascade
  would run as the owner, whom the guard lets through.
- Not partitioned (pre-flight C2): revisit at ~1 M rows.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "documents",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "customer_id",
            sa.Uuid(),
            sa.ForeignKey("customer_document_keys.customer_id"),
            nullable=False,
        ),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("content_type", sa.Text(), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("sha256", sa.LargeBinary(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("kind", "source_id", name="uq_documents_kind_source_id"),
        sa.CheckConstraint("octet_length(sha256) = 32", name="ck_documents_sha256_length"),
    )
    op.create_index("ix_documents_customer_id", "documents", ["customer_id"])
    op.execute("REVOKE UPDATE, DELETE ON documents FROM linsuite_app")

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
            -- Unlocked read of the hold, as 0024's guard: the FK to the key row is what
            -- serialises a purge against a document insert (ADR-0001 rule 7).
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
    op.execute(
        """
        CREATE TRIGGER documents_guard
        BEFORE UPDATE OR DELETE ON documents
        FOR EACH ROW EXECUTE FUNCTION public.customer_record_guard();
        """
    )


def downgrade() -> None:
    op.drop_table("documents")  # takes the trigger with it
    op.execute("DROP FUNCTION IF EXISTS public.customer_record_guard()")
