"""The business record and the two audit logs: the tables that belong to no single domain.

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
    ForeignKey,
    Identity,
    Index,
    Integer,
    LargeBinary,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from core.db import Base

# The codes migration 0008's CHECK names, spelled out rather than imported: `core/` imports
# no domain module, and `settings.timezones.PROVINCES` is where the screen reads them from.
PROVINCE_CODES = ("AB", "BC", "MB", "NB", "NL", "NS", "NT", "NU", "ON", "PE", "QC", "SK", "YT")


class Business(Base):
    __tablename__ = "businesses"
    # Every rule the database actually holds, declared here too: a CHECK that exists only in
    # a migration is one the next `alembic revision --autogenerate` proposes dropping.
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_businesses_single_row"),
        CheckConstraint(
            "province IS NULL OR province IN (" + ", ".join(f"'{p}'" for p in PROVINCE_CODES) + ")",
            name="ck_businesses_province",
        ),
        # Canada Post's format, in the one shape the API normalises to: `A1A 1A1`, upper case.
        CheckConstraint(
            "postal_code IS NULL OR postal_code ~ '^[A-Z][0-9][A-Z] [0-9][A-Z][0-9]$'",
            name="ck_businesses_postal_code",
        ),
        CheckConstraint(
            "brand_primary ~ '^#[0-9a-f]{6}$' AND brand_secondary ~ '^#[0-9a-f]{6}$'",
            name="ck_businesses_brand_hex",
        ),
        CheckConstraint(
            "slot_granularity_minutes BETWEEN 5 AND 60 AND slot_granularity_minutes % 5 = 0",
            name="ck_businesses_slot_granularity",
        ),
        CheckConstraint(
            "booking_horizon_days BETWEEN 1 AND 365", name="ck_businesses_booking_horizon"
        ),
        CheckConstraint(
            "vip_visit_threshold BETWEEN 2 AND 1000", name="ck_businesses_vip_visit_threshold"
        ),
        CheckConstraint(
            "retention_profile IN ('regulated_health', 'general_business')",
            name="ck_businesses_retention_profile",
        ),
    )

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

    # --- what the availability engine reads (tech-stack §19; `scheduling/availability.py`) --
    # The step slot starts are offered on, from local midnight. Five-minute steps, at most an
    # hour; the CHECK is in migration 0013.
    slot_granularity_minutes: Mapped[int] = mapped_column(Integer, server_default="15")
    # How far ahead anything is computed. A year at most: a horizon caps the cost of a request.
    booking_horizon_days: Mapped[int] = mapped_column(Integer, server_default="90")
    # Classification (pre-flight D8): a client is "vip" once they reach this many `completed`
    # appointments. Never stored on the customer — computed at read time from the count, so
    # lowering this number reclassifies everybody on the next read with no write to `customers`.
    vip_visit_threshold: Mapped[int] = mapped_column(Integer, server_default="10")

    # --- retention (ADR-0001, pre-flight D3; `customers/retention.py`) ---------------------
    # Which obligation wins. Default retain-side: a salon left on `regulated_health` holds data
    # a little longer, a clinic left on `general_business` would purge a chart it must keep —
    # only the first mistake is recoverable. Switched on Settings → Security, never the wizard.
    retention_profile: Mapped[str] = mapped_column(
        String(32), server_default=text("'regulated_health'")
    )
    # When an administrator last saved the profile explicitly. NULL means nobody has chosen
    # yet, and the Security panel asks them to (the default is a default, not a decision).
    retention_profile_set_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

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
    # The two reads this table exists for: one actor's trail, and a window of time.
    __table_args__ = (
        Index("ix_audit_events_actor", "actor_user_id", "occurred_at"),
        Index("ix_audit_events_occurred_at", "occurred_at"),
    )

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


class AccessLogEntry(Base):
    """One PHI read: who opened whose record, as what, from where (ADR-0002 §1).

    A second table rather than more columns on `AuditEvent`, because it answers a different
    question — "who *looked*" — at a different volume, and is yearly-partitioned for it.
    `postgresql_partition_by` here is what keeps autogenerate from proposing to recreate the
    table flat; migration 0019 is where the partitions, the sequence and the trigger live.
    The partition key has to be in the primary key, hence `(id, occurred_at)`.

    Written only by `core.access_log.LogAccess`. Identifiers only, never what was seen.
    """

    __tablename__ = "audit_access_log"
    __table_args__ = (
        PrimaryKeyConstraint("id", "occurred_at"),
        Index("ix_audit_access_log_customer", "customer_id", "occurred_at"),
        Index("ix_audit_access_log_actor", "actor_user_id", "occurred_at"),
        {"postgresql_partition_by": "RANGE (occurred_at)"},
    )

    # An explicit sequence rather than `Identity()`: Postgres 16 refuses an identity column
    # on a partitioned table.
    id: Mapped[int] = mapped_column(
        BigInteger, server_default=text("nextval('audit_access_log_id_seq')")
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # No foreign keys, as on `AuditEvent`: the trail outlives what it describes.
    actor_user_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    # The role's name at the moment of access — roles rename, the log must not.
    actor_role: Mapped[str] = mapped_column(String(64))
    customer_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    resource_type: Mapped[str] = mapped_column(String(64))
    resource_id: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(16))
    ip: Mapped[str | None] = mapped_column(INET)


class Document(Base):
    """One sealed document — a signed consent, an intake form, a scan (`core/documents.py`).

    Immutable (CLAUDE.md): `linsuite_app` holds SELECT and INSERT only, and `documents_guard`
    (0026) refuses UPDATE and DELETE to everyone but the table owner — except DELETE by
    `linsuite_purge` for a client not under a retention hold. Written and read only through
    `store_document`/`fetch_document`.

    `customer_id` references the client's *key row*, not `customers`, with no cascade
    (ADR-0001 rule 7): inserting a document locks the key row it is sealed under, so a purge
    deleting that key serialises against the insert, and the purge must delete the documents
    first. A string FK, so `core/` still imports no domain module.
    """

    __tablename__ = "documents"
    __table_args__ = (
        # A re-run render of the same source is `ON CONFLICT DO NOTHING`, never a second copy.
        UniqueConstraint("kind", "source_id", name="uq_documents_kind_source_id"),
        CheckConstraint("octet_length(digest) = 32", name="ck_documents_digest_length"),
        Index("ix_documents_customer_id", "customer_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("customer_document_keys.customer_id"))
    # What produced it (`form_submission`, ...) and that thing's id.
    kind: Mapped[str] = mapped_column(Text)
    source_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    content_type: Mapped[str] = mapped_column(Text)
    # `core.crypto.seal`: nonce || ciphertext || tag.
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    # HMAC-SHA256 of the plaintext under a subkey of the client's DEK (`core/documents.py`),
    # checked after every decrypt. Keyed, not a bare hash: it dies with the key.
    digest: Mapped[bytes] = mapped_column(LargeBinary)
    size_bytes: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
