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
from scheduling import cache
from scheduling._admin_forms import refuse
from scheduling.availability import (
    ChainLink,
    Id,
    Interval,
    Occupant,
    Requirement,
    ResourceSpec,
    ServiceSpec,
    Slot,
    StaffSpec,
    bookable_slots,
    chain_starts,
    waive_handover,
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
    db: SessionDep,
    staff_ids: Iterable[Id],
    resource_ids: Iterable[Id],
    window: Interval,
    *,
    excluding: uuid.UUID | None = None,
    handover_group_id: uuid.UUID | None = None,
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

    `excluding` leaves one appointment out — its staff span and its resource claims — which
    is how a move asks "where could this go?" without the appointment standing in its own way.

    `handover_group_id` (fix round 2) is the *waiver*: busy time contributed by another
    appointment sharing that group id, and currently touching one being checked for it, is
    trimmed at the shared edge — same client, no turnover — via `availability.waive_handover`,
    derived fresh from each row's own (never-mutated) snapshot every call, for staff and,
    while a chain is mid-construction (`appointments.book_group`) or a member is mid-move
    (`change_appointment`), for resources too — their stored `period` only catches up once a
    write actually lands (`appointments.recompute_group_periods`), so a read taken before that
    must derive the same answer itself rather than trust the column.

    `excluding` only ever drops one row from the *busy* result, never from the query this
    waiver reads: an excluded appointment is still a real group member another row may need
    to see in order to waive its own facing edge against it (this is what makes a no-op move
    of one link still see the other link's buffer correctly waived).
    """
    staff_ids, resource_ids = list(staff_ids), list(resource_ids)
    staff_busy: dict[Id, list[Interval]] = {}
    resource_busy: dict[Id, list[Interval]] = {}
    if staff_ids and handover_group_id is None:
        starts = Appointment.starts_at - _minutes(Appointment.buffer_before_minutes)
        ends = Appointment.ends_at + _minutes(Appointment.buffer_after_minutes)
        for row in await db.execute(
            select(Appointment.staff_id, starts, ends).where(
                Appointment.staff_id.in_(staff_ids),
                Appointment.status != "cancelled",
                starts < window[1],
                ends > window[0],
                *([Appointment.id != excluding] if excluding else []),
            )
        ):
            staff_busy.setdefault(row[0], []).append((row[1], row[2]))
    elif staff_ids:
        # The handover-aware path: raw spans and snapshot buffers, fetched wide enough that a
        # sibling's own buffer (bounded, but not by anything this function assumes a size for)
        # cannot fall outside it, then buffered — and waived — in Python.
        #
        # `excluding` is applied only to what gets added to `staff_busy`, never to the query
        # itself: the excluded appointment is still a group member another row may need to see
        # in order to waive its own facing edge against it (a no-op move checks the excluded
        # appointment's own old span against a sibling that is *not* excluded — that sibling's
        # busy contribution must already be waived, and it cannot be without the excluded row
        # present to compare against).
        margin = timedelta(days=1)
        rows = list(
            await db.execute(
                select(
                    Appointment.id,
                    Appointment.staff_id,
                    Appointment.booking_group_id,
                    Appointment.status,
                    Appointment.starts_at,
                    Appointment.ends_at,
                    Appointment.buffer_before_minutes,
                    Appointment.buffer_after_minutes,
                ).where(
                    Appointment.staff_id.in_(staff_ids),
                    Appointment.status != "cancelled",
                    Appointment.starts_at < window[1] + margin,
                    Appointment.ends_at > window[0] - margin,
                )
            )
        )
        occupants = [
            Occupant(
                id=r.id,
                booking_group_id=r.booking_group_id,
                status=r.status,
                starts_at=r.starts_at,
                ends_at=r.ends_at,
            )
            for r in rows
        ]
        for row, occupant in zip(rows, occupants, strict=True):
            if excluding is not None and row.id == excluding:
                continue
            before, after = waive_handover(
                occupant, occupants, row.buffer_before_minutes, row.buffer_after_minutes
            )
            start = row.starts_at - timedelta(minutes=before)
            end = row.ends_at + timedelta(minutes=after)
            if start < window[1] and end > window[0]:
                staff_busy.setdefault(row.staff_id, []).append((start, end))
    if resource_ids and handover_group_id is None:
        period = AppointmentResource.period
        for row in await db.execute(
            select(AppointmentResource.resource_id, func.lower(period), func.upper(period)).where(
                AppointmentResource.resource_id.in_(resource_ids),
                period.op("&&")(func.tstzrange(window[0], window[1], "[)")),
                *([AppointmentResource.appointment_id != excluding] if excluding else []),
            )
        ):
            resource_busy.setdefault(row[0], []).append((row[1], row[2]))
    elif resource_ids:
        # The same handover-aware path as staff, and for the same reason: a member being
        # PATCHed is excluded from `resource_busy` itself, but stays in the query so a sibling
        # still sharing this resource can see it and waive its own facing edge — this reads
        # the raw span and snapshot buffers straight off `appointments`, never the (possibly
        # not-yet-caught-up) stored `period`.
        margin = timedelta(days=1)
        rows = list(
            await db.execute(
                select(
                    AppointmentResource.resource_id,
                    Appointment.id,
                    Appointment.booking_group_id,
                    Appointment.status,
                    Appointment.starts_at,
                    Appointment.ends_at,
                    Appointment.buffer_before_minutes,
                    Appointment.buffer_after_minutes,
                )
                .join(Appointment, Appointment.id == AppointmentResource.appointment_id)
                .where(
                    AppointmentResource.resource_id.in_(resource_ids),
                    Appointment.status != "cancelled",
                    Appointment.starts_at < window[1] + margin,
                    Appointment.ends_at > window[0] - margin,
                )
            )
        )
        occupants = [
            Occupant(
                id=r.id,
                booking_group_id=r.booking_group_id,
                status=r.status,
                starts_at=r.starts_at,
                ends_at=r.ends_at,
            )
            for r in rows
        ]
        for row, occupant in zip(rows, occupants, strict=True):
            if excluding is not None and row.id == excluding:
                continue
            before, after = waive_handover(
                occupant, occupants, row.buffer_before_minutes, row.buffer_after_minutes
            )
            start = row.starts_at - timedelta(minutes=before)
            end = row.ends_at + timedelta(minutes=after)
            if start < window[1] and end > window[0]:
                resource_busy.setdefault(row.resource_id, []).append((start, end))
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
    # The advisory inputs, kept so booking can name which rules a start breaks
    # (`availability.advisory_breaches`) without reading them twice.
    staff: dict[Id, StaffSpec]
    closures: set[Date]


async def compute(
    db: SessionDep,
    service: CatalogServiceOut,
    staff_ids: list[uuid.UUID],
    from_: Date,
    to: Date,
    *,
    excluding: uuid.UUID | None = None,
    handover_group_id: uuid.UUID | None = None,
    relax_advisory: bool = False,
) -> Computed:
    """The engine's inputs, read for `staff_ids` over the local dates `from_`..`to`, and its
    answer. `service` is already the catalog's bookable view; `staff_ids` is the eligible set
    or one member of it — the caller has checked which. `excluding` is the appointment being
    moved, left out of what is busy; `handover_group_id` is the same visit's own buffer
    waiver, both threaded straight through to `busy_intervals`.

    `relax_advisory` is the engine's switch of the same name (tech-stack §22): the shift,
    time off, closures and the horizon set aside, the physical rules untouched. **Only
    `scheduling/appointments.py` passes it**, to diagnose an override and to make one. The
    availability route never does, and the client-facing portal (M3) must never be given
    it — a relaxed answer is a list of starts nobody has agreed to work."""
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
        db,
        staff_ids,
        [r.id for r in resources],
        window,
        excluding=excluding,
        handover_group_id=handover_group_id,
    )

    now = datetime.now(UTC)
    horizon_ends_on = now.astimezone(zone).date() + timedelta(days=business.booking_horizon_days)
    specs = {
        s: StaffSpec(id=s, hours=hours[s], time_off=time_off[s], max_concurrent=limits.get(s, 1))
        for s in staff_ids
    }
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
        staff=specs.values(),
        resources=[ResourceSpec(id=r.id, kind=r.kind) for r in resources],
        closures=closures,
        staff_busy=staff_busy,
        resource_busy=resource_busy,
        dates=dates,
        now=now,
        horizon_ends_on=horizon_ends_on,
        relax_advisory=relax_advisory,
    )
    return Computed(
        timezone=business.timezone,
        granularity_minutes=business.slot_granularity_minutes,
        horizon_ends_on=horizon_ends_on,
        days=days,
        resources=resources,
        resource_busy=resource_busy,
        staff=specs,
        closures=closures,
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

    # Only this — the engine's own answer — is cached (tech-stack §19). The catalog and
    # eligibility checks above always run fresh; they are cheap single-row reads, and a
    # cache hit here already reflects the latest generation, which any change to either one
    # bumps (`scheduling/cache.py`).
    async def _compute() -> dict:
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
        ).model_dump()

    parts = {
        "service_id": str(service.id),
        "staff_id": str(staff_id) if staff_id is not None else None,
        "from": from_.isoformat(),
        "to": to.isoformat(),
    }
    return AvailabilityOut.model_validate(await cache.cached("avail", parts, _compute))


def unbookable(service: CatalogServiceOut, link_index: int | None = None) -> JSONResponse:
    """A service the catalog says cannot be booked: 409, with the reasons beside it.
    `link_index` is set when this is one link of a group booking's refusal."""
    content = {
        "detail": "This service cannot be booked until its setup is complete.",
        "code": "not_bookable",
        "unbookable_reasons": service.unbookable_reasons,
    }
    if link_index is not None:
        content["link_index"] = link_index
    return JSONResponse(status_code=409, content=content)


def utc(moment: datetime) -> str:
    # `Z` rather than `+00:00`, the same shape `scheduling/time_off.py` sends instants in.
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


# --- group availability (Task 18) -----------------------------------------------------------


class GroupSlotOut(BaseModel):
    starts_at: str
    # The resolved staff, one per link, in the order `services` was asked in — a named link
    # is always itself; an "any" link is whoever the search picked for that particular start.
    staff_ids: list[str]


class GroupDayOut(BaseModel):
    date: str
    slots: list[GroupSlotOut]


class _Unbookable(Exception):
    """Signals a 409 out of `group_availability`'s cached computation without caching it."""

    def __init__(self, response: JSONResponse):
        self.response = response


class GroupAvailabilityOut(BaseModel):
    service_ids: list[str]
    timezone: str
    granularity_minutes: int
    horizon_ends_on: str
    days: list[GroupDayOut]


@router.get("/group", response_model=GroupAvailabilityOut)
async def group_availability(
    _: Viewer,
    db: SessionDep,
    services: str,
    from_: Annotated[Date, Query(alias="from")],
    to: Date,
    staff: str | None = None,
):
    """Chain-valid starts for an ordered visit (module docstring's Task 18): every start of
    the first service from which every following one is offered, back to back, ending where
    the one before it ends — the sequential search tech-stack §19 calls for. Same range rules
    and horizon as `/api/availability`; each service is checked bookable exactly as one is.

    `services` is comma-separated, in visit order. `staff` is the same length when sent —
    one staff id, or `any`, per service — and defaults to `any` for every link.
    """
    check_range(from_, to)
    service_ids = [s for s in services.split(",") if s]
    if not service_ids:
        raise refuse("services", "Name at least one service.", where="query")
    tokens = staff.split(",") if staff else [None] * len(service_ids)
    if len(tokens) != len(service_ids):
        raise refuse("staff", 'Name a provider, or "any", for every service.', where="query")

    async def _compute() -> dict:
        links: list[ChainLink] = []
        computed_first: Computed | None = None
        for i, (service_id, token) in enumerate(zip(service_ids, tokens, strict=True)):
            service = await catalog_entry(db, uuid.UUID(service_id))
            if not service.bookable:
                # Never cached — see the `except` below. `cache.cached` only ever writes back
                # the value `compute()` returns, so an exception here leaves nothing behind.
                raise _Unbookable(unbookable(service, link_index=i))
            eligible = [uuid.UUID(s) for s in service.staff_ids]
            named = uuid.UUID(token) if token and token != "any" else None
            if named is not None and named not in eligible:
                raise refuse(
                    "staff", "That staff member cannot deliver this service.", where="query"
                )
            candidates = [named] if named else eligible
            computed = await compute(db, service, candidates, from_, to)
            computed_first = computed_first or computed
            order = (
                list(
                    await db.scalars(
                        select(Staff.id)
                        .where(Staff.id.in_(candidates))
                        .order_by(Staff.sort_order, Staff.display_name, Staff.id)
                    )
                )
                if named is None
                else []
            )
            slots = [slot for day_slots in computed.days.values() for slot in day_slots]
            links.append(ChainLink(slots=slots, staff_id=named, staff_order=order))

        zone = ZoneInfo(computed_first.timezone)
        by_date: dict[Date, list] = {
            from_ + timedelta(days=n): [] for n in range((to - from_).days + 1)
        }
        for chain in chain_starts(links):
            by_date.setdefault(chain.starts_at.astimezone(zone).date(), []).append(chain)

        return GroupAvailabilityOut(
            service_ids=service_ids,
            timezone=computed_first.timezone,
            granularity_minutes=computed_first.granularity_minutes,
            horizon_ends_on=computed_first.horizon_ends_on.isoformat(),
            days=[
                GroupDayOut(
                    date=day.isoformat(),
                    slots=[
                        GroupSlotOut(
                            starts_at=utc(chain.starts_at),
                            staff_ids=[str(s) for s in chain.staff_ids],
                        )
                        for chain in sorted(chains, key=lambda c: c.starts_at)
                    ],
                )
                for day, chains in sorted(by_date.items())
            ],
        ).model_dump()

    # `service_ids` keeps its order (visit order is the chain's own semantics, tech-stack
    # §19 step 4); `tokens` is positional against it, so the pair together is the question.
    parts = {
        "service_ids": service_ids,
        "staff": tokens,
        "from": from_.isoformat(),
        "to": to.isoformat(),
    }
    try:
        data = await cache.cached("avail:group", parts, _compute)
    except _Unbookable as signal:
        return signal.response
    return GroupAvailabilityOut.model_validate(data)
