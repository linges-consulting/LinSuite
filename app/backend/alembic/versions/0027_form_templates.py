"""Form templates and their immutable numbered versions (Task 2, #45; tech-stack §18).

Revision ID: 0027
Revises: 0026
Create Date: 2026-09-22

- `form_templates`: the stable identity and the editable draft (`draft_schema` and the two
  draft flags). Ordinary app-role DML: a draft is meant to change.
- `form_template_versions`: one row per publish, numbered from 1, never rewritten. The app
  role keeps SELECT and INSERT only, and `form_template_versions_append_only` refuses UPDATE
  and DELETE to everyone but the table owner — the purge role included, unlike
  `audit_events`: a version holds no client data, so retention never has a reason to remove
  one, and a submission (Task 4) points at it for as long as the submission exists.
- `name` and `kind` are copied onto the version too: the title is part of what a client
  signed, so renaming the draft must never retitle a published version. `form_templates.name`
  is only the administrator's label for the draft.
- `is_health_form` / `is_mandatory` (owner ruling Q1) belong to the version: a submission of a
  health form is a clinical entry, and that must be read from what the client signed, not
  from whatever the template says today.
- The Administrator role holds the new `forms.manage` capability (as 0022/0025 did).
- No client data in either table.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0027"
down_revision: str | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "form_templates",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column(
            "draft_schema",
            postgresql.JSONB(),
            server_default=sa.text("""'{"fields": []}'::jsonb"""),
            nullable=False,
        ),
        sa.Column("draft_is_health_form", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("draft_is_mandatory", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("retired_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "kind IN ('intake', 'consent', 'waiver', 'other')", name="ck_form_templates_kind"
        ),
    )
    op.create_table(
        "form_template_versions",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("template_id", sa.Uuid(), sa.ForeignKey("form_templates.id"), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        # The title and kind the client saw and signed, frozen with the fields.
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("schema", postgresql.JSONB(), nullable=False),
        sa.Column("is_health_form", sa.Boolean(), nullable=False),
        sa.Column("is_mandatory", sa.Boolean(), nullable=False),
        sa.Column("requires_resignature", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column(
            "published_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("published_by_user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.UniqueConstraint(
            "template_id", "number", name="uq_form_template_versions_template_number"
        ),
        sa.CheckConstraint("number >= 1", name="ck_form_template_versions_number"),
        sa.CheckConstraint(
            "kind IN ('intake', 'consent', 'waiver', 'other')",
            name="ck_form_template_versions_kind",
        ),
    )
    op.execute("REVOKE UPDATE, DELETE ON form_template_versions FROM linsuite_app")
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.form_template_versions_append_only() RETURNS trigger
        SET search_path = pg_catalog, pg_temp
        AS $$
        BEGIN
            -- The owner only (migrations, an operator's recovery). Not the purge role: a
            -- version holds no client data and outlives every submission that cites it.
            IF current_user = (
                SELECT pg_get_userbyid(relowner) FROM pg_catalog.pg_class WHERE oid = TG_RELID
            ) THEN
                RETURN CASE TG_OP WHEN 'DELETE' THEN OLD ELSE NEW END;
            END IF;
            RAISE EXCEPTION 'form_template_versions is append-only: % is not permitted for %',
                TG_OP, current_user
                USING ERRCODE = 'insufficient_privilege';
        END $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER form_template_versions_append_only
        BEFORE UPDATE OR DELETE ON form_template_versions
        FOR EACH ROW EXECUTE FUNCTION public.form_template_versions_append_only();
        """
    )
    op.execute(
        "INSERT INTO role_capabilities (role_id, capability) "
        "SELECT id, 'forms.manage' FROM roles WHERE name = 'Administrator' AND is_system "
        "ON CONFLICT DO NOTHING"
    )


def downgrade() -> None:
    op.execute("DELETE FROM role_capabilities WHERE capability = 'forms.manage'")
    op.drop_table("form_template_versions")  # takes the trigger with it
    op.execute("DROP FUNCTION IF EXISTS public.form_template_versions_append_only()")
    op.drop_table("form_templates")
