"""Form templates: a stable identity with an editable draft, and its frozen versions.

Tech-stack §18. A submission (Task 4) and a secure link (Task 3) reference a
`FormTemplateVersion`, never the template: the template's draft keeps changing, a version
never does. `form_template_versions` is append-only for the application — REVOKE *and*
trigger (0027) — so "frozen on publish" is the database's rule, not this module's promise.

No client data in either table.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    false,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from core.db import Base

KINDS = ("intake", "consent", "waiver", "other")


class FormTemplate(Base):
    __tablename__ = "form_templates"
    __table_args__ = (
        CheckConstraint(
            "kind IN (" + ", ".join(f"'{k}'" for k in KINDS) + ")", name="ck_form_templates_kind"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(Text)
    # `forms.schema.FormSchema`, validated before it is written. What the next publish copies.
    draft_schema: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=text("""'{"fields": []}'::jsonb""")
    )
    # The next version's flags (owner ruling Q1). Versioned, so they live on the draft too.
    draft_is_health_form: Mapped[bool] = mapped_column(Boolean, server_default=false())
    draft_is_mandatory: Mapped[bool] = mapped_column(Boolean, server_default=false())
    # Hidden from new links; its versions and every submission against them stay.
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class FormTemplateVersion(Base):
    __tablename__ = "form_template_versions"
    __table_args__ = (
        UniqueConstraint("template_id", "number", name="uq_form_template_versions_template_number"),
        CheckConstraint("number >= 1", name="ck_form_template_versions_number"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    template_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("form_templates.id"))
    number: Mapped[int] = mapped_column(Integer)
    schema: Mapped[dict[str, Any]] = mapped_column(JSONB)
    # A submission of a health form is a clinical entry that extends the client's retention
    # hold (Task 4); a mandatory one is on the essential-forms checklist (Task 8).
    is_health_form: Mapped[bool] = mapped_column(Boolean)
    is_mandatory: Mapped[bool] = mapped_column(Boolean)
    # Earlier signatures no longer satisfy compliance once this is published (tech-stack §18).
    requires_resignature: Mapped[bool] = mapped_column(Boolean, server_default=false())
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    published_by_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
