"""The customer: for now, a name and how to reach them.

**Deliberately minimal.** Task 15 needs somebody to book an appointment for, and that is the
whole of what this row is: two names, an optional email, an optional phone. Phase 3 (#7) —
profiles, classification, retention rules, access auditing — extends this table in place
rather than replacing it, which is why it already has its own domain module and its own
audit event rather than living as a column on the appointment.

**Email is unique case-insensitively, when present** (`ux_customers_email`, partial). Two
records with the same address are one person entered twice, and the booking screen's search
would offer both. A customer with no email is common — a walk-in, a phone booking — and a
NULL never collides with another NULL.

**Phone is stored as digits only.** "(416) 555-0199" and "416.555.0199" are one number, and
a prefix search on the digits is the only search a receptionist with a caller on the line
can actually type.
"""

import uuid
from datetime import date, datetime

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    SmallInteger,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from core.db import Base
from customers.phone import NORMALIZED_PHONE_SQL

# What an erasure request removes whether or not the chart is held (pre-flight D5b): how to
# reach the client, both contacts, and the front-desk notes. Names and DOB go too only when
# nothing holds them — replaced by `ERASED_NAMES` and NULL, so the row survives as the
# anonymous tombstone the business's own appointment history points at.
ALWAYS_ERASED = (
    "email",
    "phone",
    "emergency_contact_name",
    "emergency_contact_phone",
    "emergency_contact_relationship",
    "secondary_contact_name",
    "secondary_contact_phone",
    "secondary_contact_email",
    "notes",
)
ERASED_NAMES = ("Erased", "Client")


class Customer(Base):
    __tablename__ = "customers"
    __table_args__ = (
        Index(
            "ux_customers_email",
            text("lower(email)"),
            unique=True,
            postgresql_where=text("email IS NOT NULL"),
        ),
        # A data-entry typo (year 1250, or next week) is caught with a better message by
        # `CustomerPatch`'s own validator; the database only needs the cheap half of the
        # invariant — nobody's birthday is in the future.
        CheckConstraint(
            "date_of_birth IS NULL OR date_of_birth <= CURRENT_DATE",
            name="ck_customers_dob_not_future",
        ),
        # The purge job's scan: `retention_expires_at < now()`.
        Index("ix_customers_retention_expires_at", "retention_expires_at"),
        # Phone lookup (Phase 14, #16): the NANP-normalised value, not a second denormalised
        # column — see `customers/phone.py` for the one expression both this index and the
        # lookup's query are built from.
        Index(
            "ix_customers_phone_normalized",
            text(NORMALIZED_PHONE_SQL),
            postgresql_where=text("phone IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    first_name: Mapped[str] = mapped_column(String(100))
    last_name: Mapped[str] = mapped_column(String(100))
    email: Mapped[str | None] = mapped_column(String(254))
    phone: Mapped[str | None] = mapped_column(String(32))
    # --- Task 3 (#38): the rest of PRD §2's profile ----------------------------------------
    date_of_birth: Mapped[date | None] = mapped_column(Date)
    # One of each, columns rather than a table — purge (Task 4/#39) is a `SET NULL` on these.
    emergency_contact_name: Mapped[str | None] = mapped_column(String(100))
    emergency_contact_phone: Mapped[str | None] = mapped_column(String(32))
    emergency_contact_relationship: Mapped[str | None] = mapped_column(String(100))
    secondary_contact_name: Mapped[str | None] = mapped_column(String(100))
    secondary_contact_phone: Mapped[str | None] = mapped_column(String(32))
    secondary_contact_email: Mapped[str | None] = mapped_column(String(254))
    # Front-desk notes — non-clinical. A session note is a different, lockable document
    # (Phase 9); this is "prefers the corner chair", not a chart entry.
    notes: Mapped[str | None] = mapped_column(Text)
    # --- Task 4 (#39): retention (ADR-0001) — written only by `customers/retention.py` ------
    # The latest form submission or session note in the chart (Phases 8/9). Not an appointment.
    last_clinical_entry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # NULL = not held; an instant = held until then; 'infinity' = held, DOB unknown. Derived
    # from the DOB, so it is PHI: only ever on the logged profile response.
    retention_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # --- Task 7 (#42): set by an erasure request. Hidden from lists, search and booking;
    # the profile still opens by id (and still logs), for the record that it was honoured.
    suppressed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CustomerDocumentKey(Base):
    """One client's data-encryption key, wrapped under `DOCUMENT_MASTER_KEY` (`keys.py`).

    Deleting this row is the crypto-shred (ADR-0001 §5): every document sealed under the key
    becomes unreadable at once. So it is written once and never rewritten —
    `linsuite_app` holds SELECT and INSERT only — and `customer_document_keys_guard` (0024)
    lets only the purge role delete it, and only when the client is not under a retention hold.
    No `ON DELETE CASCADE`: a cascade runs as the table owner, which the guard lets through, so
    deleting a customer would be a way round it."""

    __tablename__ = "customer_document_keys"

    customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("customers.id"), primary_key=True)
    # `core.crypto` format: base64 of nonce || ciphertext+tag. The nonce lives inside it.
    wrapped_key: Mapped[str] = mapped_column(Text)
    # Which master key wrapped it. Always 1 until master-key rotation exists (out of scope); it
    # is here so a rotation can re-wrap row by row and know which rows it has done.
    master_key_version: Mapped[int] = mapped_column(SmallInteger, server_default=text("1"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ErasureRequest(Base):
    """A client asked to be forgotten (ADR-0001 §3, pre-flight D5a/D5b) — and the record that
    the business honoured it. `linsuite_app` may not DELETE it (0025): it is the evidence.

    `held_until`/`held_reason` are what was held at the moment of asking (`'infinity'` when
    the chart has no DOB); the profile explains the *current* hold, which a DOB correction can
    move. `purged_at` is stamped once the key is shredded and the profile anonymised."""

    __tablename__ = "erasure_requests"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("customers.id"), index=True)
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    requested_by_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    note: Mapped[str | None] = mapped_column(Text)
    held_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    held_reason: Mapped[str | None] = mapped_column(Text)
    purged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
