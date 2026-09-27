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
    Index,
    Integer,
    LargeBinary,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    false,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from core.db import Base

KINDS = ("intake", "consent", "waiver", "other")


class FormTemplate(Base):
    __tablename__ = "form_templates"
    __table_args__ = (
        CheckConstraint(
            "kind IN (" + ", ".join(f"'{k}'" for k in KINDS) + ")", name="ck_form_templates_kind"
        ),
        CheckConstraint(
            "valid_for_months IS NULL OR valid_for_months BETWEEN 1 AND 120",
            name="ck_form_templates_valid_for_months",
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
    # --- Task 8 (#51): compliance settings. Template *identity*, not the draft: edited
    # outside versioning (`PUT /admin/forms/{id}/settings`) because they say *when* a form is
    # required, not what it says — unlike `is_mandatory`/`is_health_form` above, which are
    # versioned because a submission's compliance must be judged by what was signed. The
    # essential-forms checklist itself is driven by the latest published version's
    # `is_mandatory` (owner ruling, progress.md), not a column here — see `forms/compliance.py`.
    applies_to_all: Mapped[bool] = mapped_column(Boolean, server_default=false())
    valid_for_months: Mapped[int | None] = mapped_column(SmallInteger)
    # Hidden from new links; its versions and every submission against them stay.
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class FormTemplateVersion(Base):
    __tablename__ = "form_template_versions"
    __table_args__ = (
        UniqueConstraint("template_id", "number", name="uq_form_template_versions_template_number"),
        CheckConstraint("number >= 1", name="ck_form_template_versions_number"),
        CheckConstraint(
            "kind IN (" + ", ".join(f"'{k}'" for k in KINDS) + ")",
            name="ck_form_template_versions_kind",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    template_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("form_templates.id"))
    number: Mapped[int] = mapped_column(Integer)
    # The title and kind as published — what a link shows and a signed record is called.
    # `FormTemplate.name` is only the draft's label and changes freely.
    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(Text)
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


class FormLink(Base):
    """A secure link: one client, one frozen version, 48 hours, used once (Task 3, #46).

    Only the SHA-256 of the token is stored — the token itself exists in the URL handed back
    once and in the email, so a database dump yields no usable link. Mutable on purpose: the
    app role stamps `consumed_at` (Task 4) and `revoked_at`. No ciphertext, so no key FK."""

    __tablename__ = "form_links"
    __table_args__ = (
        CheckConstraint("expires_at > issued_at", name="ck_form_links_expiry"),
        CheckConstraint("octet_length(token_sha256) = 32", name="ck_form_links_sha256"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    token_sha256: Mapped[bytes] = mapped_column(LargeBinary, unique=True)
    customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("customers.id"), index=True)
    version_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("form_template_versions.id"))
    issued_by_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class FormSubmission(Base):
    """A filled-in form: immutable, and its answers sealed under the client's DEK (#47).

    `answers_sealed` is the only copy of the answers — signature and typed name included —
    and `forms.submissions.open_answers` the only reader. It references the *version* it was
    filled against, so republishing never changes what a record says. Append-only for the app
    role (0029: REVOKE and `customer_record_guard`); only the purge role deletes, only while the
    client is not held. Its FK is to the client's key row, not `customers` (ADR-0001 rule 7)."""

    __tablename__ = "form_submissions"
    __table_args__ = (
        CheckConstraint("method IN ('link', 'scan')", name="ck_form_submissions_method"),
        CheckConstraint(
            "method = 'scan' OR (link_id IS NOT NULL AND answers_sealed IS NOT NULL)",
            name="ck_form_submissions_link_answers",
        ),
        Index(
            "ix_form_submissions_customer_template_submitted",
            "customer_id",
            "template_id",
            "submitted_at",
        ),
    )

    # Chosen by the client's page, so a retry after a lost answer is recognisably the same.
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("customer_document_keys.customer_id"))
    version_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("form_template_versions.id"))
    template_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("form_templates.id"))
    link_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("form_links.id"), unique=True)
    method: Mapped[str] = mapped_column(Text)
    answers_sealed: Mapped[bytes | None] = mapped_column(LargeBinary)
    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    source_ip: Mapped[str | None] = mapped_column(INET)


class FormTemplateService(Base):
    """Which services make a template apply to a client (Task 8, #51; owner ruling Q6 —
    services only, staff types dropped). The other half of applicability,
    `FormTemplate.applies_to_all`, needs no row here. Both FKs cascade: dropping a template or
    a service drops the mapping, never the other row. No client data — an ordinary app-role
    table, nothing to guard."""

    __tablename__ = "form_template_services"

    template_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("form_templates.id", ondelete="CASCADE"), primary_key=True
    )
    service_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("services.id", ondelete="CASCADE"), primary_key=True
    )
