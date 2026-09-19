"""The staff member: the person behind an account, and the facts the rest of the app needs.

**Why this lives in `scheduling` and not in `settings`.** Almost everything on this row is
read by the domains that do the work: the calendar draws one column per staff member and
paints their appointments in `colour` (tech-stack §13), the overlap trigger reads
`max_concurrent_appointments` (§20), commission reporting reads the two basis-point rates and
treatment receipts read `designation` and `licence_number` (§21). Task 11's weekly
availability matrix hangs off this table and belongs beside it. `settings/` is this
deployment's own identity — the profile, the brand, the images — and a staff member is not
that; the fact that an administrator edits them from a settings tab is a property of the
screen, not of the data. The admin surface is `scheduling/staff.py`.

**One row per account, always.** `user_id` is NOT NULL and unique, the setup wizard creates
one for the first administrator, `POST /api/admin/staff` creates the pair together, and
migration 0009 backfilled every account that predated the table. So "the staff member behind
this session" is never a question with no answer, and no screen needs a branch for it.

**Deactivation, never deletion.** `active` going false ends every session and refuses the
next sign-in; the row stays, because appointments, invoices, treatment receipts and audit
entries all point at it and history that loses its author is not history.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from core.db import Base

# Commission is stored in basis points — integers, like every other money-adjacent number in
# this application (CLAUDE.md). 45% is 4500, and 100% is the ceiling: a rate above it would
# be a business paying out more than it took in, which is a typo rather than an arrangement.
MAX_BASIS_POINTS = 10_000


class Staff(Base):
    __tablename__ = "staff"
    __table_args__ = (
        CheckConstraint(
            "commission_rate_services_bp BETWEEN 0 AND 10000 "
            "AND commission_rate_retail_bp BETWEEN 0 AND 10000",
            name="ck_staff_commission_basis_points",
        ),
        CheckConstraint(
            "max_concurrent_appointments >= 1", name="ck_staff_max_concurrent_appointments"
        ),
        # The rule tech-stack §21 turns into a rejected insurance claim. Refused at the API
        # boundary with a 422 somebody can act on; this is what refuses it when the next
        # writer is a migration, an import or a psql session.
        CheckConstraint(
            "NOT is_practitioner OR ("
            "designation IS NOT NULL AND btrim(designation) <> '' "
            "AND licence_number IS NOT NULL AND btrim(licence_number) <> '')",
            name="ck_staff_practitioner_credentials",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    # CASCADE matches the rest of what hangs off an account. It is not a deletion path
    # anything uses — accounts are deactivated, never deleted — but if a row ever does go,
    # leaving a staff record pointing at nobody would be worse than losing it with them.
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True
    )
    first_name: Mapped[str] = mapped_column(String(100))
    last_name: Mapped[str] = mapped_column(String(100), server_default="")
    # What the calendar column, the appointment block and a receipt are headed with. Defaults
    # to the two names joined, and is its own column because "Dr. Okonkwo" and "Ana R." are
    # what businesses actually put on a schedule.
    display_name: Mapped[str] = mapped_column(String(200))
    is_practitioner: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    # "RMT", "RMT #12345" (tech-stack §21). Free text: designations differ per college and
    # per province, and a pattern that refused a valid one would be unfixable from the screen.
    designation: Mapped[str | None] = mapped_column(String(64))
    licence_number: Mapped[str | None] = mapped_column(String(64))
    commission_rate_services_bp: Mapped[int] = mapped_column(Integer, server_default="0")
    commission_rate_retail_bp: Mapped[int] = mapped_column(Integer, server_default="0")
    # A key from `scheduling/palette.py`, not a hex: the palette is curated for legibility and
    # carries a second value for the dark theme, so storing a hex would store half the answer
    # and let a future freehand picker write an unreadable one.
    colour: Mapped[str] = mapped_column(String(16))
    # Policy, not physics (tech-stack §20): 2 is the stylist running two chairs through a
    # colour process. The trigger that enforces it arrives with the booking ticket.
    max_concurrent_appointments: Mapped[int] = mapped_column(Integer, server_default="1")
    active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    # The calendar's column order. A tie is broken by display name, so equal values are a
    # stable list rather than whatever the planner returns.
    sort_order: Mapped[int] = mapped_column(Integer, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Resource(Base):
    """A space or a piece of equipment: the physical things a service is delivered in and
    with (PRD §1, tech-stack §15, §20).

    **One table for both, told apart by `kind`.** They carry the same facts — a name, an
    optional colour to paint the schedule with, whether they can still be booked — and a
    booking only cares that a resource exists and is active, never which kind it is. Two
    tables would be two copies of every query that later reads either.

    **`kind` is a CHECK-constrained string, not a Postgres enum**, consistent with `staff`'s
    `active`/practitioner rules: adding a third kind is an ALTER TABLE either way, and a
    plain column keeps this file free of a dialect-specific enum type migration.

    **Names are unique per kind, case-insensitively** — `ux_resources_kind_name` below — so
    "Room 1" and "room 1" collide but the space "Room 1" and the equipment "Room 1" do not.

    **No hard delete.** `active` going false is the only ending: a booking that already
    claimed this resource must never lose what it pointed at. Later tickets' pickers read
    `active` to leave it off the list; this table never refuses a write because of it.
    """

    __tablename__ = "resources"
    __table_args__ = (
        CheckConstraint("kind IN ('space', 'equipment')", name="ck_resources_kind"),
        Index("ux_resources_kind_name", "kind", text("lower(name)"), unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    kind: Mapped[str] = mapped_column(String(16))
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    # A key from `scheduling/palette.py`, like `staff.colour` — optional here, because a
    # resource showing up on the schedule in no particular colour is a reasonable start.
    colour: Mapped[str | None] = mapped_column(String(16))
    active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    sort_order: Mapped[int] = mapped_column(Integer, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
