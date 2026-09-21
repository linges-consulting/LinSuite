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

**Advisory rules may be overridden; physical ones may not (tech-stack §22).** When the
engine does not offer a start for a named staff member, it is run once more *relaxed* — the
shift, time off, closures and the horizon set aside — purely to diagnose. Offered that way,
the answer is 422 `override_available` naming the `rules` the start breaks, and the screen
asks a human to confirm. Not offered even then, the conflict is a busy room or device or a
staff member at their limit, and the answer stays `not_offered`: there is nothing to
confirm. `override: true` books past the advisory rules — for one's own schedule with
`schedule.override_availability`, for anybody else's with `admin` in Admin Mode on top —
and is recorded as `appointment.availability_overridden` with the rules, the reason and the
authorizer. **The relaxed engine is reached from nowhere but these two places**, and the
client-facing booking endpoint (M3) must accept no `override` and run nothing relaxed.
"""

import uuid
from datetime import UTC, datetime, time, timedelta
from datetime import date as Date
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import AwareDatetime, BaseModel, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import Range
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from auth import modes
from auth.capabilities import ADMIN, BY_KEY, Requires
from auth.models import User
from auth.session import ClaimsDep
from core.audit import record_event
from core.db import SessionDep
from core.errors import CAPABILITY_REQUIRED, Forbidden
from customers.models import Customer
from customers.routes import CustomerIn, create_customer
from scheduling._admin_forms import blank_to_none, refuse
from scheduling.availability import Interval, Slot, advisory_breaches
from scheduling.clock import localize
from scheduling.models import Appointment, AppointmentResource, Staff
from scheduling.services import CatalogServiceOut, RequirementOut, catalog_entry
from scheduling.slots import Computed, ResourceRow, check_range, compute, unbookable, utc
from scheduling.time_off import business_zone

router = APIRouter(prefix="/appointments", tags=["appointments"])

Scheduler = Annotated[User, Depends(Requires("schedule.manage"))]
Viewer = Annotated[User, Depends(Requires("schedule.view"))]

OVERRIDE = "schedule.override_availability"

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
    # Task 18: when each terminal state was reached, and cancellation's free-text reason.
    completed_at: str | None
    cancelled_at: str | None
    cancel_reason: str | None
    no_show_at: str | None
    # The advisory rules this was confirmed past, and why — null when none were. The card's
    # marker; the authorizer is in the audit log.
    overridden_rules: list[str] | None
    override_reason: str | None
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
        completed_at=utc(appointment.completed_at) if appointment.completed_at else None,
        cancelled_at=utc(appointment.cancelled_at) if appointment.cancelled_at else None,
        cancel_reason=appointment.cancel_reason,
        no_show_at=utc(appointment.no_show_at) if appointment.no_show_at else None,
        overridden_rules=appointment.overridden_rules,
        override_reason=appointment.override_reason,
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


class OverrideIn(BaseModel):
    """The two fields a confirmed override adds to a booking or a move. `override` without
    a rule to override is a no-op, and without the right to override it is a 403 whether or
    not one was needed: asking for a power is refused, not ignored."""

    override: bool = False
    override_reason: Annotated[str | None, Field(max_length=500)] = None

    @field_validator("override_reason", mode="after")
    @classmethod
    def _reason_trimmed(cls, value: str | None) -> str | None:
        return blank_to_none(value)


class BookingIn(OverrideIn):
    """`starts_at` is the instant `/api/availability` offered — aware, so a naive local time
    is refused at the boundary rather than guessed at. Exactly one of `customer_id` and an
    inline `customer`."""

    service_id: uuid.UUID
    # None is "any available": the engine's slot names who could take it, and the first by
    # `sort_order` does. An override names one person: the rules and the right to set them
    # aside are both about a particular schedule.
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
    relax_advisory: bool = False,
) -> tuple[Computed, Slot | None]:
    """The engine's answer for the local day `starts_at` falls on, and the slot at exactly
    that instant if it is offered to one of `staff_ids`. Its own function so a test can hold
    the door open between the check and the insert (`test_two_concurrent_bookings...`).
    `excluding` is the appointment being moved, left out of what is busy; `relax_advisory`
    is the override question (module docstring), asked here and nowhere else."""
    day = starts_at.astimezone(await business_zone(db)).date()
    computed = await compute(
        db, service, staff_ids, day, day, excluding=excluding, relax_advisory=relax_advisory
    )
    slot = next((s for s in computed.days[day] if s.starts_at == starts_at), None)
    return computed, slot


def broken_rules(
    computed: Computed, staff_id: uuid.UUID, slot: Slot, before: int, after: int
) -> list[str]:
    """The advisory rules `slot`, buffered, breaks for `staff_id` — empty when the start
    would have been offered without relaxing anything."""
    zone = ZoneInfo(computed.timezone)
    return advisory_breaches(
        timezone=computed.timezone,
        staff=computed.staff[staff_id],
        day=slot.starts_at.astimezone(zone).date(),
        span=(slot.starts_at - timedelta(minutes=before), slot.ends_at + timedelta(minutes=after)),
        closures=computed.closures,
        horizon_ends_on=computed.horizon_ends_on,
    )


async def authorize_override(
    db: AsyncSession, actor: User, claims: dict, staff_id: uuid.UUID
) -> None:
    """Who may set the advisory rules aside for `staff_id`'s schedule (§22): the capability
    for one's own; `admin`, *in Admin Mode*, for anybody else's. Committing somebody else's
    evening is the administrative act, so it takes the administrative window, checked and
    slid exactly as `require_admin_mode` does."""
    if OVERRIDE not in actor.capabilities:
        raise Forbidden(
            CAPABILITY_REQUIRED, f"Your role does not allow this: {BY_KEY[OVERRIDE].description}"
        )
    own = await db.scalar(select(Staff.id).where(Staff.user_id == actor.id))
    if own == staff_id:
        return
    if ADMIN not in actor.capabilities:
        raise Forbidden(
            CAPABILITY_REQUIRED,
            "Only an administrator may book outside another staff member's availability.",
        )
    state = await modes.read_state(claims)
    if not state.in_admin_mode:
        raise modes.ADMIN_MODE_REQUIRED
    await modes.slide(claims, state.hard_limit_at)


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


def _with_link(content: dict, link_index: int | None) -> dict:
    """A group booking's refusal carries which link in the chain it was — the same code and
    detail a single booking would have sent, plus where in the chain it happened, so the
    dialog can point at the second service rather than the first."""
    if link_index is not None:
        content["link_index"] = link_index
    return content


def not_offered(link_index: int | None = None) -> JSONResponse:
    """The engine does not offer that start. FastAPI's 422 shape, plus a code the screen can
    switch on without reading `loc`: it means the same as `slot_taken` to a person — what
    you were looking at is out of date — and a screen should treat both alike."""
    return JSONResponse(
        status_code=422,
        content=_with_link(
            {
                "detail": [
                    {
                        "type": "value_error",
                        "loc": ["body", "starts_at"],
                        "msg": "That time is no longer available. Pick another.",
                    }
                ],
                "code": "not_offered",
            },
            link_index,
        ),
    )


def override_available(rules: list[str], link_index: int | None = None) -> JSONResponse:
    """The engine does not offer that start, but a human may: the same 422 shape as
    `not_offered` with the advisory rules the start breaks beside it, for the screen to
    put into words and ask about."""
    return JSONResponse(
        status_code=422,
        content=_with_link(
            {
                "detail": [
                    {
                        "type": "value_error",
                        "loc": ["body", "starts_at"],
                        "msg": "That time is outside availability. "
                        "It can be booked with an override.",
                    }
                ],
                "code": "override_available",
                "rules": rules,
            },
            link_index,
        ),
    )


def slot_taken(link_index: int | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content=_with_link(
            {"detail": "That time was just taken. Pick another.", "code": "slot_taken"}, link_index
        ),
    )


def invalid_transition(status: str) -> JSONResponse:
    """A status endpoint (or a move) asked of an appointment that is not `confirmed`. Every
    one of the four statuses is terminal but `confirmed` in M1 (module docstring's ruling),
    so the only question this answers is which one it already is."""
    return JSONResponse(
        status_code=409,
        content={
            "detail": f"A {status} appointment cannot be changed that way.",
            "code": "invalid_transition",
        },
    )


def not_yet_started() -> JSONResponse:
    """A no-show marked before `starts_at`: there is nothing to have missed yet."""
    return JSONResponse(
        status_code=422,
        content={
            "detail": "This appointment has not started yet.",
            "code": "not_yet_started",
        },
    )


def group_refused(field: str, message: str, link_index: int) -> JSONResponse:
    """A group link refused for a reason that is not the engine's own — the staff member
    named cannot deliver the service, or an override was asked with nobody named. Shaped
    like `scheduling._admin_forms.refuse`'s 422, with `link_index` for which link it was."""
    return JSONResponse(
        status_code=422,
        content={
            "detail": [
                {"type": "value_error", "loc": ["body", "links", link_index, field], "msg": message}
            ],
            "code": "invalid_link",
            "link_index": link_index,
        },
    )


def record_override(db: AsyncSession, appointment: Appointment, actor: User) -> None:
    record_event(
        db,
        "appointment.availability_overridden",
        target_type="appointment",
        target_id=str(appointment.id),
        actor_user_id=actor.id,
        metadata={
            "appointment_id": str(appointment.id),
            "rules": appointment.overridden_rules,
            "reason": appointment.override_reason,
            "authorizer_user_id": str(actor.id),
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
async def book_appointment(payload: BookingIn, actor: Scheduler, claims: ClaimsDep, db: SessionDep):
    """The documented body is `AppointmentOut`; the coded refusals are `JSONResponse`s
    because each carries something beside `detail` — the catalog's reasons and
    `not_bookable`, `not_offered`, `override_available`, or `slot_taken`.

    Staff-side only. The client-facing booking endpoint (M3) is a different route: it must
    accept no `override` and never run the engine relaxed."""
    service = await catalog_entry(db, payload.service_id)
    if not service.bookable:
        return unbookable(service)
    eligible = [uuid.UUID(s) for s in service.staff_ids]
    if payload.staff_id is not None and payload.staff_id not in eligible:
        raise refuse("staff_id", "That staff member cannot deliver this service.")
    candidates = [payload.staff_id] if payload.staff_id else eligible
    if payload.override:
        if payload.staff_id is None:
            raise refuse("staff_id", "Name the staff member whose availability is overridden.")
        await authorize_override(db, actor, claims, payload.staff_id)

    computed, slot = await offered_slot(
        db, service, candidates, payload.starts_at, relax_advisory=payload.override
    )
    if slot is None:
        if not payload.override and payload.staff_id is not None:
            # The diagnosis: reachable with the advisory rules lifted, or not at all?
            relaxed, reachable = await offered_slot(
                db, service, candidates, payload.starts_at, relax_advisory=True
            )
            if reachable is not None:
                before, after = service.buffer_before_minutes, service.buffer_after_minutes
                return override_available(
                    broken_rules(relaxed, payload.staff_id, reachable, before, after)
                )
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
    # What the override actually set aside — nothing, when the start was offered anyway.
    rules = (
        broken_rules(
            computed, staff_id, slot, service.buffer_before_minutes, service.buffer_after_minutes
        )
        if payload.override
        else []
    )

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
        overridden_rules=rules or None,
        override_reason=payload.override_reason if rules else None,
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
        return slot_taken()
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
    if rules:
        record_override(db, appointment, actor)
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


class ChangeIn(OverrideIn):
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
    appointment_id: uuid.UUID,
    payload: ChangeIn,
    actor: Scheduler,
    claims: ClaimsDep,
    db: SessionDep,
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
    and nothing else. The override rules are booking's (module docstring), for the
    appointment's own staff member; the marker follows the appointment — set by a confirmed
    override, cleared by a move that needed none.
    """
    appointment = await _load(db, appointment_id)
    if appointment is None:
        raise HTTPException(status_code=404, detail="No such appointment.")
    if appointment.status != "confirmed":
        return invalid_transition(appointment.status)
    if payload.override:
        await authorize_override(db, actor, claims, appointment.staff_id)
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
    before, after = appointment.buffer_before_minutes, appointment.buffer_after_minutes
    computed, slot = await offered_slot(
        db,
        service,
        [appointment.staff_id],
        starts_at,
        excluding=appointment.id,
        relax_advisory=payload.override,
    )
    if slot is None:
        if not payload.override:
            relaxed, reachable = await offered_slot(
                db,
                service,
                [appointment.staff_id],
                starts_at,
                excluding=appointment.id,
                relax_advisory=True,
            )
            if reachable is not None:
                return override_available(
                    broken_rules(relaxed, appointment.staff_id, reachable, before, after)
                )
        return not_offered()

    span = (slot.starts_at - timedelta(minutes=before), slot.ends_at + timedelta(minutes=after))
    held = {r.resource_id for r in appointment.resources}
    claimed = assign_resources(
        service.requirements,
        sorted(computed.resources, key=lambda r: r.id not in held),
        computed.resource_busy,
        span,
    )
    if claimed is None:
        return not_offered()
    rules = (
        broken_rules(computed, appointment.staff_id, slot, before, after)
        if payload.override
        else []
    )

    appointment.starts_at = slot.starts_at
    appointment.ends_at = slot.ends_at
    appointment.duration_minutes = duration
    appointment.overridden_rules = rules or None
    appointment.override_reason = payload.override_reason if rules else None
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
        return slot_taken()
    if rules:
        record_override(db, appointment, actor)
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


# --- status lifecycle (Task 18) ------------------------------------------------------------
#
# `confirmed → completed | cancelled | no_show`, every one of the three terminal — no reopen
# in M1. Each transition is its own endpoint rather than a generic "set status" so the
# request itself says what happened, and each has exactly one body: `complete_appointment`
# is the single place `completed_at` and `appointment.completed` are ever written, because
# later phases (treatment receipts, package credits, commission) key off that event and a
# second path to it would be a second place for them to disagree about whether it happened.
#
# **Cancelling frees the room.** `appointment_resources` has no WHERE clause on its exclusion
# constraint (`scheduling/models.py`), so a cancelled booking stops claiming its resources by
# no longer having the rows — `appointment.resources = []` deletes them (`delete-orphan`).
# A no-show frees them too: the span is over either way, and leaving stale claims on a
# no-show would make a room "busy" for a visit that never happened.


class CancelIn(BaseModel):
    """The one thing a cancellation may say beyond "not this": why. Optional, and free text
    like `time_off.reason` — a dropdown of reasons would enumerate a client's business."""

    reason: Annotated[str | None, Field(max_length=500)] = None

    @field_validator("reason", mode="after")
    @classmethod
    def _trimmed(cls, value: str | None) -> str | None:
        return blank_to_none(value)


def _cancel(db: AsyncSession, appointment: Appointment, actor: User, reason: str | None) -> None:
    """The one cancellation code path — a single member and every member of a group both
    call this, so "cancelled" means the same three things everywhere it happens."""
    appointment.status = "cancelled"
    appointment.cancelled_at = datetime.now(UTC)
    appointment.cancel_reason = reason
    appointment.resources = []
    record_event(
        db,
        "appointment.cancelled",
        target_type="appointment",
        target_id=str(appointment.id),
        actor_user_id=actor.id,
        metadata={"reason": reason},
    )


@router.post("/{appointment_id}/complete", response_model=AppointmentOut)
async def complete_appointment(appointment_id: uuid.UUID, actor: Scheduler, db: SessionDep):
    """The one explicit, recorded event later phases hang behaviour on (module docstring;
    CLAUDE.md "package credits deduct on completion"). Only from `confirmed`."""
    appointment = await _load(db, appointment_id)
    if appointment is None:
        raise HTTPException(status_code=404, detail="No such appointment.")
    if appointment.status != "confirmed":
        return invalid_transition(appointment.status)
    appointment.status = "completed"
    appointment.completed_at = datetime.now(UTC)
    record_event(
        db,
        "appointment.completed",
        target_type="appointment",
        target_id=str(appointment.id),
        actor_user_id=actor.id,
        metadata={},
    )
    await db.commit()
    return _out(await _load(db, appointment.id))


@router.post("/{appointment_id}/cancel", response_model=AppointmentOut)
async def cancel_appointment(
    appointment_id: uuid.UUID, payload: CancelIn, actor: Scheduler, db: SessionDep
):
    """Cancelling one member of a group leaves the others untouched — only
    `POST /group/{id}/cancel` acts on the whole visit."""
    appointment = await _load(db, appointment_id)
    if appointment is None:
        raise HTTPException(status_code=404, detail="No such appointment.")
    if appointment.status != "confirmed":
        return invalid_transition(appointment.status)
    _cancel(db, appointment, actor, payload.reason)
    await db.commit()
    return _out(await _load(db, appointment.id))


@router.post("/{appointment_id}/no-show", response_model=AppointmentOut)
async def mark_no_show(appointment_id: uuid.UUID, actor: Scheduler, db: SessionDep):
    """Distinguishable from cancelled in reporting (acceptance criteria): the client did not
    cancel, they did not come. Only once `starts_at` has passed — there is nothing to have
    missed yet before then."""
    appointment = await _load(db, appointment_id)
    if appointment is None:
        raise HTTPException(status_code=404, detail="No such appointment.")
    if appointment.status != "confirmed":
        return invalid_transition(appointment.status)
    now = datetime.now(UTC)
    if now < appointment.starts_at:
        return not_yet_started()
    appointment.status = "no_show"
    appointment.no_show_at = now
    appointment.resources = []
    record_event(
        db,
        "appointment.no_show",
        target_type="appointment",
        target_id=str(appointment.id),
        actor_user_id=actor.id,
        metadata={},
    )
    await db.commit()
    return _out(await _load(db, appointment.id))


# --- booking groups (Task 18) ---------------------------------------------------------------
#
# A visit is an ordered chain of services for one customer, each link its own appointment —
# own staff, own resources, own snapshot — sharing one `booking_group_id`. The sequential
# search (`scheduling.availability.chain_starts`) finds the starts; this books them and
# cancels them together.


class GroupOut(BaseModel):
    booking_group_id: str
    appointments: list[AppointmentOut]


class GroupLinkIn(OverrideIn):
    service_id: uuid.UUID
    # None is "any available", resolved per link the same way a single booking resolves it.
    staff_id: uuid.UUID | None = None


class GroupBookingIn(BaseModel):
    """`starts_at` is link 0's start — a start `/api/availability/group` offered. Every
    following link's start is computed here, at the link before it's `ends_at`; the client
    never sends them."""

    starts_at: AwareDatetime
    links: Annotated[list[GroupLinkIn], Field(min_length=1)]
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


@router.post("/group", status_code=201, response_model=GroupOut)
async def book_group(payload: GroupBookingIn, actor: Scheduler, claims: ClaimsDep, db: SessionDep):
    """Every link validated exactly as `book_appointment` validates one booking — offered at
    its computed start, resources distinct per link, authorization for its own override —
    and all of it in the one transaction this request already holds: nothing commits until
    every link has. A refusal at link `i` is that link's own code, `link_index` beside it,
    and (because nothing before this was committed) nothing written; a constraint race is
    409 `slot_taken` and the same full rollback.
    """
    if payload.customer is not None:
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

    group_id = uuid.uuid4()
    created: list[Appointment] = []
    cursor = payload.starts_at
    try:
        for i, link in enumerate(payload.links):
            service = await catalog_entry(db, link.service_id)
            if not service.bookable:
                return unbookable(service, link_index=i)
            eligible = [uuid.UUID(s) for s in service.staff_ids]
            if link.staff_id is not None and link.staff_id not in eligible:
                return group_refused(
                    "staff_id", "That staff member cannot deliver this service.", i
                )
            candidates = [link.staff_id] if link.staff_id else eligible
            if link.override:
                if link.staff_id is None:
                    return group_refused(
                        "staff_id", "Name the staff member whose availability is overridden.", i
                    )
                await authorize_override(db, actor, claims, link.staff_id)

            computed, slot = await offered_slot(
                db, service, candidates, cursor, relax_advisory=link.override
            )
            if slot is None:
                if not link.override and link.staff_id is not None:
                    relaxed, reachable = await offered_slot(
                        db, service, candidates, cursor, relax_advisory=True
                    )
                    if reachable is not None:
                        before = service.buffer_before_minutes
                        after = service.buffer_after_minutes
                        return override_available(
                            broken_rules(relaxed, link.staff_id, reachable, before, after),
                            link_index=i,
                        )
                return not_offered(link_index=i)

            staff_id = link.staff_id or await db.scalar(
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
                return not_offered(link_index=i)
            rules = (
                broken_rules(
                    computed,
                    staff_id,
                    slot,
                    service.buffer_before_minutes,
                    service.buffer_after_minutes,
                )
                if link.override
                else []
            )

            appointment = Appointment(
                customer_id=customer.id,
                staff_id=staff_id,
                service_id=uuid.UUID(service.id),
                starts_at=slot.starts_at,
                ends_at=slot.ends_at,
                duration_minutes=service.duration_minutes,
                buffer_before_minutes=service.buffer_before_minutes,
                buffer_after_minutes=service.buffer_after_minutes,
                price_cents=service.price_cents,
                status="confirmed",
                booking_group_id=group_id,
                notes=payload.notes if i == 0 else None,
                overridden_rules=rules or None,
                override_reason=link.override_reason if rules else None,
                created_by_user_id=actor.id,
                resources=[
                    AppointmentResource(
                        resource_id=r.id, kind=r.kind, period=Range(span[0], span[1], bounds="[)")
                    )
                    for r in claimed
                ],
            )
            db.add(appointment)
            created.append(appointment)
            cursor = slot.ends_at
        await db.flush()
    except IntegrityError as error:
        await db.rollback()
        if not _is_slot_taken(error):
            raise
        return slot_taken()

    for appointment in created:
        record_event(
            db,
            "appointment.booked",
            target_type="appointment",
            target_id=str(appointment.id),
            actor_user_id=actor.id,
            metadata={
                "service_id": str(appointment.service_id),
                "staff_id": str(appointment.staff_id),
                "customer_id": str(customer.id),
            },
        )
        if appointment.overridden_rules:
            record_override(db, appointment, actor)
    record_event(
        db,
        "group.booked",
        target_type="booking_group",
        target_id=str(group_id),
        actor_user_id=actor.id,
        metadata={
            "group_id": str(group_id),
            "appointment_ids": [str(a.id) for a in created],
        },
    )
    await db.commit()
    loaded = [await _load(db, a.id) for a in created]
    return GroupOut(booking_group_id=str(group_id), appointments=[_out(a) for a in loaded])


@router.post("/group/{group_id}/cancel", response_model=GroupOut)
async def cancel_group(group_id: uuid.UUID, payload: CancelIn, actor: Scheduler, db: SessionDep):
    """Every non-terminal member, the same code path a single cancel uses — a completed
    member stays completed, an already-cancelled one is untouched."""
    members = list(
        await db.scalars(
            select(Appointment)
            .where(Appointment.booking_group_id == group_id)
            .order_by(Appointment.starts_at)
            .execution_options(populate_existing=True)
        )
    )
    if not members:
        raise HTTPException(status_code=404, detail="No such booking group.")
    cancelled_ids = []
    for appointment in members:
        if appointment.status != "confirmed":
            continue
        _cancel(db, appointment, actor, payload.reason)
        cancelled_ids.append(str(appointment.id))
    if cancelled_ids:
        record_event(
            db,
            "group.cancelled",
            target_type="booking_group",
            target_id=str(group_id),
            actor_user_id=actor.id,
            metadata={"group_id": str(group_id), "appointment_ids": cancelled_ids},
        )
    await db.commit()
    loaded = [await _load(db, a.id) for a in members]
    return GroupOut(booking_group_id=str(group_id), appointments=[_out(a) for a in loaded])


# --- listing ------------------------------------------------------------------------------


async def appointments_between(
    db: AsyncSession,
    from_: Date,
    to: Date,
    zone,
    staff_id: uuid.UUID | None = None,
    *,
    include_cancelled: bool = False,
) -> list[AppointmentOut]:
    """Every appointment starting on a business-local date from `from_` to `to` inclusive,
    in start order; `staff_id` narrows to one column. Shared with `/api/schedule`, so the
    two reads can never disagree about which day an instant is on. Cancelled appointments
    are left out unless `include_cancelled` — the calendar's own toggle (Task 18); the
    default keeps the day's picture to what is actually happening."""
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
    if not include_cancelled:
        query = query.where(Appointment.status != "cancelled")
    return [_out(a) for a in await db.scalars(query)]


@router.get("")
async def list_appointments(
    _: Viewer,
    db: SessionDep,
    from_: Annotated[Date, Query(alias="from")],
    to: Date,
    staff_id: uuid.UUID | None = None,
    include_cancelled: bool = False,
) -> AppointmentsOut:
    """Every appointment starting on a business-local date from `from` to `to` inclusive (at
    most 31 days), in start order; `staff_id` narrows to one column. Cancelled ones are left
    out unless `include_cancelled=true` — the calendar decides how to draw them when it asks
    for them, with `status` saying so."""
    check_range(from_, to)
    zone = await business_zone(db)
    return AppointmentsOut(
        timezone=str(zone),
        appointments=await appointments_between(
            db, from_, to, zone, staff_id, include_cancelled=include_cancelled
        ),
    )


def _local_midnight(day: Date, zone):
    return localize(datetime.combine(day, time.min), zone)
