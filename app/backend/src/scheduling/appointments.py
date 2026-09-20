"""`/api/appointments`: book one, list them. The tracer bullet (PRD §3).

**The engine decides what is offered; the database decides what is booked.** A booking
re-runs `scheduling/slots.compute` for the one local day it is about and refuses a start the
engine would not have offered — so the calendar and this endpoint never disagree about what
"available" means. Then it inserts, and the two rules in migration 0014 have the last word:
the exclusion constraint on `appointment_resources` and the staff-concurrency trigger on
`appointments`. Either refusing is **409 `slot_taken`** — the engine's picture was a moment
stale (tech-stack §19, "staleness is safe by design"), never a 500 and never a second
booking. The handler holds no lock of its own; the database serialises the race.

**One transaction.** The inline customer, the appointment, its resource rows and the two
audit events land together or not at all: a refused booking creates nobody.

**The service is snapshotted, never referenced** (`Service`'s SNAPSHOT CONTRACT): duration,
buffers and price are copied onto the row here and read from the row ever after.

**"Any available" assigns a person and rooms here.** The engine names every staff member who
could take a slot; the lowest `sort_order` (then display name) among them gets it. A named
requirement takes its resource; an "any" one takes the lowest-`sort_order` active resource of
its kind that is free for the whole buffered span, and two requirements never receive the same
one — the constraint would refuse the double claim, so this is what makes a service that needs
two rooms bookable rather than reliably refused.

`schedule.manage` to book, `schedule.view` to list: booking is staff work in either mode.
Creating the client inline is `customers.manage` on top — the same rule as `POST
/api/customers`, checked here because the request is one request.
The list is the read Task 16's calendar draws from; `AppointmentOut` is its shape and
`tests/test_appointments.py` pins the keys.
"""

import uuid
from datetime import date as Date
from datetime import datetime, time, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import AwareDatetime, BaseModel, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import Range
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from auth.capabilities import BY_KEY, Requires
from auth.models import User
from core.audit import record_event
from core.db import SessionDep
from core.errors import CAPABILITY_REQUIRED, Forbidden
from customers.models import Customer
from customers.routes import CustomerIn, create_customer
from scheduling._admin_forms import blank_to_none, refuse
from scheduling.availability import Interval, Slot
from scheduling.clock import localize
from scheduling.models import Appointment, AppointmentResource, Staff
from scheduling.services import CatalogServiceOut, RequirementOut, catalog_entry
from scheduling.slots import Computed, ResourceRow, check_range, compute, unbookable, utc
from scheduling.time_off import business_zone

router = APIRouter(prefix="/appointments", tags=["appointments"])

Scheduler = Annotated[User, Depends(Requires("schedule.manage"))]
Viewer = Annotated[User, Depends(Requires("schedule.view"))]

# The two names migration 0014 gives the database's refusals. Either is "just taken".
SLOT_TAKEN_CONSTRAINTS = (
    "ex_appointment_resources_no_overlap",
    "tg_appointments_staff_concurrency",
)


# --- what goes over the wire ------------------------------------------------------------


class CustomerRef(BaseModel):
    id: str
    first_name: str
    last_name: str
    email: str | None
    phone: str | None


class ServiceRef(BaseModel):
    id: str
    name: str


class StaffRef(BaseModel):
    id: str
    display_name: str
    # A palette key (`scheduling/palette.py`); `GET /api/staff` carries the hex for it.
    colour: str


class ResourceRef(BaseModel):
    id: str
    name: str
    kind: str


class AppointmentOut(BaseModel):
    """What the calendar draws. Numbers are the snapshot on the row; instants are UTC `Z`."""

    id: str
    status: str
    starts_at: str
    ends_at: str
    duration_minutes: int
    buffer_before_minutes: int
    buffer_after_minutes: int
    price_cents: int
    notes: str | None
    booking_group_id: str | None
    customer: CustomerRef
    service: ServiceRef
    staff: StaffRef
    resources: list[ResourceRef]


