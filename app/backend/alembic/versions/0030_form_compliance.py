"""Essential forms and compliance: template settings and service mapping (Task 8, #51).

Revision ID: 0030
Revises: 0029
Create Date: 2026-09-22

- `form_templates.applies_to_all` / `valid_for_months`: template *identity* settings, edited
  outside versioning (`PUT /api/admin/forms/{id}/settings`, `forms.manage`, audited) — unlike
  `is_health_form`/`is_mandatory` (0027), which are versioned because a submission's
  compliance is judged by what the client actually signed. The essential-forms checklist
  itself is driven by the latest published version's `is_mandatory`, not a new column here
  (progress.md's ruling: "mandatory = the essential-forms concept") — see `forms/compliance.py`.
- `form_template_services`: which services make a template apply to a client with a confirmed
  future appointment for one of them (owner ruling Q6 — services only, staff types dropped).
  Both FKs cascade: dropping a template or a service drops the mapping, never the other row.
  No client data — ordinary app-role table, nothing to guard.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0030"
down_revision: str | None = "0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "form_templates",
        sa.Column("applies_to_all", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.add_column("form_templates", sa.Column("valid_for_months", sa.SmallInteger()))
    op.create_check_constraint(
        "ck_form_templates_valid_for_months",
        "form_templates",
        "valid_for_months IS NULL OR valid_for_months BETWEEN 1 AND 120",
    )
    op.create_table(
        "form_template_services",
        sa.Column(
            "template_id",
            sa.Uuid(),
            sa.ForeignKey("form_templates.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "service_id",
            sa.Uuid(),
            sa.ForeignKey("services.id", ondelete="CASCADE"),
            primary_key=True,
        ),
    )


def downgrade() -> None:
    op.drop_table("form_template_services")
    op.drop_constraint("ck_form_templates_valid_for_months", "form_templates", type_="check")
    op.drop_column("form_templates", "valid_for_months")
    op.drop_column("form_templates", "applies_to_all")
