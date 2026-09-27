"""`GET /api/public/booking/availability` and `POST /api/public/booking` (Phase 6 Tasks 1-2,
#10): the client-facing counterpart to `scheduling/slots.py`'s `GET /availability` and
`scheduling/appointments.py`'s `POST /api/appointments`, over the client's own unauthenticated
request.

No capability, no session — mounted under `/public/booking` (`main.py`'s `/api/public/`
prefix), which inherits Traefik's `public`/`public-ratelimit` middleware and `main.py`'s
`Cache-Control: no-store`/`Referrer-Policy: no-referrer`/Origin-check treatment for the whole
prefix (`forms/public.py`'s pattern, mirrored exactly) — no second Traefik router.

**`relax_advisory` is not reachable from here, structurally.** `scheduling/slots.py`'s
`resolve_availability` is the one function both this route and the staff route call, and it
has no parameter of that name at all — the override/relax switch (`scheduling/availability.
py`'s and `scheduling/appointments.py`'s docstrings) is reached from nowhere but the staff
booking path's diagnose step and its actual override. A client can never see an
out-of-shift, time-off, closure or beyond-horizon slot through this endpoint even when one
exists for staff (`tests/test_booking_public.py`'s adversarial case). **`PublicBookingIn` has
no `override` field at all** — not merely defaulted off, structurally absent, the same
discipline Task 1 applied to `resolve_availability`'s signature — and `book_public` never
passes anything but the engine's own default to `offered_slot`.

**`bookable_online` is the per-service gate** (`scheduling/services.py`'s `ServiceFields`,
already stored since M1/M2). A service an administrator has not opted into online booking is
a 404 here — the same generic "No such service." `catalog_entry` already gives an unknown or
inactive one, since there is no reason a client should be able to tell "exists but is
in-person-only" apart from "does not exist". The separate, *business-wide*
`online_booking_enabled` toggle (Phase 6 Task 4) is a later gate on the whole portal; this
route does not check it, per this task's own scope note in `m3.md`.

**Booking is one atomic request against the same `offered_slot`/`assign_resources` primitives
staff booking uses** (`scheduling/appointments.py::book_appointment`) — immediately
`confirmed`, no hold/unconfirmed state at all (m3.md's owner ruling: `APPOINTMENT_STATUSES`
does not grow a new value for this). **Client match-or-create**: an exact match on the
normalised email or phone (`_match_existing_customer`), never the booking dialog's fuzzy
prefix search (`customers/routes.py::find_customers`, built for a human narrowing a list, not
for deciding whether this is the same person) — otherwise `create_customer` inline, the same
function staff booking already calls, with no actor at all (migration 0037 made
`appointments.created_by_user_id` and `create_customer`'s `actor_id` both accept `None`).

**Abuse controls** (#10's own acceptance criteria), checked in this order, before anything
touches the database: the honeypot first (`PublicBookingIn.website` must arrive empty, or the
request is silently accepted and thrown away — a 200 indistinguishable from a real booking's),
then the per-IP and per-email **daily** caps (`_daily_cap_exceeded`, a day window, Redis
`incr`+`expire(86400)` keyed on the digest of the address/email — `auth/throttle.py`'s
digest-keying style, not `forms/public.py`'s 60-second one). `online_booking_enabled` is
Task 4's gate, not built yet — treated as always on for now, per this task's own scope note.

**Notification**: `notify_booking_confirmed` (Phase 12 Task 5) is called on success, over the
freshly committed, freshly reloaded appointment. `dispatch` (`notifications/triggers.py`)
already no-ops silently when the business is not `email_ready`/`sms_ready` — the booking has
already committed by the time it runs, so nothing here needs its own try/except to keep a
missing sender from failing the request."""

import hashlib
import uuid
from datetime import date as Date
from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import AwareDatetime, BaseModel, Field, model_validator
from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import Range
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