class AppointmentsOut(BaseModel):
    """`timezone` is the business's, so a screen can print the day the appointments are on."""

    timezone: str
    appointments: list[AppointmentOut]


def _out(appointment: Appointment) -> AppointmentOut:
    return AppointmentOut(
        id=str(appointment.id),
        status=appointment.status,
        starts_at=utc(appointment.starts_at),
        ends_at=utc(appointment.ends_at),
        duration_minutes=appointment.duration_minutes,
        buffer_before_minutes=appointment.buffer_before_minutes,
        buffer_after_minutes=appointment.buffer_after_minutes,
        price_cents=appointment.price_cents,
        notes=appointment.notes,
        booking_group_id=str(appointment.booking_group_id)
        if appointment.booking_group_id
        else None,
        customer=CustomerRef(
            id=str(appointment.customer.id),
            first_name=appointment.customer.first_name,
            last_name=appointment.customer.last_name,
            email=appointment.customer.email,
            phone=appointment.customer.phone,
        ),
        service=ServiceRef(id=str(appointment.service.id), name=appointment.service.name),
        staff=StaffRef(
            id=str(appointment.staff.id),
            display_name=appointment.staff.display_name,
            colour=appointment.staff.colour,
        ),
        # Spaces before equipment, then the administrator's order: a stable list, decided
        # here rather than by whatever order the rows came back in.
        resources=[
            ResourceRef(id=str(r.resource.id), name=r.resource.name, kind=r.kind)
            for r in sorted(
                appointment.resources,
                key=lambda r: (r.kind != "space", r.resource.sort_order, r.resource.name),
            )
        ],
    )


# --- what comes in ------------------------------------------------------------------------


class BookingIn(BaseModel):
    """`starts_at` is the instant `/api/availability` offered — aware, so a naive local time
    is refused at the boundary rather than guessed at. Exactly one of `customer_id` and an
    inline `customer`."""

    service_id: uuid.UUID
    # None is "any available": the engine's slot names who could take it, and the first by
    # `sort_order` does.
    staff_id: uuid.UUID | None = None
    starts_at: AwareDatetime
    customer_id: uuid.UUID | None = None
    customer: CustomerIn | None = None
    notes: Annotated[str | None, Field(max_length=2000)] = None

    @field_validator("notes", mode="after")
    @classmethod
    def _trimmed(cls, value: str | None) -> str | None:
        return blank_to_none(value)

    @model_validator(mode="after")
    def _one_customer(self):
        if (self.customer_id is None) == (self.customer is None):
            raise ValueError("send customer_id or customer, and not both")
        return self


# --- booking ------------------------------------------------------------------------------


async def offered_slot(
    db: AsyncSession,
    service: CatalogServiceOut,
    staff_ids: list[uuid.UUID],
    starts_at,
    *,
    excluding: uuid.UUID | None = None,
) -> tuple[Computed, Slot | None]:
    """The engine's answer for the local day `starts_at` falls on, and the slot at exactly
    that instant if it is offered to one of `staff_ids`. Its own function so a test can hold
    the door open between the check and the insert (`test_two_concurrent_bookings...`).
    `excluding` is the appointment being moved, left out of what is busy."""
    day = starts_at.astimezone(await business_zone(db)).date()
    computed = await compute(db, service, staff_ids, day, day, excluding=excluding)
    slot = next((s for s in computed.days[day] if s.starts_at == starts_at), None)
    return computed, slot


