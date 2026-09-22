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

from sqlalchemy import CheckConstraint, Date, DateTime, Index, String, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column

from core.db import Base


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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