from core.audit import record_event
from core.db import SessionDep
from core.redis import get_redis
from customers.models import Customer
from customers.routes import CustomerIn, create_customer
from notifications.triggers import notify_booking_confirmed
from scheduling import cache
from scheduling._admin_forms import refuse
from scheduling.appointments import (
    _is_slot_taken,
    _load,
    assign_resources,
    customer_suppressed,
    not_offered,
    offered_slot,
    slot_taken,
)
from scheduling.models import Appointment, AppointmentResource, Staff
from scheduling.services import catalog_entry
from scheduling.slots import AvailabilityOut, check_range, resolve_availability, unbookable, utc

router = APIRouter(prefix="/public/booking", tags=["public"])


@router.get("/availability", response_model=AvailabilityOut)
async def public_availability(
    db: SessionDep,
    service_id: uuid.UUID,
    from_: Annotated[Date, Query(alias="from")],
    to: Date,
    staff_id: uuid.UUID | None = None,
):
    """Bookable slots for `service_id`, business-local `from`..`to` inclusive (at most 31
    days) — the same shape `GET /availability` returns. `staff_id` omitted means "any
    eligible staff member", exactly as it does there (`CatalogServiceOut.staff_ids`, the
    engine's own "any available" mode — not reinvented here)."""
    check_range(from_, to)
    service = await catalog_entry(db, service_id)
    if not service.bookable_online:
        raise HTTPException(status_code=404, detail="No such service.")
    return await resolve_availability(db, service, from_, to, staff_id)


# --- booking (Task 2) -----------------------------------------------------------------------


# ponytail: fixed constants for now. m3.md's own Phase 6 Task 4 makes these admin-configurable
# (extending the notification settings panel); nothing here should grow a placeholder setting
# ahead of that task.
_DAILY_CAP_PER_IP = 20
_DAILY_CAP_PER_EMAIL = 5


class PublicBookingIn(BaseModel):
    """The public counterpart to `scheduling.appointments.BookingIn` — deliberately with no
    `override` field at all (module docstring). `customer` reuses `CustomerIn` whole (name
    trimming, lower-cased email, digits-only phone) rather than repeating its validators."""

    service_id: uuid.UUID
    # None is "any available", exactly as it is for staff (`BookingIn.staff_id`).
    staff_id: uuid.UUID | None = None
    starts_at: AwareDatetime
    customer: CustomerIn
    # Honeypot (#10's own acceptance criteria): a field no real client's form shows or lets a
    # person fill in. A scripted submission's autofill often populates anything that looks
    # like an ordinary input; this one must arrive empty, or `book_public` accepts the request
    # with a 200 and does nothing else — the detection itself is never revealed.
    website: Annotated[str, Field(max_length=200)] = ""

    @model_validator(mode="after")
    def _reachable(self):
        if not self.customer.email and not self.customer.phone:
            raise ValueError("send an email or a phone number")
        return self


class PublicBookingOut(BaseModel):
    """What the confirmation screen needs. Task 3 (the booking-management link) extends this
    same shape with a `management_link` field, per that task's own scope note in m3.md — this
    response is otherwise final."""

    appointment_id: str
    starts_at: str
    ends_at: str
    service_name: str
    staff_name: str


def _digest(value: str) -> str:
    return hashlib.sha256(value.strip().lower().encode()).hexdigest()


async def _daily_cap_exceeded(kind: str, value: str, limit: int) -> bool:
    """`incr` + `expire(86400, nx=True)`, keyed on the digest of `value` — `auth/throttle.py`'s
    digest-keying style, a day window rather than its failure window. `nx=True` only sets the
    TTL on this key's first hit of a new day, so a request under the cap never resets somebody
    else's clock (the same reasoning `forms/public.py::throttle`'s minute window already uses
    for its own `expire(60, nx=True)`)."""
    redis = get_redis()
    key = f"public:booking:{kind}:" + _digest(value)
    count = await redis.incr(key)
    await redis.expire(key, 86400, nx=True)
    return count > limit


