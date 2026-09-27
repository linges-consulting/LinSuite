"""Appointment-linked sealed session notes and configurable templates (#9)."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "note_templates",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("fields", JSONB, nullable=False),
        sa.Column("diagram_ids", JSONB, nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
    )
    op.create_table(
        "session_notes",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "customer_id",
            sa.Uuid(),
            sa.ForeignKey("customer_document_keys.customer_id"),
            nullable=False,
        ),
        sa.Column("appointment_id", sa.Uuid(), sa.ForeignKey("appointments.id"), nullable=False),
        sa.Column("author_staff_id", sa.Uuid(), sa.ForeignKey("staff.id"), nullable=False),
        sa.Column("template_id", sa.Uuid(), sa.ForeignKey("note_templates.id"), nullable=False),
        sa.Column("template_snapshot", JSONB, nullable=False),
        sa.Column("content_sealed", sa.LargeBinary(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("locked_at", sa.DateTime(timezone=True)),
        sa.Column("locked_by_user_id", sa.Uuid(), sa.ForeignKey("users.id")),
        sa.CheckConstraint("revision >= 1", name="ck_session_notes_revision"),
        sa.CheckConstraint(
            "(locked_at IS NULL) = (locked_by_user_id IS NULL)", name="ck_session_notes_lock_actor"
        ),
        sa.CheckConstraint(
            "updated_at >= created_at AND (locked_at IS NULL OR locked_at >= created_at)",
            name="ck_session_notes_times",
        ),
    )
    op.create_index("ix_session_notes_customer_id", "session_notes", ["customer_id"])
    op.execute("REVOKE DELETE ON session_notes FROM linsuite_app")
    op.execute(
        "CREATE TRIGGER session_notes_delete_guard BEFORE DELETE ON session_notes "
        "FOR EACH ROW EXECUTE FUNCTION public.customer_record_guard()"
    )
    op.execute("""
        CREATE FUNCTION public.session_notes_guard() RETURNS trigger
        SET search_path = pg_catalog, pg_temp
        AS $$
        BEGIN
            IF current_user = (
                SELECT pg_get_userbyid(relowner) FROM pg_catalog.pg_class WHERE oid = TG_RELID
            ) THEN
                RETURN NEW;
            END IF;
            IF current_user <> 'linsuite_app' THEN
                RAISE EXCEPTION 'session_notes: write is not permitted for %', current_user
                    USING ERRCODE = 'insufficient_privilege';
            END IF;
            IF TG_OP = 'INSERT' THEN
                IF NEW.locked_at IS NOT NULL OR NEW.locked_by_user_id IS NOT NULL
                   OR NEW.revision <> 1 THEN
                    RAISE EXCEPTION 'session_notes: a note must start as a draft'
                        USING ERRCODE = 'insufficient_privilege';
                END IF;
            ELSE
                IF OLD.locked_at IS NOT NULL THEN
                    RAISE EXCEPTION 'session_notes: locked notes are immutable'
                        USING ERRCODE = 'insufficient_privilege';
                END IF;
                IF ROW(NEW.id, NEW.customer_id, NEW.appointment_id, NEW.author_staff_id,
                       NEW.template_id, NEW.template_snapshot, NEW.created_at)
                   IS DISTINCT FROM ROW(OLD.id, OLD.customer_id, OLD.appointment_id,
                       OLD.author_staff_id, OLD.template_id, OLD.template_snapshot, OLD.created_at)
                   OR NEW.revision <> OLD.revision + 1 OR NEW.updated_at < OLD.updated_at THEN
                    RAISE EXCEPTION 'session_notes: identity is immutable; edits advance revision'
                        USING ERRCODE = 'insufficient_privilege';
                END IF;
            END IF;
            RETURN NEW;
        END $$ LANGUAGE plpgsql;
    """)
    op.execute(
        "CREATE TRIGGER session_notes_guard BEFORE INSERT OR UPDATE ON session_notes "
        "FOR EACH ROW EXECUTE FUNCTION public.session_notes_guard()"
    )
    for key in ("notes.view", "notes.write"):
        op.execute(
            sa.text(
                "INSERT INTO role_capabilities (role_id, capability) SELECT id, :key FROM roles "
                "WHERE is_system AND name IN ('Administrator', 'Staff') ON CONFLICT DO NOTHING"
            ).bindparams(key=key)
        )
    op.execute(
        "INSERT INTO role_capabilities (role_id, capability) SELECT id, 'notes.manage' FROM roles "
        "WHERE is_system AND name = 'Administrator' ON CONFLICT DO NOTHING"
    )


def downgrade():
    op.execute(
        "DELETE FROM role_capabilities "
        "WHERE capability IN ('notes.view', 'notes.write', 'notes.manage')"
    )
    op.drop_table("session_notes")
    op.execute("DROP FUNCTION public.session_notes_guard()")
    op.drop_table("note_templates")
