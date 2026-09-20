"""`GET /api/availability`: the bookable slots for a service, over HTTP.

The loader for `scheduling/availability.py`. Everything the engine needs is read here —
the business's zone, grid and horizon; the service in its catalog shape; the eligible staff
members' weekly blocks and time off; the active resources; the closures — and handed over as
plain values. Nothing in this module decides what is bookable.

**Dates are business-local, `to` is inclusive**, and the range is capped at 31 days: a
month view is the widest thing any screen asks for, and the cap is what keeps one request
from computing a year. Dates past the booking horizon are still answered, with no slots —
`horizon_ends_on` in the response is how a screen knows why.

**A service the catalog says is unbookable is a 409 carrying its reasons**, not an empty
day. Nobody eligible, a named device deactivated, no room of the required kind: each is
something an administrator has to fix, and an empty calendar would send them looking at
the wrong screen.

`schedule.view`, in either mode: seeing when somebody could be booked is the calendar's own
question, and the same capability that lets somebody look at it.

**Appointments do not exist yet.** `busy_intervals` is the seam Task 15 wires them into;
until then it returns nothing busy, and `tests/test_availability.py` is what proves the
engine honours busy intervals when it is given them.
"""

import uuid
from collections.abc import Iterable
from datetime import UTC, datetime, time, timedelta
from datetime import date as Date
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select

from auth.capabilities import Requires
from auth.models import User
from core.db import SessionDep
from core.models import Business
from scheduling._admin_forms import refuse
from scheduling.availability import (
    Id,
    Interval,
    Requirement,
    ResourceSpec,
    ServiceSpec,
    StaffSpec,
    bookable_slots,
)
from scheduling.clock import localize
from scheduling.models import Closure, Resource, Staff, TimeOff, WorkingHours
from scheduling.services import catalog_entry

router = APIRouter(prefix="/availability", tags=["availability"])

Viewer = Annotated[User, Depends(Requires("schedule.view"))]

# Inclusive: `from` and `to` thirty days apart is thirty-one dates, a month.
MAX_RANGE_DAYS = 31


class SlotOut(BaseModel):
    starts_at: str
    ends_at: str
    staff_ids: list[str]


class DayOut(BaseModel):
    date: str
    slots: list[SlotOut]


class AvailabilityOut(BaseModel):
    service_id: str
    timezone: str
    granularity_minutes: int
    # The last local date anything is computed for. Days after it come back empty.
    horizon_ends_on: str
    days: list[DayOut]


async def busy_intervals(
    db: SessionDep, staff_ids: Iterable[Id], resource_ids: Iterable[Id], window: Interval
) -> tuple[dict[Id, list[Interval]], dict[Id, list[Interval]]]:
    """The intervals already taken inside `window`, per staff member and per resource.

    **Task 15's seam.** Appointments do not exist yet, so nothing is busy. When they do, this
    reads each appointment's span — inflated by the buffers snapshotted on it, because the
    turnaround after one booking is time nobody else can have either — and returns it under
    the staff member and under every resource it claimed. Nothing else changes: the engine
    already honours what this returns (`tests/test_availability.py`, "buffers", "spaces and
    equipment", "concurrency").
    """
    del db, staff_ids, resource_ids, window
    return {}, {}