def assign_resources(
    requirements: list[RequirementOut],
    resources: list[ResourceRow],
    resource_busy: dict,
    span: Interval,
) -> list[ResourceRow] | None:
    """One distinct resource per requirement, or None when they cannot all be met.

    Named requirements first, so an "any space" never takes the very room a named one needs.
    `resources` is already in `sort_order` (then name) order, which is the order "any" picks
    in. The engine has usually already said yes; this returns None only where it was more
    generous than reality — two "any" requirements of one kind with a single free resource
    — and the caller answers as it does for any start that cannot be booked.
    """

    def free(resource: ResourceRow) -> bool:
        return not any(
            busy_start < span[1] and busy_end > span[0]
            for busy_start, busy_end in resource_busy.get(resource.id, ())
        )

    chosen: list[ResourceRow] = []
    for requirement in sorted(requirements, key=lambda r: r.resource_id is None):
        pick = next(
            (
                r
                for r in resources
                if r not in chosen
                and free(r)
                and (
                    str(r.id) == requirement.resource_id
                    if requirement.resource_id
                    else r.kind == requirement.kind
                )
            ),
            None,
        )
        if pick is None:
            return None
        chosen.append(pick)
    return chosen


def not_offered() -> JSONResponse:
    """The engine does not offer that start. FastAPI's 422 shape, plus a code the screen can
    switch on without reading `loc`: it means the same as `slot_taken` to a person — what
    you were looking at is out of date — and a screen should treat both alike."""
    return JSONResponse(
        status_code=422,
        content={
            "detail": [
                {
                    "type": "value_error",
                    "loc": ["body", "starts_at"],
                    "msg": "That time is no longer available. Pick another.",
                }
            ],
            "code": "not_offered",
        },
    )


def _is_slot_taken(error: IntegrityError) -> bool:
    """Whether the database refused because the time is taken, rather than for any other
    reason. asyncpg carries `constraint_name`; SQLAlchemy's wrapper may keep it a cause
    down; the message is the last resort (same shape as `services._is_duplicate_name`)."""
    for candidate in (error.orig, getattr(error.orig, "__cause__", None)):
        if getattr(candidate, "constraint_name", None) in SLOT_TAKEN_CONSTRAINTS:
            return True
    return any(name in str(error.orig) for name in SLOT_TAKEN_CONSTRAINTS)


@router.post("", status_code=201, response_model=AppointmentOut)
async def book_appointment(payload: BookingIn, actor: Scheduler, db: SessionDep):
    """The documented body is `AppointmentOut`; the coded refusals are `JSONResponse`s
    because each carries something beside `detail` — the catalog's reasons and
    `not_bookable`, `not_offered`, or `slot_taken`."""
    service = await catalog_entry(db, payload.service_id)
    if not service.bookable:
        return unbookable(service)
    eligible = [uuid.UUID(s) for s in service.staff_ids]
    if payload.staff_id is not None and payload.staff_id not in eligible:
        raise refuse("staff_id", "That staff member cannot deliver this service.")
    candidates = [payload.staff_id] if payload.staff_id else eligible

    computed, slot = await offered_slot(db, service, candidates, payload.starts_at)
    if slot is None:
        return not_offered()

    staff_id = payload.staff_id or await db.scalar(
        select(Staff.id)
        .where(Staff.id.in_(slot.staff_ids))
        .order_by(Staff.sort_order, Staff.display_name, Staff.id)
        .limit(1)
    )

    span = (
        slot.starts_at - timedelta(minutes=service.buffer_before_minutes),
        slot.ends_at + timedelta(minutes=service.buffer_after_minutes),
    )
    claimed = assign_resources(
        service.requirements, computed.resources, computed.resource_busy, span
    )
    if claimed is None:
        return not_offered()

    if payload.customer is not None:
        # The same door `POST /api/customers` has, in the same words `Requires` would use.
        if "customers.manage" not in actor.capabilities:
            raise Forbidden(
                CAPABILITY_REQUIRED,
                f"Your role does not allow this: {BY_KEY['customers.manage'].description}",
            )
        customer = await create_customer(db, payload.customer, actor.id)
    else:
        customer = await db.get(Customer, payload.customer_id)
        if customer is None:
            raise refuse("customer_id", "No such customer.")

    appointment = Appointment(
        customer_id=customer.id,
        staff_id=staff_id,
        service_id=uuid.UUID(service.id),
        starts_at=slot.starts_at,
        ends_at=slot.ends_at,
        # The snapshot. From the catalog view of the service, at this moment, and never
        # read through to the service again.
        duration_minutes=service.duration_minutes,
        buffer_before_minutes=service.buffer_before_minutes,
        buffer_after_minutes=service.buffer_after_minutes,
        price_cents=service.price_cents,
        status="confirmed",
        notes=payload.notes,
        created_by_user_id=actor.id,
        resources=[
            AppointmentResource(
                resource_id=r.id, kind=r.kind, period=Range(span[0], span[1], bounds="[)")
            )
            for r in claimed
        ],
    )
    db.add(appointment)
    try:
        await db.flush()
    except IntegrityError as error:
        await db.rollback()
        if not _is_slot_taken(error):
            raise
        return JSONResponse(
            status_code=409,
            content={"detail": "That time was just taken. Pick another.", "code": "slot_taken"},
        )
    record_event(
        db,
        "appointment.booked",
        target_type="appointment",
        target_id=str(appointment.id),
        actor_user_id=actor.id,
        # Ids only: the notes, the names and the price are on the row.
        metadata={
            "service_id": service.id,
            "staff_id": str(staff_id),
            "customer_id": str(customer.id),
        },
    )
    await db.commit()
    return _out(await _load(db, appointment.id))


