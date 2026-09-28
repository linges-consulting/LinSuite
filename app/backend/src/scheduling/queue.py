"""`/api/queue-entries`: add a walk-in, list who is waiting, mark one abandoned (Phase 7 Task
2, #12) — the "take a number" queue, never the always-on "next available" search shortcut
(Task 3, a different feature entirely; CLAUDE.md's "two things are called 'walk-in'").

**Gated on `businesses.enable_walk_in_queue`, structurally, on every route** — "with the queue
disabled, no queue surface exists anywhere in the product" (#12's own acceptance criterion,
applied here to the API layer now that a real one exists). The closest existing precedent for
a *whole feature surface* gated by a business toggle is `scheduling/public.py`'s
`online_booking_enabled` check: a 404 before anything else runs, checked first in every
handler. `business.sms_enabled` is not that shape — it only ever gates whether a single send
is attempted (`notifications/triggers.py::dispatch`), never a whole route — so this copies the
public-booking style, not the SMS one, the same call Phase 6 Task 4's own toggle-of-the-whole-
portal check makes. Unlike the public routes this one is staff-authenticated, so the 404 has
no anti-enumeration purpose (a staff member can already see the toggle in Settings) — it is
still a plain 404, because "this feature does not exist here" is the honest answer, not a 403.

**One capability, `queue.manage`**, for add/list/abandon alike — the surface is small enough
that splitting a `.view` off a `.manage` (the way `schedule.view`/`schedule.manage` split for
the calendar) would be a distinction nobody at a front desk needs; `catalog.manage` already
covers both halves of a smaller surface the same way.

**Quick-create identity**: exactly one of `customer_id` (a known returning client, matched the
same way the booking dialog's own search already works) or `bare_name` (+ optional
`bare_phone`) — "a walk-in may never become a full customer record" (CLAUDE.md). The API-level
validator mirrors the database's own `ck_queue_entries_identity` CHECK
(`num_nonnulls(customer_id, bare_name) = 1`) so a bad request is a 422 with a field-level
reason, not a 500 off an `IntegrityError` the CHECK was always going to catch anyway.

**Audited, not `LogAccess`-ed**: adding and abandoning a queue entry each get a `record_event`
— the same discipline `appointment.booked`/`.cancelled` and `customer.created` already apply
to every operational create/transition in this codebase, audited-not-logged because a queue
entry is operational schedule data, not a PHI read the way opening a customer profile is (no
`scheduling/*.py` route uses `LogAccess` anywhere — listing or booking appointments doesn't
either). Listing the queue is a plain read with no `record_event` of its own, the same as
`GET /api/appointments`.

**Abandoned entries are excluded from the default list** (this task's own call, documented
here since m3.md left it open): the front desk cares who is still waiting, and an abandoned
entry has nothing left to act on. `?include_abandoned=true` shows the full history for the one
case that wants it (Task 8's "abandoned is recordable and countable" — countable via this flag
plus a status filter, not a second endpoint).

**Conversion to `in_service`/`done`** is Task 5's `POST /queue-entries/{id}/start` (below): the
fourth call site of `assign_resources` (staff booking, public booking, public reschedule, this
one), gated by `queue_eligibility.can_start` (Task 4) before ever touching it — never a fourth
implementation of the physical/advisory checks either. `cache.bump()` *is* called there, once
the new `Appointment` actually commits — the one write in this file that changes bookability;
add/list/abandon still never call it, unchanged from Task 2.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import Range
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from auth.capabilities import Requires
from auth.models import User
from core.audit import record_event
from core.db import SessionDep
from customers.models import Customer
from customers.routes import CustomerIn, create_customer
from notifications.triggers import load_business
from scheduling import cache
from scheduling._admin_forms import refuse
from scheduling.appointments import (
    AppointmentOut,
    _is_slot_taken,
    assign_resources,
    customer_suppressed,
    invalid_transition,
    slot_taken,
)
from scheduling.appointments import _load as _load_appointment
from scheduling.appointments import _out as _appointment_out
from scheduling.availability import ServiceSpec
from scheduling.models import Appointment, AppointmentResource, Closure, QueueEntry, Service, Staff
from scheduling.queue_eligibility import can_start
from scheduling.services import catalog_entry
from scheduling.slots import active_resources, busy_intervals, staff_specs, unbookable
from scheduling.time_off import business_zone

router = APIRouter(prefix="/queue-entries", tags=["queue"])

Manager = Annotated[User, Depends(Requires("queue.manage"))]


async def _feature_gate(db: AsyncSession) -> None:
    business = await load_business(db)
    if not business.enable_walk_in_queue:
        raise HTTPException(status_code=404, detail="Not found.")


def _blank_to_none(value: str | None) -> str | None:
    if value is None:
        return None
    trimmed = value.strip()
    return trimmed or None


class AddQueueEntryIn(BaseModel):
    """Exactly one of `customer_id`/`bare_name`, mirroring `ck_queue_entries_identity`.
    `bare_phone` only ever accompanies `bare_name` — a matched customer's phone already lives
    on their own record."""

    customer_id: uuid.UUID | None = None
    bare_name: Annotated[str | None, Field(max_length=200)] = None
    bare_phone: Annotated[str | None, Field(max_length=32)] = None
    requested_service_id: uuid.UUID
    preferred_staff_id: uuid.UUID | None = None

    @model_validator(mode="before")
    @classmethod
    def _trim(cls, data):
        if isinstance(data, dict):
            for field in ("bare_name", "bare_phone"):
                if field in data:
                    data[field] = _blank_to_none(data[field])
        return data

    @model_validator(mode="after")
    def _identity(self):
        if (self.customer_id is None) == (self.bare_name is None):
            raise ValueError("send exactly one of customer_id or bare_name")
        if self.bare_phone and self.customer_id is not None:
            raise ValueError("bare_phone only applies to a name-only walk-in")
        return self


class ServiceRef(BaseModel):
    id: str
    name: str


class StaffRef(BaseModel):
    id: str
    display_name: str


class CustomerRef(BaseModel):
    id: str
    first_name: str
    last_name: str
    phone: str | None


class QueueEntryOut(BaseModel):
    id: str
    status: str
    arrived_at: datetime
    requested_service: ServiceRef
    preferred_staff: StaffRef | None
    customer: CustomerRef | None
    bare_name: str | None
    bare_phone: str | None
    # Phase 7 Task 5: set once `start` converts this entry — Task 8's display screen reads it
    # to link through to the appointment (session notes, treatment receipt, and so on).
    appointment_id: str | None


def _out(entry: QueueEntry) -> QueueEntryOut:
    return QueueEntryOut(
        id=str(entry.id),
        status=entry.status,
        arrived_at=entry.arrived_at,
        requested_service=ServiceRef(
            id=str(entry.requested_service.id), name=entry.requested_service.name
        ),
        preferred_staff=StaffRef(
            id=str(entry.preferred_staff.id), display_name=entry.preferred_staff.display_name
        )
        if entry.preferred_staff is not None
        else None,
        customer=CustomerRef(
            id=str(entry.customer.id),
            first_name=entry.customer.first_name,
            last_name=entry.customer.last_name,
            phone=entry.customer.phone,
        )
        if entry.customer is not None
        else None,
        appointment_id=str(entry.appointment_id) if entry.appointment_id is not None else None,
        bare_name=entry.bare_name,
        bare_phone=entry.bare_phone,
    )


async def _load(db: AsyncSession, entry_id: uuid.UUID) -> QueueEntry | None:
    # `populate_existing` for the same reason `scheduling.appointments._load` uses it: this
    # session is `expire_on_commit=False`, and the joined relationships on a row this request
    # just inserted were never loaded.
    return await db.scalar(
        select(QueueEntry)
        .where(QueueEntry.id == entry_id)
        .execution_options(populate_existing=True)
    )


async def _lock(db: AsyncSession, entry_id: uuid.UUID) -> QueueEntry | None:
    """`SELECT ... FOR UPDATE` on the bare row first, the joined reload second — the same
    two-step `scheduling.appointments._lock` uses, and for the same reason: `customer`/
    `preferred_staff` are nullable FKs, `lazy="joined"` makes them a LEFT OUTER JOIN, and
    Postgres refuses `FOR UPDATE` on the nullable side of an outer join."""
    locked = await db.scalar(
        select(QueueEntry.id).where(QueueEntry.id == entry_id).with_for_update()
    )
    if locked is None:
        return None
    return await _load(db, entry_id)


@router.post("", status_code=201, response_model=QueueEntryOut)
async def add_queue_entry(payload: AddQueueEntryIn, actor: Manager, db: SessionDep):
    await _feature_gate(db)

    service = await db.get(Service, payload.requested_service_id)
    if service is None or not service.active:
        raise HTTPException(status_code=404, detail="No such service.")

    if payload.preferred_staff_id is not None:
        staff = await db.get(Staff, payload.preferred_staff_id)
        if staff is None or not staff.active:
            raise HTTPException(status_code=404, detail="No such staff member.")

    if payload.customer_id is not None:
        customer = await db.get(Customer, payload.customer_id)
        if customer is None or customer.suppressed_at is not None:
            raise HTTPException(status_code=404, detail="No such customer.")

    entry = QueueEntry(
        customer_id=payload.customer_id,
        bare_name=payload.bare_name,
        bare_phone=payload.bare_phone,
        requested_service_id=payload.requested_service_id,
        preferred_staff_id=payload.preferred_staff_id,
    )
    db.add(entry)
    await db.flush()
    record_event(
        db,
        "queue.entry_added",
        target_type="queue_entry",
        target_id=str(entry.id),
        actor_user_id=actor.id,
        metadata={"identity": "customer" if payload.customer_id else "walk_in"},
    )
    await db.commit()
    return _out(await _load(db, entry.id))


class QueueOut(BaseModel):
    entries: list[QueueEntryOut]


@router.get("", response_model=QueueOut)
async def list_queue(_: Manager, db: SessionDep, include_abandoned: bool = False):
    """Oldest arrival first — first in, first served. Abandoned entries are left out unless
    `include_abandoned` is set (module docstring's own documented call)."""
    await _feature_gate(db)
    # `arrived_at` then `id` — the same tie-break `customers/routes.py::find_customers` uses,
    # so two entries that land in the same instant never swap places between requests.
    query = select(QueueEntry).order_by(QueueEntry.arrived_at, QueueEntry.id)
    if not include_abandoned:
        query = query.where(QueueEntry.status != "abandoned")
    entries = list(await db.scalars(query))
    return QueueOut(entries=[_out(e) for e in entries])


@router.post("/{entry_id}/abandon", response_model=QueueEntryOut)
async def abandon_queue_entry(entry_id: uuid.UUID, actor: Manager, db: SessionDep):
    """Only from `waiting` — an entry already `in_service`/`done` has moved on (Task 5), and a
    second `abandoned` on top of one already `abandoned` is not a new fact."""
    await _feature_gate(db)
    entry = await _lock(db, entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="No such queue entry.")
    if entry.status != "waiting":
        return invalid_transition(entry.status)
    entry.status = "abandoned"
    record_event(
        db,
        "queue.entry_abandoned",
        target_type="queue_entry",
        target_id=str(entry.id),
        actor_user_id=actor.id,
        metadata={},
    )
    await db.commit()
    return _out(await _load(db, entry.id))


# --- start: conversion to an appointment (Task 5, #12) --------------------------------------


def not_eligible(reason: str) -> JSONResponse:
    """Every staff candidate `can_start` was asked about refused — busy, or outside shift with
    no override path (unlike a scheduled booking's dialog, a walk-in start is immediate: there
    is no human on the other end confirming past an advisory rule for it, so this endpoint
    never accepts `override`). Same 422 shape as `scheduling.appointments.not_offered`, with
    `can_start`'s own `Eligibility.reason` beside it (`queue_eligibility.STAFF_BUSY` or one of
    `availability.ADVISORY_RULES`) — "a clear 422 naming the reason" (m3.md's own text)."""
    return JSONResponse(
        status_code=422,
        content={
            "detail": "This walk-in cannot be started right now.",
            "code": "not_eligible",
            "reason": reason,
        },
    )


def resource_unavailable() -> JSONResponse:
    """`can_start` never checks resources at all, deliberately (its own module docstring) —
    this is the one place a walk-in's room/device requirement is actually asked, through the
    same `assign_resources` every other booking path uses. Reachable even when every staff
    candidate was otherwise eligible: `can_start` and a resource claim are two different
    questions, same as they are for a scheduled booking."""
    return JSONResponse(
        status_code=422,
        content={
            "detail": "No free room or device for this service right now.",
            "code": "resource_unavailable",
        },
    )


class QueueStartOut(BaseModel):
    """What Task 8's queue display screen reads back from a successful start: the queue entry
    itself, now `in_service` and carrying `appointment_id`, and the real `Appointment` it
    became — the exact shape `POST /api/appointments` itself returns, so nothing downstream
    needs a second appointment schema for a walk-in's."""

    queue_entry: QueueEntryOut
    appointment: AppointmentOut


@router.post("/{entry_id}/start", status_code=201, response_model=QueueStartOut)
async def start_queue_entry(entry_id: uuid.UUID, actor: Manager, db: SessionDep):
    """A waiting entry becomes an ordinary `Appointment`, `starts_at=now` — the fourth call
    site of `assign_resources` (staff booking, public booking, public reschedule, this one),
    never a fourth implementation of it. `queue_eligibility.can_start` (Task 4) is the
    eligibility gate, asked before any of it: staff concurrency (physical, never overridable)
    first, then the shift/time-off/closure/horizon rules (advisory) second — **no override
    path here**, unlike `book_appointment`'s dialog: a walk-in start is immediate, and nobody
    is being asked to confirm past an advisory rule for it.

    `preferred_staff_id` set means exactly that person or refusal; unset means "any eligible
    staff who can start it right now" — tried in the same lowest-`sort_order`-then-
    `display_name` order `book_appointment`'s own "any available" resolution already uses,
    the first one `can_start` accepts wins.

    A bare-name entry (no `customer_id`) gets a real `Customer` created here, inline — the same
    no-actor `create_customer` shape the public booking route already uses for its own
    no-signed-in-caller identity. `Appointment.customer_id` is `NOT NULL` (unlike the public
    route's own `created_by_user_id`), so this is the first point one has to exist for a walk-in
    that never gave more than a name; the name is split on its first space (a single word
    becomes both first and last) since Task 2's quick-create only ever collected one free-text
    field, never a first/last pair.
    """
    business = await load_business(db)
    if not business.enable_walk_in_queue:
        raise HTTPException(status_code=404, detail="Not found.")

    entry = await _lock(db, entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="No such queue entry.")
    if entry.status != "waiting":
        return invalid_transition(entry.status)

    service = await catalog_entry(db, entry.requested_service_id)
    if not service.bookable:
        return unbookable(service)

    eligible = [uuid.UUID(s) for s in service.staff_ids]
    if entry.preferred_staff_id is not None:
        if entry.preferred_staff_id not in eligible:
            raise refuse("preferred_staff_id", "That staff member cannot deliver this service.")
        candidates = [entry.preferred_staff_id]
    else:
        # The same lowest-`sort_order`-then-`display_name` tie-break `book_appointment`'s own
        # "any available" resolution already uses — tried in this order until one is eligible.
        candidates = list(
            await db.scalars(
                select(Staff.id)
                .where(Staff.id.in_(eligible))
                .order_by(Staff.sort_order, Staff.display_name, Staff.id)
            )
        )

    zone = await business_zone(db)
    now = datetime.now(UTC)
    today = now.astimezone(zone).date()
    # The one span this whole request is about: `can_start`'s own arithmetic (buffer before,
    # duration, buffer after), computed once here so the busy-interval query and the
    # eligibility check ask about exactly the same instant.
    span = (
        now - timedelta(minutes=service.buffer_before_minutes),
        now + timedelta(minutes=service.duration_minutes + service.buffer_after_minutes),
    )
    horizon_ends_on = today + timedelta(days=business.booking_horizon_days)
    closures = frozenset(await db.scalars(select(Closure.date).where(Closure.date == today)))

    resources = await active_resources(db)
    specs = await staff_specs(db, candidates, span)
    staff_busy, resource_busy = await busy_intervals(
        db, candidates, [r.id for r in resources], span
    )

    staff_id: uuid.UUID | None = None
    reason: str | None = None
    for candidate in candidates:
        eligibility = can_start(
            staff=specs[candidate],
            service=ServiceSpec(
                duration_minutes=service.duration_minutes,
                buffer_before_minutes=service.buffer_before_minutes,
                buffer_after_minutes=service.buffer_after_minutes,
            ),
            timezone=str(zone),
            now=now,
            staff_busy=staff_busy.get(candidate, ()),
            closures=closures,
            horizon_ends_on=horizon_ends_on,
        )
        if eligibility.eligible:
            staff_id = candidate
            break
        reason = reason or eligibility.reason
    if staff_id is None:
        return not_eligible(reason or "no_staff_available")

    claimed = assign_resources(service.requirements, resources, resource_busy, span)
    if claimed is None:
        return resource_unavailable()

    if entry.customer_id is not None:
        # FOR KEY SHARE: the same erasure-race discipline every other booking path takes
        # (`scheduling.appointments.customer_suppressed`'s own docstring).
        customer = await db.get(
            Customer,
            entry.customer_id,
            with_for_update={"key_share": True},
            populate_existing=True,
        )
        if customer is None:
            raise HTTPException(status_code=404, detail="No such customer.")
        if customer.suppressed_at is not None:
            return customer_suppressed()
    else:
        first, _, last = entry.bare_name.partition(" ")
        customer = await create_customer(
            db,
            CustomerIn(first_name=first, last_name=last or first, phone=entry.bare_phone),
            actor_id=None,
        )

    appointment = Appointment(
        customer_id=customer.id,
        staff_id=staff_id,
        service_id=uuid.UUID(service.id),
        starts_at=now,
        ends_at=now + timedelta(minutes=service.duration_minutes),
        # The snapshot, same rule every other booking path follows (`Service`'s own SNAPSHOT
        # CONTRACT).
        duration_minutes=service.duration_minutes,
        buffer_before_minutes=service.buffer_before_minutes,
        buffer_after_minutes=service.buffer_after_minutes,
        price_cents=service.price_cents,
        status="confirmed",
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
    except DBAPIError as error:
        await db.rollback()
        if not _is_slot_taken(error):
            raise
        return slot_taken()

    entry.status = "in_service"
    entry.appointment_id = appointment.id
    record_event(
        db,
        "appointment.booked",
        target_type="appointment",
        target_id=str(appointment.id),
        actor_user_id=actor.id,
        metadata={
            "service_id": service.id,
            "staff_id": str(staff_id),
            "customer_id": str(customer.id),
            "source": "walk_in_queue",
        },
    )
    record_event(
        db,
        "queue.entry_started",
        target_type="queue_entry",
        target_id=str(entry.id),
        actor_user_id=actor.id,
        metadata={"appointment_id": str(appointment.id)},
    )
    await db.commit()
    await cache.bump()

    return QueueStartOut(
        queue_entry=_out(await _load(db, entry.id)),
        appointment=_appointment_out(await _load_appointment(db, appointment.id)),
    )
