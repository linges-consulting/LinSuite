"""`GET /api/availability`: the bookable slots for a service, over HTTP.

The loader for `scheduling/availability.py`. Everything the engine needs is read here —
the business's zone, grid and horizon; the service in its catalog shape; the eligible staff
members' weekly blocks and time off; the active resources; the closures — and handed over as
plain values. Nothing in this module decides what is bookable.

**Dates are business-local, `to` is inclusive**, and the range is capped at 31 days: a
month view is the widest thing any screen asks for, and the cap is what keeps one request
from computing a year. The horizon is **today plus `booking_horizon_days`, inclusive** — a
horizon of 1 computes today and tomorrow. Dates past it are still answered, with no slots;
`horizon_ends_on` in the response is how a screen knows why.

**A service the catalog says is unbookable is a 409 carrying its reasons**, not an empty
day. Nobody eligible, a named device deactivated, no room of the required kind: each is
something an administrator has to fix, and an empty calendar would send them looking at
the wrong screen.

`schedule.view`, in either mode: seeing when somebody could be booked is the calendar's own
question, and the same capability that lets somebody look at it.

**`compute` is the loader, and the route is one caller of it.** `scheduling/appointments.py`
is the other: booking re-runs the same computation for the one local day it is about, so
"is this start offered?" has exactly one answer wherever it is asked. `busy_intervals` is
where appointments enter the engine — each one's span inflated by the buffers snapshotted
on it, under its staff member and under every resource it claimed.
"""

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from datetime import date as Date
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import func, select

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
    Slot,
    StaffSpec,
    bookable_slots,
)
from scheduling.clock import localize
from scheduling.models import (
    Appointment,
    AppointmentResource,
    Closure,
    Resource,
    Staff,
    TimeOff,
    WorkingHours,
)
from scheduling.services import CatalogServiceOut, catalog_entry

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
    # The last local date anything is computed for: today (business-local) plus
    # `booking_horizon_days`, *inclusive* — so a horizon of N days is N + 1 computable days,
    # today among them. Days after it come back empty.
    horizon_ends_on: str
    days: list[DayOut]


async def busy_intervals(
    db: SessionDep, staff_ids: Iterable[Id], resource_ids: Iterable[Id], window: Interval
) -> tuple[dict[Id, list[Interval]], dict[Id, list[Interval]]]:
    """The intervals already taken inside `window`, per staff member and per resource.

    A staff member is busy for each non-cancelled appointment's span **inflated by the buffers
    snapshotted on that appointment** — the turnaround after one booking is time nobody else
    can have either, and it is the appointment's own turnaround, not the service's current
    one. A resource is busy for each `appointment_resources.period`, which already is that
    buffered span (cancellation deletes those rows, so there is no status to filter). Both
    are the same arithmetic the trigger and the constraint compare; the engine honours what
    comes back (`tests/test_availability.py`, "buffers", "spaces and equipment",
    "concurrency").
    """
    staff_ids, resource_ids = list(staff_ids), list(resource_ids)
    staff_busy: dict[Id, list[Interval]] = {}
    resource_busy: dict[Id, list[Interval]] = {}
    if staff_ids:
        starts = Appointment.starts_at - _minutes(Appointment.buffer_before_minutes)
        ends = Appointment.ends_at + _minutes(Appointment.buffer_after_minutes)
        for row in await db.execute(
            select(Appointment.staff_id, starts, ends).where(
                Appointment.staff_id.in_(staff_ids),
                Appointment.status != "cancelled",
                starts < window[1],
                ends > window[0],
            )
        ):
            staff_busy.setdefault(row[0], []).append((row[1], row[2]))
    if resource_ids:
        period = AppointmentResource.period
        for row in await db.execute(
            select(AppointmentResource.resource_id, func.lower(period), func.upper(period)).where(
                AppointmentResource.resource_id.in_(resource_ids),
                period.op("&&")(func.tstzrange(window[0], window[1], "[)")),
            )
        ):
            resource_busy.setdefault(row[0], []).append((row[1], row[2]))
    return staff_busy, resource_busy


def _minutes(column):
    """`make_interval(mins => column)` — its positional signature is years, months, weeks,
    days, hours, mins."""
    return func.make_interval(0, 0, 0, 0, 0, column)


@dataclass(frozen=True)
class ResourceRow:
    """An active resource, with the order the booking screen picks "any" ones in."""

    id: uuid.UUID
    kind: str
    sort_order: int
    name: str