async def _load(db: AsyncSession, appointment_id: uuid.UUID) -> Appointment:
    # `populate_existing` for the same reason `services._load` uses it: the sessions are
    # `expire_on_commit=False`, and the relationships on a row this request just inserted
    # were never loaded.
    return await db.scalar(
        select(Appointment)
        .where(Appointment.id == appointment_id)
        .execution_options(populate_existing=True)
    )


# --- moving and resizing --------------------------------------------------------------------


class ChangeIn(BaseModel):
    """A move (`starts_at`), a resize (`duration_minutes`), or both. Nothing else about an
    appointment changes here: the price stays what was agreed, the service stays the service.
    An empty body is a request for nothing and is refused as one."""

    starts_at: AwareDatetime | None = None
    duration_minutes: Annotated[int | None, Field(gt=0, le=24 * 60)] = None

    @model_validator(mode="after")
    def _something(self):
        if self.starts_at is None and self.duration_minutes is None:
            raise ValueError("send a new starts_at, a new duration_minutes, or both")
        return self


@router.patch("/{appointment_id}", response_model=AppointmentOut)
async def change_appointment(
    appointment_id: uuid.UUID, payload: ChangeIn, actor: Scheduler, db: SessionDep
):
    """The calendar's drag: a move by the body, a resize by the bottom edge, or a keyboard
    doing either. The rule is the booking rule — **the engine decides what is offered, the
    database decides what is booked** — with one addition: the engine is run with this
    appointment left out of what is busy, so it never stands in its own way (a fifteen-minute
    nudge overlaps the old span, and that must be fine).

    Rooms are handed out afresh under the booking rules. A named requirement keeps its
    resource; an "any" one keeps the resource it had when that is still free and re-picks
    when it is not — the old rooms go first in the pick order, which is all that takes.
    Buffers and price are the row's own snapshot; a resize changes the duration and the end
    and nothing else. Shift-end and time-off overrides are Task 17's; this refuses them.
    """
    appointment = await _load(db, appointment_id)
    if appointment is None:
        raise HTTPException(status_code=404, detail="No such appointment.")
    if appointment.status != "confirmed":
        raise HTTPException(
            status_code=409, detail=f"A {appointment.status} appointment cannot be moved."
        )
    old_start, old_duration = appointment.starts_at, appointment.duration_minutes
    starts_at = payload.starts_at or old_start
    duration = payload.duration_minutes or old_duration

    # The catalog's view of the service for its requirements and eligibility; the
    # appointment's own snapshot for the numbers the engine slides across the day.
    catalog = await catalog_entry(db, appointment.service_id, include_inactive=True)
    service = catalog.model_copy(
        update={
            "duration_minutes": duration,
            "buffer_before_minutes": appointment.buffer_before_minutes,
            "buffer_after_minutes": appointment.buffer_after_minutes,
        }
    )
    computed, slot = await offered_slot(
        db, service, [appointment.staff_id], starts_at, excluding=appointment.id
    )
    if slot is None:
        return not_offered()

    span = (
        slot.starts_at - timedelta(minutes=appointment.buffer_before_minutes),
        slot.ends_at + timedelta(minutes=appointment.buffer_after_minutes),
    )
    held = {r.resource_id for r in appointment.resources}
    claimed = assign_resources(
        service.requirements,
        sorted(computed.resources, key=lambda r: r.id not in held),
        computed.resource_busy,
        span,
    )
    if claimed is None:
        return not_offered()

    appointment.starts_at = slot.starts_at
    appointment.ends_at = slot.ends_at
    appointment.duration_minutes = duration
    appointment.resources = [
        AppointmentResource(
            resource_id=r.id, kind=r.kind, period=Range(span[0], span[1], bounds="[)")
        )
        for r in claimed
    ]
    try:
        await db.flush()
    except IntegrityError as error:
        await db.rollback()
        if not _is_slot_taken(error):
            raise
        return JSONResponse(
            status_code=409,
            content={"detail": "That time was just taken. Pick another.", "code": "slot_taken"},
        )
    if slot.starts_at != old_start:
        record_event(
            db,
            "appointment.rescheduled",
            target_type="appointment",
            target_id=str(appointment.id),
            actor_user_id=actor.id,
            metadata={"from": utc(old_start), "to": utc(slot.starts_at)},
        )
    if duration != old_duration:
        record_event(
            db,
            "appointment.resized",
            target_type="appointment",
            target_id=str(appointment.id),
            actor_user_id=actor.id,
            metadata={"from": old_duration, "to": duration},
        )
    await db.commit()
    return _out(await _load(db, appointment.id))


