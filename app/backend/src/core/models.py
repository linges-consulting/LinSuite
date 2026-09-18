"""The business record and the audit log: the two tables that belong to no single domain.

Single-tenant means exactly one business, so `id` is pinned to 1 by a CHECK constraint —
the database, not the application, refuses a second business. Branding columns arrive with
the branding task; only what the setup wizard collects lives here.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Identity,
    Integer,
    String,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from core.db import Base


class Business(Base):
    __tablename__ = "businesses"
    __table_args__ = (CheckConstraint("id = 1", name="ck_businesses_single_row"),)

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, server_default="1", autoincrement=False
    )
    name: Mapped[str] = mapped_column(String(200))
    # IANA name. Recurring availability is wall-clock against this; see CLAUDE.md "Time".
    timezone: Mapped[str] = mapped_column(String(64))
    # Set once, when the setup wizard completes. Non-null permanently disables setup.
    setup_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Periodic password expiry. Null — the default — is off, which is the NIST position:
    # rotation without evidence of compromise degrades password quality. Exposed only
    # because some insurers and colleges require it (PRD §1, tech-stack §14).
    password_rotation_days: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AuditEvent(Base):
    """Append-only: who did what, and when (ADR-0002).

    `linsuite_app` has INSERT and SELECT and nothing else — UPDATE and DELETE are revoked
    *and* a trigger raises on both. Two mechanisms because they fail differently: a grant
    is undone by one careless `GRANT ALL`, and a trigger is undone by one `DISABLE
    TRIGGER`. Erasure under the retention policy is the purge role's job, never this one.

    Identifiers only. The content of whatever was viewed or changed never comes in here;
    copying it would duplicate personal health information into a second table with a
    different retention horizon.
    """

    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    # Deliberately not a foreign key. An audit trail outlives the accounts it describes:
    # a FK would either block the retention purge or, with ON DELETE SET NULL, make
    # deleting a user an UPDATE of this table — which the append-only trigger refuses.
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    event_type: Mapped[str] = mapped_column(String(64))
    target_type: Mapped[str] = mapped_column(String(64))
    target_id: Mapped[str | None] = mapped_column(String(64))
    event_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, server_default=text("'{}'::jsonb")
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