@dataclass(frozen=True)
class Computed:
    """Everything `compute` read and what the engine made of it. Booking wants the day's
    slots *and* the resources with what is booked on them, to hand out a free room."""

    timezone: str
    granularity_minutes: int
    horizon_ends_on: Date
    days: dict[Date, list[Slot]]
    resources: list[ResourceRow]
    resource_busy: dict[Id, list[Interval]]


async def compute(
    db: SessionDep,
    service: CatalogServiceOut,
    staff_ids: list[uuid.UUID],
    from_: Date,
    to: Date,
) -> Computed:
    """The engine's inputs, read for `staff_ids` over the local dates `from_`..`to`, and its
    answer. `service` is already the catalog's bookable view; `staff_ids` is the eligible set
    or one member of it — the caller has checked which."""
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
        ResourceRow(id=row.id, kind=row.kind, sort_order=row.sort_order, name=row.name)
        for row in await db.execute(
            select(Resource.id, Resource.kind, Resource.sort_order, Resource.name)
            .where(Resource.active)
            .order_by(Resource.sort_order, Resource.name)
        )
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
        resources=[ResourceSpec(id=r.id, kind=r.kind) for r in resources],
        closures=closures,
        staff_busy=staff_busy,
        resource_busy=resource_busy,
        dates=dates,
        now=now,
        horizon_ends_on=horizon_ends_on,
    )
    return Computed(
        timezone=business.timezone,
        granularity_minutes=business.slot_granularity_minutes,
        horizon_ends_on=horizon_ends_on,
        days=days,
        resources=resources,
        resource_busy=resource_busy,
    )


def check_range(from_: Date, to: Date) -> None:
    """The two range rules every date-ranged read shares: `to` not before `from`, and at
    most a month. Shared with the appointment list so the calendar meets one rule."""
    if to < from_:
        raise refuse("to", "The last day cannot come before the first.", where="query")
    if (to - from_).days >= MAX_RANGE_DAYS:
        raise refuse("to", f"Ask for at most {MAX_RANGE_DAYS} days at a time.", where="query")


@router.get("", response_model=AvailabilityOut)
async def availability(
    _: Viewer,
    db: SessionDep,
    service_id: uuid.UUID,
    from_: Annotated[Date, Query(alias="from")],
    to: Date,
    staff_id: uuid.UUID | None = None,
):
    """Bookable slots for `service_id` on each business-local date from `from` to `to`
    inclusive (at most 31 days). Days after `horizon_ends_on` — today plus the business's
    `booking_horizon_days`, inclusive — are answered with no slots.

    The documented body is `AvailabilityOut`; the 409 below is a `JSONResponse` because
    `HTTPException` carries one `detail` and this refusal has a list beside it."""
    check_range(from_, to)
    service = await catalog_entry(db, service_id)
    if not service.bookable:
        return unbookable(service)
    staff_ids = [uuid.UUID(s) for s in service.staff_ids]
    if staff_id is not None:
        if staff_id not in staff_ids:
            raise refuse(
                "staff_id", "That staff member cannot deliver this service.", where="query"
            )
        staff_ids = [staff_id]

    computed = await compute(db, service, staff_ids, from_, to)

    return AvailabilityOut(
        service_id=service.id,
        timezone=computed.timezone,
        granularity_minutes=computed.granularity_minutes,
        horizon_ends_on=computed.horizon_ends_on.isoformat(),
        days=[
            DayOut(
                date=day.isoformat(),
                slots=[
                    SlotOut(
                        starts_at=utc(slot.starts_at),
                        ends_at=utc(slot.ends_at),
                        staff_ids=[str(s) for s in slot.staff_ids],
                    )
                    for slot in slots
                ],
            )
            for day, slots in computed.days.items()
        ],
    )


def unbookable(service: CatalogServiceOut) -> JSONResponse:
    """A service the catalog says cannot be booked: 409, with the reasons beside it."""
    return JSONResponse(
        status_code=409,
        content={
            "detail": "This service cannot be booked until its setup is complete.",
            "code": "not_bookable",
            "unbookable_reasons": service.unbookable_reasons,
        },
    )


def utc(moment: datetime) -> str:
    # `Z` rather than `+00:00`, the same shape `scheduling/time_off.py` sends instants in.
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")