@router.get("", response_model=AvailabilityOut)
async def availability(
    _: Viewer,
    db: SessionDep,
    service_id: uuid.UUID,
    from_: Annotated[Date, Query(alias="from")],
    to: Date,
    staff_id: uuid.UUID | None = None,
):
    """The documented body is `AvailabilityOut`; the 409 below is a `JSONResponse` because
    `HTTPException` carries one `detail` and this refusal has a list beside it."""
    if to < from_:
        raise refuse("to", "The last day cannot come before the first.", where="query")
    if (to - from_).days >= MAX_RANGE_DAYS:
        raise refuse("to", f"Ask for at most {MAX_RANGE_DAYS} days at a time.", where="query")

    business = (
        await db.execute(
            select(
                Business.timezone,
                Business.slot_granularity_minutes,
                Business.booking_horizon_days,
            ).where(Business.id == 1)
        )
    ).one()
    zone = ZoneInfo(business.timezone)

    service = await catalog_entry(db, service_id)
    if not service.bookable:
        return JSONResponse(
            status_code=409,
            content={
                "detail": "This service cannot be booked until its setup is complete.",
                "unbookable_reasons": service.unbookable_reasons,
            },
        )
    staff_ids = [uuid.UUID(s) for s in service.staff_ids]
    if staff_id is not None:
        if staff_id not in staff_ids:
            raise refuse(
                "staff_id", "That staff member cannot deliver this service.", where="query"
            )
        staff_ids = [staff_id]

    dates = [from_ + timedelta(days=n) for n in range((to - from_).days + 1)]
    # Every instant on these local days, for the time-off and busy queries.
    window = (
        localize(datetime.combine(from_, time.min), zone),
        localize(datetime.combine(to + timedelta(days=1), time.min), zone),
    )

    limits = dict(
        (
            await db.execute(
                select(Staff.id, Staff.max_concurrent_appointments).where(Staff.id.in_(staff_ids))
            )
        ).all()  # type: ignore[arg-type]
    )
    hours: dict[uuid.UUID, dict[int, list[tuple[int, int]]]] = {s: {} for s in staff_ids}
    for row in await db.execute(
        select(
            WorkingHours.staff_id,
            WorkingHours.weekday,
            WorkingHours.start_minute,
            WorkingHours.end_minute,
        ).where(WorkingHours.staff_id.in_(staff_ids))
    ):
        hours[row.staff_id].setdefault(row.weekday, []).append((row.start_minute, row.end_minute))
    time_off: dict[uuid.UUID, list[Interval]] = {s: [] for s in staff_ids}
    for row in await db.execute(
        select(TimeOff.staff_id, TimeOff.starts_at, TimeOff.ends_at).where(
            TimeOff.staff_id.in_(staff_ids),
            TimeOff.ends_at > window[0],
            TimeOff.starts_at < window[1],
        )
    ):
        time_off[row.staff_id].append((row.starts_at, row.ends_at))

    resources = [
        ResourceSpec(id=row.id, kind=row.kind)
        for row in await db.execute(select(Resource.id, Resource.kind).where(Resource.active))
    ]
    closures = set(
        await db.scalars(select(Closure.date).where(Closure.date >= from_, Closure.date <= to))
    )
    staff_busy, resource_busy = await busy_intervals(
        db, staff_ids, [r.id for r in resources], window
    )

    now = datetime.now(UTC)
    horizon_ends_on = now.astimezone(zone).date() + timedelta(days=business.booking_horizon_days)
    days = bookable_slots(
        timezone=business.timezone,
        granularity_minutes=business.slot_granularity_minutes,
        service=ServiceSpec(
            duration_minutes=service.duration_minutes,
            buffer_before_minutes=service.buffer_before_minutes,
            buffer_after_minutes=service.buffer_after_minutes,
            staff_ids=staff_ids,
            requirements=[
                Requirement(
                    kind=r.kind, resource_id=uuid.UUID(r.resource_id) if r.resource_id else None
                )
                for r in service.requirements
            ],
        ),
        staff=[
            StaffSpec(id=s, hours=hours[s], time_off=time_off[s], max_concurrent=limits.get(s, 1))
            for s in staff_ids
        ],
        resources=resources,
        closures=closures,
        staff_busy=staff_busy,
        resource_busy=resource_busy,
        dates=dates,
        now=now,
        horizon_ends_on=horizon_ends_on,
    )

    return AvailabilityOut(
        service_id=service.id,
        timezone=business.timezone,
        granularity_minutes=business.slot_granularity_minutes,
        horizon_ends_on=horizon_ends_on.isoformat(),
        days=[
            DayOut(
                date=day.isoformat(),
                slots=[
                    SlotOut(
                        starts_at=_utc(slot.starts_at),
                        ends_at=_utc(slot.ends_at),
                        staff_ids=[str(s) for s in slot.staff_ids],
                    )
                    for slot in slots
                ],
            )
            for day, slots in days.items()
        ],
    )


def _utc(moment: datetime) -> str:
    # `Z` rather than `+00:00`, the same shape `scheduling/time_off.py` sends instants in.
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")
