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
from datetime import datetime

from sqlalchemy import DateTime, Index, String, func, text
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
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    first_name: Mapped[str] = mapped_column(String(100))
    last_name: Mapped[str] = mapped_column(String(100))
    email: Mapped[str | None] = mapped_column(String(254))
    phone: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
