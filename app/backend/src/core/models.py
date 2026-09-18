"""The business record and the audit log: the two tables that belong to no single domain.

Single-tenant means exactly one business, so `id` is pinned to 1 by a CHECK constraint —
the database, not the application, refuses a second business.

The profile and brand colours live on this row because every domain reads them — a receipt
needs the address and the currency symbol, the shell needs the colours. The two *images* do
not: they are in `settings/models.py`, off this row, so a megabyte of PNG is not loaded by
every request that only wanted the business name.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Identity,
    Integer,
    String,
    Text,
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
    # On by default (PRD §1, tech-stack §14): an account that can administer the business is
    # the one worth a second factor. Disableable, because a solo operator with one device is
    # otherwise one lost phone away from being locked out of their own business — which is
    # why this is a setting rather than a rule.
    mfa_required_for_admin: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    # Email as a *primary* second factor, off by default. It is lower assurance — NIST does
    # not recognise email as an out-of-band channel, since the inbox usually lives in the
    # same browser session an attacker already has — so a business opts into it knowingly.
    # It is available as the recovery fallback regardless; that is `auth/mfa.py`'s business.
    mfa_email_otp_allowed: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))

    # --- the profile (PRD §1, §7; written by `settings/routes.py`) -------------------------
    # The address is what the tax components will be derived from, so it is structured rather
    # than one free-text block: `province` is the field a later rate table joins on.
    address_line1: Mapped[str | None] = mapped_column(String(200))
    address_line2: Mapped[str | None] = mapped_column(String(200))
    city: Mapped[str | None] = mapped_column(String(100))
    province: Mapped[str | None] = mapped_column(String(2))
    postal_code: Mapped[str | None] = mapped_column(String(7))
    # Fixed for now. A column rather than a constant because an address without a country is
    # not an address, and the day a second one is supported this is where it is read from.
    country: Mapped[str] = mapped_column(String(2), server_default=text("'CA'"))
    phone: Mapped[str | None] = mapped_column(String(32))
    email: Mapped[str | None] = mapped_column(String(320))
    # Labelled free text, not a validated registration number. The formats differ per
    # jurisdiction and change; a business that mistypes its own GST number notices on the
    # first invoice, and a regex that refuses a valid one is unfixable from the screen.
    gst_hst_number: Mapped[str | None] = mapped_column(String(64))
    pst_qst_number: Mapped[str | None] = mapped_column(String(64))
    currency_symbol: Mapped[str] = mapped_column(String(8), server_default=text("'$'"))
    receipt_footer: Mapped[str | None] = mapped_column(Text)
    # Six-digit hex, lowercase. The CHECK is in the migration: these end up as CSS variable
    # values on `<html>`, so "is it really a colour" is worth asserting in two places.
    brand_primary: Mapped[str] = mapped_column(String(7), server_default=text("'#1d4ed8'"))
    brand_secondary: Mapped[str] = mapped_column(String(7), server_default=text("'#0f766e'"))

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
