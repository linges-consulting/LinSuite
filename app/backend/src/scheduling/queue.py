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

No conversion to `in_service`/`done` here — that is Task 5's `POST /queue-entries/{id}/start`,
a call into the same `offered_slot`/`assign_resources` primitives staff and public booking
already use. `cache.bump()` is not called from this file: nothing here changes bookability
(the availability cache only reflects `Appointment`/resource state), the same as a `Customer`
create/update never bumping it either.
"""

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.capabilities import Requires
from auth.models import User
from core.audit import record_event
from core.db import SessionDep
from customers.models import Customer
from notifications.triggers import load_business
from scheduling.appointments import invalid_transition
from scheduling.models import QueueEntry, Service, Staff

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
