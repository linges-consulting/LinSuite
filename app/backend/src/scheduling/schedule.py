"""`GET /api/schedule`: everything the calendar grid draws, in one read.

The grid needs five things for a range of business-local days — who has a column, when each
of them works, when they are away, when the business is shut, and what is booked — and it
needs them together: a column shaded for a shift that has not arrived yet, or an appointment
drawn before the time off under it, is a grid that is briefly wrong. One response, one
moment.

**Working blocks come back as UTC instants**, converted per date through
`clock.local_blocks_to_instants` — the same conversion the engine makes, so the shading on
screen and the slots the server offers are one picture of the day. The rule itself (minutes
since local midnight) never leaves the server; a screen that had to localize it would be a
second implementation of the DST arithmetic, and the one on screen would be the one that
drifts.

`schedule.view`, which every staff member holds. This is also the door through which a
staff member sees **their own hours**: `/admin/staff/{id}/hours` is the editor and stays
behind `users.manage`, but a person's own shift on their own calendar was never an
administrative fact.

The other reads stay: `/api/appointments` for a list, `/api/staff` for the roster.
"""

import uuid
from datetime import date as Date
from datetime import datetime, time, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import or_, select

from auth.capabilities import Requires
from auth.models import User
from core.db import SessionDep
from core.models import Business
from scheduling.appointments import AppointmentOut, appointments_between
from scheduling.clock import local_blocks_to_instants, localize
from scheduling.models import Appointment, Closure, Staff, TimeOff, WorkingHours
from scheduling.palette import BY_KEY
from scheduling.slots import check_range, utc
from scheduling.time_off import business_zone

router = APIRouter(prefix="/schedule", tags=["schedule"])

Viewer = Annotated[User, Depends(Requires("schedule.view"))]


class ColumnOut(BaseModel):
    id: str
    display_name: str
    colour: str
    hex: str
    dark_hex: str
    # The column header's "×2": how many appointments this person may run at once (§20).
    max_concurrent_appointments: int


class BlockOut(BaseModel):
    """One shift on one local date, as instants. `date` is the local date the rule was read
    against — which is not always the UTC date of `starts_at`."""

    staff_id: str
    date: str
    starts_at: str
    ends_at: str


class TimeOffOut(BaseModel):
    id: str
    staff_id: str
    all_day: bool
    reason: str | None
    starts_at: str
    ends_at: str


class ClosureOut(BaseModel):
    id: str
    date: str
    name: str


class ScheduleOut(BaseModel):
    timezone: str
    granularity_minutes: int
    staff: list[ColumnOut]
    working_blocks: list[BlockOut]
    time_off: list[TimeOffOut]
    closures: list[ClosureOut]
    appointments: list[AppointmentOut]


@router.get("")
async def read_schedule(
    _: Viewer,
    db: SessionDep,
    from_: Annotated[Date, Query(alias="from")],
    to: Date,
    staff_id: uuid.UUID | None = None,
    include_cancelled: bool = False,
) -> ScheduleOut:
    """Business-local dates, `to` inclusive, at most 31 days; `staff_id` narrows every list
    to one column. Cancelled appointments are left out unless `include_cancelled=true` — the
    grid's "Show cancelled" toggle (Task 18)."""
    check_range(from_, to)
    zone = await business_zone(db)
    granularity = await db.scalar(select(Business.slot_granularity_minutes).where(Business.id == 1))
    dates = [from_ + timedelta(days=n) for n in range((to - from_).days + 1)]
    window = (
        localize(datetime.combine(from_, time.min), zone),
        localize(datetime.combine(to + timedelta(days=1), time.min), zone),
    )

    # A column for everyone who works here **and** for anyone who does not any more but is
    # still booked in this window (fix wave, finding 9): deactivating somebody neither
    # cancels nor moves what they were booked for, and a card with no column is a client
    # nobody at the desk can see, let alone cancel or hand to a colleague. Their shift and
    # time off are *not* drawn — the column is there for the cards on it, not to suggest
    # they can be booked.
    booked = select(Appointment.staff_id).where(
        Appointment.starts_at >= window[0],
        Appointment.starts_at < window[1],
        *([] if include_cancelled else [Appointment.status != "cancelled"]),
    )
    roster = list(
        await db.scalars(
            select(Staff)
            .where(
                or_(Staff.active, Staff.id.in_(booked)),
                *([Staff.id == staff_id] if staff_id else []),
            )
            .order_by(Staff.sort_order, Staff.display_name)
        )
    )
    ids = [s.id for s in roster if s.active]

    hours: dict[uuid.UUID, dict[int, list[tuple[int, int]]]] = {s: {} for s in ids}
    for row in await db.execute(
        select(WorkingHours)
        .where(WorkingHours.staff_id.in_(ids))
        .order_by(WorkingHours.weekday, WorkingHours.start_minute)
    ):
        block = row[0]
        hours[block.staff_id].setdefault(block.weekday, []).append(
            (block.start_minute, block.end_minute)
        )
    blocks = [
        BlockOut(staff_id=str(s), date=day.isoformat(), starts_at=utc(start), ends_at=utc(end))
        for s in ids
        for day in dates
        for start, end in local_blocks_to_instants(hours[s].get(day.weekday(), ()), day, zone)
    ]

    absences = await db.scalars(
        select(TimeOff)
        .where(
            TimeOff.staff_id.in_(ids), TimeOff.ends_at > window[0], TimeOff.starts_at < window[1]
        )
        .order_by(TimeOff.starts_at)
    )
    closures = await db.scalars(
        select(Closure).where(Closure.date >= from_, Closure.date <= to).order_by(Closure.date)
    )

    return ScheduleOut(
        timezone=str(zone),
        granularity_minutes=granularity,
        staff=[
            ColumnOut(
                id=str(s.id),
                display_name=s.display_name,
                colour=s.colour,
                hex=BY_KEY[s.colour].hex,
                dark_hex=BY_KEY[s.colour].dark_hex,
                max_concurrent_appointments=s.max_concurrent_appointments,
            )
            for s in roster
        ],
        working_blocks=blocks,
        time_off=[
            TimeOffOut(
                id=str(t.id),
                staff_id=str(t.staff_id),
                all_day=t.all_day,
                reason=t.reason,
                starts_at=utc(t.starts_at),
                ends_at=utc(t.ends_at),
            )
            for t in absences
        ],
        closures=[ClosureOut(id=str(c.id), date=c.date.isoformat(), name=c.name) for c in closures],
        appointments=await appointments_between(
            db, from_, to, zone, staff_id, include_cancelled=include_cancelled
        ),
    )
