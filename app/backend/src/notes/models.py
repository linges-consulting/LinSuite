import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from core.db import Base


class NoteTemplate(Base):
    __tablename__ = "note_templates"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    name: Mapped[str] = mapped_column(String(200))
    fields: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    diagram_ids: Mapped[list[str]] = mapped_column(JSONB)
    active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))


class SessionNote(Base):
    __tablename__ = "session_notes"
    __table_args__ = (
        CheckConstraint("revision >= 1", name="ck_session_notes_revision"),
        CheckConstraint(
            "(locked_at IS NULL) = (locked_by_user_id IS NULL)", name="ck_session_notes_lock_actor"
        ),
        CheckConstraint(
            "updated_at >= created_at AND (locked_at IS NULL OR locked_at >= created_at)",
            name="ck_session_notes_times",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    # No cascade: these sealed rows must be removed before the key in one purge transaction.
    customer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("customer_document_keys.customer_id"), index=True
    )
    appointment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("appointments.id"))
    author_staff_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("staff.id"))
    template_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("note_templates.id"))
    template_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB)
    content_sealed: Mapped[bytes] = mapped_column(LargeBinary)
    revision: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
