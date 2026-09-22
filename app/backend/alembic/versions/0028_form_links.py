"""Secure form links (Task 3, #46; CLAUDE.md "secure form links").

Revision ID: 0028
Revises: 0027
Create Date: 2026-09-22

- `form_links`: one row per link — the SHA-256 of its token (never the token), the client, the
  pinned template *version*, who issued it, and when it was issued, expires, was consumed and
  was revoked. Ordinary app-role DML: the app stamps `consumed_at` (Task 4) and `revoked_at`.
  It holds no ciphertext, so it does not reference `customer_document_keys`.
- Enforced in the database, not by the handlers (fix round 1): the app role holds no DELETE
  (the purge role keeps its default DELETE; nothing on the purge path needs the app role to
  delete a link), and `form_links_guard` BEFORE UPDATE freezes the token digest, the client,
  the version, the issuer, `issued_at` and `expires_at`, and lets `revoked_at` and
  `consumed_at` go NULL → value only — never back, never rewritten. Task 4's single use and
  erasure's revocation rest on exactly those two columns. The table owner passes.
- `forms.issue` goes to both seeded roles: sending a form is front-desk work in Staff Mode.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0028"
down_revision: str | None = "0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "form_links",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("token_sha256", sa.LargeBinary(), nullable=False, unique=True),
        sa.Column("customer_id", sa.Uuid(), sa.ForeignKey("customers.id"), nullable=False),
        sa.Column(
            "version_id", sa.Uuid(), sa.ForeignKey("form_template_versions.id"), nullable=False
        ),
        sa.Column("issued_by_user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("expires_at > issued_at", name="ck_form_links_expiry"),
        sa.CheckConstraint("octet_length(token_sha256) = 32", name="ck_form_links_sha256"),
    )
    op.create_index("ix_form_links_customer_id", "form_links", ["customer_id"])
    op.execute("REVOKE DELETE ON form_links FROM linsuite_app")
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.form_links_guard() RETURNS trigger
        SET search_path = pg_catalog, pg_temp
        AS $$
        BEGIN
            -- The owner only (migrations, an operator's recovery, the test harness).
            IF current_user = (
                SELECT pg_catalog.pg_get_userbyid(relowner)
                  FROM pg_catalog.pg_class WHERE oid = TG_RELID
            ) THEN
                RETURN NEW;
            END IF;
            IF NEW.id IS DISTINCT FROM OLD.id
               OR NEW.token_sha256 IS DISTINCT FROM OLD.token_sha256
               OR NEW.customer_id IS DISTINCT FROM OLD.customer_id
               OR NEW.version_id IS DISTINCT FROM OLD.version_id
               OR NEW.issued_by_user_id IS DISTINCT FROM OLD.issued_by_user_id
               OR NEW.issued_at IS DISTINCT FROM OLD.issued_at
               OR NEW.expires_at IS DISTINCT FROM OLD.expires_at THEN
                RAISE EXCEPTION 'form_links: only revoked_at and consumed_at may change'
                    USING ERRCODE = 'insufficient_privilege';
            END IF;
            IF (OLD.revoked_at IS NOT NULL AND NEW.revoked_at IS DISTINCT FROM OLD.revoked_at)
               OR (OLD.consumed_at IS NOT NULL
                   AND NEW.consumed_at IS DISTINCT FROM OLD.consumed_at) THEN
                RAISE EXCEPTION 'form_links: a revoked or consumed link stays that way'
                    USING ERRCODE = 'insufficient_privilege';
            END IF;
            RETURN NEW;
        END $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER form_links_guard
        BEFORE UPDATE ON form_links
        FOR EACH ROW EXECUTE FUNCTION public.form_links_guard();
        """
    )
    op.execute(
        "INSERT INTO role_capabilities (role_id, capability) "
        "SELECT id, 'forms.issue' FROM roles WHERE name IN ('Administrator', 'Staff') "
        "AND is_system ON CONFLICT DO NOTHING"
    )


def downgrade() -> None:
    op.execute("DELETE FROM role_capabilities WHERE capability = 'forms.issue'")
    op.drop_table("form_links")  # takes the trigger with it
    op.execute("DROP FUNCTION IF EXISTS public.form_links_guard()")