def _too_many_bookings() -> HTTPException:
    return HTTPException(
        status_code=429,
        detail="Too many booking requests today. Try again tomorrow.",
        headers={"Retry-After": "86400"},
    )


async def _match_existing_customer(db: AsyncSession, identity: CustomerIn) -> Customer | None:
    """An exact match on the already-normalised email or phone — never the booking dialog's
    fuzzy prefix search (`customers/routes.py::find_customers`, built for a human narrowing a
    list, not for deciding whether this is the same person). Matches an erased client too; the
    caller is the one that decides what to do about `suppressed_at`, the same as staff booking
    with an explicit `customer_id` does (`scheduling/appointments.py::customer_suppressed`)."""
    conditions = []
    if identity.email:
        conditions.append(func.lower(Customer.email) == identity.email)
    if identity.phone:
        conditions.append(Customer.phone == identity.phone)
    if not conditions:
        return None
    return await db.scalar(select(Customer).where(or_(*conditions)))


@router.post("", status_code=201, response_model=PublicBookingOut)
async def book_public(payload: PublicBookingIn, request: Request, db: SessionDep):
    """The client-facing booking creation route (module docstring). No capability, no
    session — the request itself is the only thing that has to be trusted, which is exactly
    why the abuse controls run first, before the honeypot's own answer or a cap refusal ever
    touches the database."""
    if payload.website:
        # Tripped: silently accepted, nothing created. The same 200 shape a real booking's
        # caller could not tell apart from success by status code alone.
        return JSONResponse(status_code=200, content={"status": "received"})

    address = request.client.host if request.client else "unknown"
    if await _daily_cap_exceeded("ip", address, _DAILY_CAP_PER_IP):
        raise _too_many_bookings()
    if payload.customer.email and await _daily_cap_exceeded(
        "email", payload.customer.email, _DAILY_CAP_PER_EMAIL
    ):
        raise _too_many_bookings()

    service = await catalog_entry(db, payload.service_id)
    if not service.bookable_online:
        raise HTTPException(status_code=404, detail="No such service.")
    if not service.bookable:
        return unbookable(service)
    eligible = [uuid.UUID(s) for s in service.staff_ids]
    if payload.staff_id is not None and payload.staff_id not in eligible:
        raise refuse("staff_id", "That staff member cannot deliver this service.")
    candidates = [payload.staff_id] if payload.staff_id else eligible

    # No `relax_advisory` reaches this call — `offered_slot`'s default, never overridden here.
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

    customer = await _match_existing_customer(db, payload.customer)
    if customer is not None:
        if customer.suppressed_at is not None:
            return customer_suppressed()
    else:
        # No actor at all (migration 0037; `customers/routes.py::create_customer`'s docstring).
        customer = await create_customer(db, payload.customer, actor_id=None)

    appointment = Appointment(
        customer_id=customer.id,
        staff_id=staff_id,
        service_id=uuid.UUID(service.id),
        starts_at=slot.starts_at,
        ends_at=slot.ends_at,
        # The snapshot, same rule as staff booking (module docstring).
        duration_minutes=service.duration_minutes,
        buffer_before_minutes=service.buffer_before_minutes,
        buffer_after_minutes=service.buffer_after_minutes,
        price_cents=service.price_cents,
        status="confirmed",
        created_by_user_id=None,
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

    record_event(
        db,
        "appointment.booked",
        target_type="appointment",
        target_id=str(appointment.id),
        actor_user_id=None,
        metadata={
            "service_id": service.id,
            "staff_id": str(staff_id),
            "customer_id": str(customer.id),
            "source": "public_booking",
        },
    )
    await db.commit()
    await cache.bump()

    loaded = await _load(db, appointment.id)
    await notify_booking_confirmed(db, loaded)

    return PublicBookingOut(
        appointment_id=str(loaded.id),
        starts_at=utc(loaded.starts_at),
        ends_at=utc(loaded.ends_at),
        service_name=loaded.service.name,
        staff_name=loaded.staff.display_name,
    )