# --- listing ------------------------------------------------------------------------------


async def appointments_between(
    db: AsyncSession, from_: Date, to: Date, zone, staff_id: uuid.UUID | None = None
) -> list[AppointmentOut]:
    """Every appointment starting on a business-local date from `from_` to `to` inclusive,
    in start order; `staff_id` narrows to one column. Shared with `/api/schedule`, so the
    two reads can never disagree about which day an instant is on."""
    window = (
        _local_midnight(from_, zone),
        _local_midnight(to + timedelta(days=1), zone),
    )
    query = (
        select(Appointment)
        .join(Staff)
        .where(Appointment.starts_at >= window[0], Appointment.starts_at < window[1])
        .order_by(Appointment.starts_at, Staff.sort_order, Staff.display_name)
    )
    if staff_id is not None:
        query = query.where(Appointment.staff_id == staff_id)
    return [_out(a) for a in await db.scalars(query)]


@router.get("")
async def list_appointments(
    _: Viewer,
    db: SessionDep,
    from_: Annotated[Date, Query(alias="from")],
    to: Date,
    staff_id: uuid.UUID | None = None,
) -> AppointmentsOut:
    """Every appointment starting on a business-local date from `from` to `to` inclusive (at
    most 31 days), in start order; `staff_id` narrows to one column. Cancelled ones are
    included — the calendar decides how to draw them — with their `status` saying so."""
    check_range(from_, to)
    zone = await business_zone(db)
    return AppointmentsOut(
        timezone=str(zone), appointments=await appointments_between(db, from_, to, zone, staff_id)
    )


def _local_midnight(day: Date, zone):
    return localize(datetime.combine(day, time.min), zone)
