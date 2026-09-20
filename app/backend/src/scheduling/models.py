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
from datetime import date as Date
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
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy import (
    Date as DateColumn,
)
from sqlalchemy.dialects.postgresql import TSTZRANGE, ExcludeConstraint, Range
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.db import Base
from customers.models import Customer  # noqa: F401 — the `Appointment.customer` relationship

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


# Minutes since local midnight, and the step the matrix editor offers. Five rather than one
# because a shift that starts at 09:07 is a typo, and a picker with 1440 options is not a
# picker. Both halves of the rule are CHECK constraints in migration 0011.
MINUTES_IN_DAY = 1440
MINUTE_STEP = 5


class WorkingHours(Base):
    """One block of a staff member's recurring week — **local wall-clock, never an instant.**

    `(weekday, start_minute, end_minute)` with no timezone on the row is the whole point
    (CLAUDE.md "Time", PRD §1, tech-stack §19). "Works Mondays 09:00–12:00" is a rule about a
    clock face: stored as a UTC instant it would slide by an hour at every DST boundary, and
    the first symptom would be clients arriving to an empty room twice a year. The zone comes
    from `Business.timezone` at read time, applied by `scheduling/clock.py`, which is also why
    changing that setting cannot corrupt anything here — there is nothing on this row to
    reinterpret.

    **Several rows on one weekday are the split shift.** 09:00–12:00 and 15:00–18:00 is two
    rows; the unavailable middle is expressed by its absence rather than by a third row saying
    "not working", which would be a second way to say the same thing and a second way to get
    it wrong.

    **Overlap is refused by the database.** `ex_working_hours_no_overlap` (migration 0011) is
    `EXCLUDE USING gist (staff_id WITH =, weekday WITH =, int4range(start, end) WITH &&)`, so
    a code path that forgets to check gets a constraint violation rather than a staff member
    booked into two shifts at once. `int4range` is half-open, so 09:00–12:00 and 12:00–15:00
    touch without overlapping — somebody who does not take lunch, not a conflict.

    `weekday` is ISO with Monday = 0, matching `datetime.weekday()`, so no caller ever
    translates.
    """

    __tablename__ = "working_hours"
    __table_args__ = (
        CheckConstraint("weekday BETWEEN 0 AND 6", name="ck_working_hours_weekday"),
        # 1440 is allowed on the end: a block may run to midnight.
        CheckConstraint(
            f"start_minute >= 0 AND end_minute <= {MINUTES_IN_DAY} AND start_minute < end_minute",
            name="ck_working_hours_within_the_day",
        ),
        CheckConstraint(
            f"start_minute % {MINUTE_STEP} = 0 AND end_minute % {MINUTE_STEP} = 0",
            name="ck_working_hours_five_minute_steps",
        ),
        # The rule the API never has to trust itself about. Declared here as well as in
        # migration 0011 so the metadata is not a smaller schema than the database: a model
        # that omits it would have `--autogenerate` emitting a DROP for it on the next
        # revision somebody writes. `tests/test_hours.py` compares the two.
        ExcludeConstraint(
            ("staff_id", "="),
            ("weekday", "="),
            (text("int4range(start_minute, end_minute)"), "&&"),
            name="ex_working_hours_no_overlap",
            using="gist",
        ),
        Index("ix_working_hours_staff_week", "staff_id", "weekday", "start_minute"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    staff_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("staff.id", ondelete="CASCADE"))
    weekday: Mapped[int] = mapped_column(Integer)
    start_minute: Mapped[int] = mapped_column(Integer)
    # May be 1440 — a block that runs to midnight. `time` has no 24:00, which is one more
    # reason these are integers rather than two `time` columns.
    end_minute: Mapped[int] = mapped_column(Integer)


class TimeOff(Base):
    """A specific absence: vacation, an appointment, a day owed (PRD §1).

    **Instants, unlike working hours.** "Away 1–5 July" is a moment in this business's life,
    not a recurring rule, so `timestamptz` is right and wall-clock would be wrong. The API
    accepts local dates or local datetimes and converts with the business timezone at write
    time; the boundary is the only place that conversion happens.

    `all_day` is kept rather than inferred from the instants, because "the whole of 1 July"
    and "00:00 to 00:00" stop being the same sentence the moment the business timezone
    changes — and the screen has to render the one that was meant.

    Overlap for one staff member is refused by `ex_time_off_no_overlap`. Two entries that
    overlap are one absence entered twice, and leaving both would make "is she away?" a
    question with two answers.

    Advisory at booking time, absolutely (tech-stack §22): a human with
    `schedule.override_availability` may book over this, logged. That rule belongs to the
    booking ticket; this table only records the fact.
    """

    __tablename__ = "time_off"
    __table_args__ = (
        CheckConstraint("starts_at < ends_at", name="ck_time_off_span"),
        ExcludeConstraint(
            ("staff_id", "="),
            (text("tstzrange(starts_at, ends_at)"), "&&"),
            name="ex_time_off_no_overlap",
            using="gist",
        ),
        Index("ix_time_off_staff_start", "staff_id", "starts_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    staff_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("staff.id", ondelete="CASCADE"))
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    all_day: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    # Optional, and deliberately free text. A dropdown of reasons would be a list of a
    # colleague's private business, enumerated.
    reason: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Closure(Base):
    """A day the business is shut — a statutory holiday, or one it chose.

    **One table, one row per date, and `source` is a label rather than a rule.** A statutory
    holiday imported from the `holidays` package and a manually added staff retreat block
    bookings identically; the only thing `source` changes is what an import is allowed to skip
    and what the screen calls the row.

    **No computation at read time, and no overrides table.** The alternative — deriving the
    holiday list on every availability query, with a second table saying which ones this
    business ignores — is two mechanisms for one answer. Importing a year writes rows, and
    deleting one means the business works that day. That is the whole model, and its one
    sharp edge is that re-importing the same year adds a deleted statutory day back; the
    screen says so before the delete rather than growing a tombstone table to prevent it.
    """

    __tablename__ = "closures"
    __table_args__ = (
        CheckConstraint("source IN ('manual', 'statutory')", name="ck_closures_source"),
        # Named rather than left to `unique=True` on the column: Postgres would call it
        # `closures_date_key`, which is a name no file in this repository contains, and the
        # drift test compares by name. Everything else here is named too.
        UniqueConstraint("date", name="uq_closures_date"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    # A local calendar date, not an instant: "Canada Day" is a day, and which instants it
    # covers is a question for `Business.timezone` at the moment it is asked.
    date: Mapped[Date] = mapped_column(DateColumn)
    name: Mapped[str] = mapped_column(String(200))
    source: Mapped[str] = mapped_column(String(16), server_default=text("'manual'"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# The catalog's own step and floor. Five-minute steps for the same reason the weekly matrix
# uses them — a 47-minute service is a typo — and a floor of one step, because a service of
# no length is not something the engine can slide across a day.
MIN_DURATION = 5


class Service(Base):
    """What the business sells, and what delivering one costs in time, money and things.

    **This row is the availability engine's input** (tech-stack §19). `duration_minutes` plus
    `buffer_before_minutes` and `buffer_after_minutes` is the width slid across a staff
    member's free intervals; `ServiceRequirement` says which rooms' and devices' free
    intervals that has to be intersected with; `ServiceStaff` says whose day to look at.
    Buffers are stored separately from the duration rather than baked into it because the
    appointment block a client sees is the duration, and the turnaround either side of it is
    not time anybody booked.

    **Money is integer cents** (CLAUDE.md, tech-stack §21). The screen shows dollars and
    converts; nothing between here and the invoice ever holds a float.

    **SNAPSHOT CONTRACT — Task 15 must copy, never reference.** Editing a service must never
    alter an appointment already booked against it (PRD §2). So booking copies
    `duration_minutes`, `buffer_before_minutes`, `buffer_after_minutes` and `price_cents`
    onto the appointment row at the moment it is made, and every later read — the calendar
    block, the invoice line, the treatment receipt — reads the copy. An appointment that
    joined back to this table for its price would silently rewrite last month's takings the
    first time somebody raised a rate. This is the same rule as the commission rate
    snapshotted on an invoice line, for the same reason.

    **Names are unique case-insensitively** — `ux_services_name` — because "Swedish Massage"
    and "swedish massage" are one service entered twice, and a booking screen offering both
    is a coin flip over which one the reports add up.

    **No hard delete** (tech-stack §15, §20): `active` going false takes it off every picker
    and leaves every appointment that already claimed it pointing at something real.

    **Not here, deliberately:** packages and bundles (M4), per-item tax components (out of
    M1), pricing tiers. Each is a table of its own when it arrives, not a column added here.
    """

    __tablename__ = "services"
    __table_args__ = (
        CheckConstraint(
            f"duration_minutes >= {MIN_DURATION} AND duration_minutes % {MINUTE_STEP} = 0",
            name="ck_services_duration",
        ),
        CheckConstraint(
            f"buffer_before_minutes >= 0 AND buffer_before_minutes % {MINUTE_STEP} = 0 "
            f"AND buffer_after_minutes >= 0 AND buffer_after_minutes % {MINUTE_STEP} = 0",
            name="ck_services_buffers",
        ),
        # Integer cents, and never negative. A discount is a line on an invoice, not a
        # service priced below nothing.
        CheckConstraint("price_cents >= 0", name="ck_services_price"),
        Index("ux_services_name", text("lower(name)"), unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    duration_minutes: Mapped[int] = mapped_column(Integer)
    buffer_before_minutes: Mapped[int] = mapped_column(Integer, server_default="0")
    buffer_after_minutes: Mapped[int] = mapped_column(Integer, server_default="0")
    price_cents: Mapped[int] = mapped_column(Integer, server_default="0")
    # False is "staff may book it, the public portal may not offer it" — a consultation a
    # receptionist schedules by hand, not a service that has been withdrawn.
    bookable_online: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    sort_order: Mapped[int] = mapped_column(Integer, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Both sets are loaded with the service, always: every caller of this table wants them,
    # and an unloaded relationship on an async session is an error rather than a second
    # query. `delete-orphan` is what makes "replace the whole set" one assignment.
    eligible_staff: Mapped[list["ServiceStaff"]] = relationship(
        cascade="all, delete-orphan", lazy="selectin", passive_deletes=True
    )
    requirements: Mapped[list["ServiceRequirement"]] = relationship(
        cascade="all, delete-orphan", lazy="selectin", passive_deletes=True
    )


class ServiceStaff(Base):
    """Who may deliver a service — a direct many-to-many, with no category layer.

    The PRD words this as "staff categories permitted to deliver a service". A category table
    between the two would be a second thing to maintain for a business with four
    practitioners, and the question every caller actually asks is "can Ana do this?" — which
    a direct pair answers without a join nobody asked for. If a business ever has enough
    staff for categories to save work, they are a grouping *over* these rows rather than a
    replacement for them.

    Composite primary key, so the same pair cannot be stored twice; the API replaces the
    whole set rather than adding to it, which is what makes that a sufficient rule.
    """

    __tablename__ = "service_staff"

    service_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("services.id", ondelete="CASCADE"), primary_key=True
    )
    staff_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("staff.id", ondelete="CASCADE"), primary_key=True
    )

    # The person, not just their id: `GET /catalog/services` has to leave a departed
    # practitioner out of what it tells the availability engine, and "is this one still
    # here?" is a question the id alone cannot answer. Joined rather than selectin — one row
    # per link, and the links are already being loaded by their service.
    staff: Mapped["Staff"] = relationship(lazy="joined")


class ServiceRequirement(Base):
    """One thing delivering a service needs: a kind of resource, or one exact resource.

    `resource_id IS NULL` means **any active resource of that kind** — "a treatment room, any
    of them", which is what most services want. A set `resource_id` means that one device or
    that one room, and its `kind` has to agree with this row's. Several rows are the normal
    case: any space *and* laser unit 2 is two rows, and that is precisely the requirement the
    availability engine needs in order to refuse a slot when the device is busy though the
    practitioner and every room are free (tech-stack §19 step 3).

    **Why the kind is on the row at all** when a named resource already carries one: without
    it there would be no way to say "any space", which is the majority case. With it, a named
    resource's row says the same thing twice — so the API checks they agree and refuses when
    they do not. A composite foreign key onto `(resources.id, kind)` would let the database
    enforce that, at the cost of a second unique constraint on `resources` and a rule stated
    in two places; the API is already the only writer here, and it has to reject an *inactive*
    resource in the same breath, which no constraint can express.
    """

    __tablename__ = "service_requirements"
    __table_args__ = (
        CheckConstraint("kind IN ('space', 'equipment')", name="ck_service_requirements_kind"),
        Index("ix_service_requirements_service", "service_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    service_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("services.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(16))
    resource_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("resources.id", ondelete="CASCADE")
    )

    # For the resource's name in the response. Joined rather than selectin: it is one row per
    # requirement and the requirements are already being loaded by their service.
    resource: Mapped["Resource | None"] = relationship(lazy="joined")


# The four states an appointment moves through (PRD §3). A string with a CHECK rather than a
# Postgres enum, like `resources.kind`: adding a state is an ALTER either way.
APPOINTMENT_STATUSES = ("confirmed", "completed", "cancelled", "no_show")


class Appointment(Base):
    """One service, one staff member, one client, one span of time (CLAUDE.md "Domain rules").

    **The service is snapshotted, never referenced** (`Service`'s SNAPSHOT CONTRACT).
    `duration_minutes`, the two buffers and `price_cents` are copied here at booking and read
    from here ever after — the calendar block, the invoice line, the treatment receipt. The
    `service_id` stays for the name and for reporting, not for any number.

    **`starts_at`/`ends_at` are the span the client sees**, as `timestamptz`. The buffered
    span — what the staff member and the rooms are actually occupied for — is derived where
    it is compared: `scheduling/slots.busy_intervals` for the engine, the trigger below for
    the staff limit, and `AppointmentResource.period` stores it outright for the constraint.
    Three places, one arithmetic: `[starts_at - before, ends_at + after)`.

    **Staff overlap is enforced by `tg_appointments_staff_concurrency`** (migration 0014,
    tech-stack §20), a trigger rather than a constraint because the limit is a number on
    the staff row and constraints cannot read one. It fires before every insert and update,
    skips cancelled rows, counts the overlapping non-cancelled ones over their buffered
    spans, and refuses when that count has reached `staff.max_concurrent_appointments`. It
    locks the staff row first, which is what turns two simultaneous bookings for one person
    into one booking and one refusal.

    No cascades on the three foreign keys: history must never lose its author.
    """

    __tablename__ = "appointments"
    __table_args__ = (
        CheckConstraint(
            "status IN ('confirmed', 'completed', 'cancelled', 'no_show')",
            name="ck_appointments_status",
        ),
        CheckConstraint("starts_at < ends_at", name="ck_appointments_span"),
        CheckConstraint(
            "duration_minutes > 0 AND buffer_before_minutes >= 0 "
            "AND buffer_after_minutes >= 0 AND price_cents >= 0",
            name="ck_appointments_snapshot",
        ),
        Index("ix_appointments_staff_start", "staff_id", "starts_at"),
        Index("ix_appointments_customer", "customer_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("customers.id"))
    staff_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("staff.id"))
    service_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("services.id"))
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    duration_minutes: Mapped[int] = mapped_column(Integer)
    buffer_before_minutes: Mapped[int] = mapped_column(Integer)
    buffer_after_minutes: Mapped[int] = mapped_column(Integer)
    price_cents: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), server_default=text("'confirmed'"))
    # Task 18: the linked appointments of one multi-service visit share this.
    booking_group_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    notes: Mapped[str | None] = mapped_column(Text)
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Every reader of an appointment wants the three names on it, so they load with the row.
    customer: Mapped["Customer"] = relationship(lazy="joined")
    staff: Mapped["Staff"] = relationship(lazy="joined")
    service: Mapped["Service"] = relationship(lazy="joined")
    resources: Mapped[list["AppointmentResource"]] = relationship(
        cascade="all, delete-orphan", lazy="selectin", passive_deletes=True
    )


class AppointmentResource(Base):
    """One space or device an appointment claims, for its whole buffered span.

    **`period` is the buffered span** — `[starts_at - before, ends_at + after)` — because the
    room is occupied while it is being turned over. Stored rather than derived so that
    `ex_appointment_resources_no_overlap` (tech-stack §15, migration 0014) can compare it:
    `EXCLUDE USING gist (resource_id WITH =, period WITH &&)`. A chair cannot be in two
    places, and this is the database saying so to every code path there will ever be.

    **No WHERE clause on the constraint.** Cancelling an appointment (Task 18) deletes these
    rows, so a cancelled booking frees its room by no longer claiming it. `ON DELETE CASCADE`
    from the appointment is the same idea for a row that goes altogether.

    `kind` is copied from the resource so the calendar can say "Room 1" and "Laser 2" apart
    without a join, and so the constraint's index says which kind of clash it refused.
    """

    __tablename__ = "appointment_resources"
    __table_args__ = (
        CheckConstraint("kind IN ('space', 'equipment')", name="ck_appointment_resources_kind"),
        ExcludeConstraint(
            ("resource_id", "="),
            ("period", "&&"),
            name="ex_appointment_resources_no_overlap",
            using="gist",
        ),
    )

    appointment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("appointments.id", ondelete="CASCADE"), primary_key=True
    )
    resource_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("resources.id"), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))
    period: Mapped[Range[datetime]] = mapped_column(TSTZRANGE)

    resource: Mapped["Resource"] = relationship(lazy="joined")
