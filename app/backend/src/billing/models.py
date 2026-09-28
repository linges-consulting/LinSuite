"""The service bill: what `scheduling/appointments.py::complete_appointment` (#59) creates or
appends to, one line per completed appointment, grouped onto one bill per visit.

**Why "service bill" and not "invoice".** CLAUDE.md ("Domain rules"): services and retail
invoice separately, and an invoice is a *voidable* document — never edited in place, issued
once, cancelled and replaced rather than mutated (tech-stack, document immutability). A draft
is the opposite of that on purpose: it is appended to, line by line, as sibling appointments in
one visit complete, right up until #65 issues it. Calling the mutable, pre-issue thing a
"bill" and the immutable, post-issue thing an "invoice" keeps those two document classes from
sharing a name while #65 (which reads this table and writes the issued document) hasn't landed
yet.

**Visit grouping reuses `Appointment.booking_group_id` directly** (m4.md "Reusable patterns") —
no new grouping concept. `ServiceBill.booking_group_id` is nullable for the same reason the
appointment column is: an appointment booked alone has no group, and its bill never needs to
be found by anything but its own id, so nothing here forces one.

**Commission rate is snapshotted onto the line, not the bill, at completion — not at invoice
issue (#54's explicit M4 reversal of the older assumption, m4.md).** `Staff.commission_rate_
services_bp` can change between completion and issue; the line is what a later commission
report reads, and it must keep reading what was true the moment the work was delivered.

**Status is `draft`/`issued` even though nothing in this ticket ever sets `issued`** — #65 is
what does that. The column exists now because #59's own acceptance criteria require it: a
later completion for a visit whose bill has already moved past `draft` must open a new one
rather than appending to a document that's supposed to be immutable from that point on.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.db import Base


class ServiceBill(Base):
    """One visit's draft (or, later, issued) service bill. `customer_id` is copied off the
    first appointment that creates it — every appointment in one `booking_group_id` shares a
    customer (a visit is for one client), so there is nothing to reconcile across lines."""

    __tablename__ = "service_bills"
    __table_args__ = (
        CheckConstraint("status IN ('draft', 'issued')", name="ck_service_bills_status"),
        # The lookup #59's completion hook runs on every grouped completion: "the open draft
        # for this visit, if one exists yet."
        Index("ix_service_bills_group_status", "booking_group_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("customers.id"))
    # Task 18's visit tag (scheduling/models.py) — null for a single, ungrouped appointment's
    # own bill, which nothing else is ever grouped onto.
    booking_group_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    status: Mapped[str] = mapped_column(String(16), server_default=text("'draft'"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    lines: Mapped[list["ServiceBillLine"]] = relationship(
        cascade="all, delete-orphan", lazy="selectin", passive_deletes=True
    )


class ServiceBillLine(Base):
    """One completed appointment's contribution to its visit's bill. `appointment_id` is
    unique — an appointment appears on at most one line, ever — belt-and-braces alongside the
    real guarantee, which is `complete_appointment`'s own `_lock`/status-guard already
    refusing a retried completion before the hook that inserts this row ever runs (m4.md
    "the completion transaction to hook into"; this table adds no second dedup mechanism of
    its own, because it does not need one)."""

    __tablename__ = "service_bill_lines"
    __table_args__ = (
        CheckConstraint("price_cents >= 0", name="ck_service_bill_lines_price"),
        CheckConstraint(
            "commission_rate_bp BETWEEN 0 AND 10000", name="ck_service_bill_lines_commission_bp"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    bill_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("service_bills.id", ondelete="CASCADE"))
    appointment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("appointments.id"), unique=True)
    service_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("services.id"))
    staff_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("staff.id"))
    # The appointment's own price snapshot, copied rather than referenced — the same
    # SNAPSHOT CONTRACT `Appointment.price_cents` itself follows against `Service` (CLAUDE.md,
    # scheduling/models.py): a later price change on the service must never rewrite a bill
    # line for work already delivered.
    price_cents: Mapped[int] = mapped_column(Integer)
    # The snapshot this whole ticket exists for: `Staff.commission_rate_services_bp` *at
    # completion*, never re-read at invoice issue (#65) or report time (#69).
    commission_rate_bp: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
