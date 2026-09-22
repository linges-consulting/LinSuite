"""Secure form links (Task 3, #46; CLAUDE.md "secure form links").

Revision ID: 0028
Revises: 0027
Create Date: 2026-09-22

- `form_links`: one row per link — the SHA-256 of its token (never the token), the client, the
  pinned template *version*, who issued it, and when it was issued, expires, was consumed and
  was revoked. Ordinary app-role DML: the app stamps `consumed_at` (Task 4) and `revoked_at`.
  It holds no ciphertext, so it does not reference `customer_document_keys`.
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
    op.execute(
        "INSERT INTO role_capabilities (role_id, capability) "
        "SELECT id, 'forms.issue' FROM roles WHERE name IN ('Administrator', 'Staff') "
        "AND is_system ON CONFLICT DO NOTHING"
    )


def downgrade() -> None:
    op.execute("DELETE FROM role_capabilities WHERE capability = 'forms.issue'")
    op.drop_table("form_links")
