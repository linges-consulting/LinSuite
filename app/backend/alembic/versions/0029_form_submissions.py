"""Form submissions: the client's answers, sealed and immutable (Task 4, #47; ADR-0001 rule 7).

Revision ID: 0029
Revises: 0028
Create Date: 2026-09-22

- `form_submissions`: one row per filled-in form. `id` is chosen by the client's page (an
  idempotent retry sends the same one). The pinned `version_id` — never the template — plus
  `template_id` (denormalised, for compliance queries), the `link_id` it came through (UNIQUE:
  a link is used once), `method` (`link` now, `scan` in Task 7), `submitted_at` and
  `source_ip`.
- **No plaintext answers.** `answers_sealed` is the answers JSON — the drawn signature and
  the typed name included — AES-256-GCM under the client's DEK (owner ruling Q8). NULL only
  for a scan, whose content is its document. So the crypto-shred destroys the answers with
  the key, and a `pg_dump` holds nothing readable.
- `customer_id` references `customer_document_keys(customer_id)`, no cascade (rule 7): the
  insert takes `FOR KEY SHARE` on the key row, so a purge's key DELETE serialises against it.
- Immutable: `linsuite_app` keeps SELECT and INSERT only, and `form_submissions_guard` runs
  0026's `public.customer_record_guard()` — the owner passes, the purge role may DELETE only
  while the client is not held, everything else is refused. The purge role keeps its default
  SELECT and DELETE.
- `forms.view` ("Open clients' completed forms.") goes to both seeded roles.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0029"
down_revision: str | None = "0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "form_submissions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "customer_id",
            sa.Uuid(),
            sa.ForeignKey("customer_document_keys.customer_id"),
            nullable=False,
        ),
        sa.Column(
            "version_id", sa.Uuid(), sa.ForeignKey("form_template_versions.id"), nullable=False
        ),
        sa.Column("template_id", sa.Uuid(), sa.ForeignKey("form_templates.id"), nullable=False),
        sa.Column("link_id", sa.Uuid(), sa.ForeignKey("form_links.id"), unique=True),
        sa.Column("method", sa.Text(), nullable=False),
        sa.Column("answers_sealed", sa.LargeBinary()),
        sa.Column(
            "submitted_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("source_ip", postgresql.INET()),
        sa.CheckConstraint("method IN ('link', 'scan')", name="ck_form_submissions_method"),
        # A link submission always has its link and its answers; only a scan has neither.
        sa.CheckConstraint(
            "method = 'scan' OR (link_id IS NOT NULL AND answers_sealed IS NOT NULL)",
            name="ck_form_submissions_link_answers",
        ),
    )
    op.create_index(
        "ix_form_submissions_customer_template_submitted",
        "form_submissions",
        ["customer_id", "template_id", "submitted_at"],
    )
    op.execute("REVOKE UPDATE, DELETE ON form_submissions FROM linsuite_app")
    op.execute(
        """
        CREATE TRIGGER form_submissions_guard
        BEFORE UPDATE OR DELETE ON form_submissions
        FOR EACH ROW EXECUTE FUNCTION public.customer_record_guard();
        """
    )
    op.execute(
        "INSERT INTO role_capabilities (role_id, capability) "
        "SELECT id, 'forms.view' FROM roles WHERE name IN ('Administrator', 'Staff') "
        "AND is_system ON CONFLICT DO NOTHING"
    )


def downgrade() -> None:
    op.execute("DELETE FROM role_capabilities WHERE capability = 'forms.view'")
    op.drop_table("form_submissions")  # takes the trigger with it; the guard function is 0026's
