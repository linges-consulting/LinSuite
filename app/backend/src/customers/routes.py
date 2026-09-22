"""`/api/customers`: create one, find one, open one, edit one.

Two capabilities, one per verb-group. `customers.view` finds and opens people — the Clients
list, the booking screen's search box, the profile — and `customers.manage` adds and edits
them. The booking endpoint creates a customer inline under its own capability
(`scheduling/appointments.py`) and does it through `create_customer` here, so there is one
place a customer is made and one audit event for it wherever that happens.

**Listing never logs; opening always does** (ADR-0002 §4). The list and the search render
names and a derived `classification` (never PHI, pre-flight D8) — never the profile fields
this module grew in Task 3 — and logging every render would bury the access events in noise.
Opening a profile is `GET /customers/{customer_id}`, which carries `LogAccess` and returns
the profile *and* its appointment history in one response — so a profile open is exactly one
row, not one per panel. The history is read straight off `scheduling.models.Appointment` here
rather than through `scheduling.appointments`, which imports this module for
`create_customer`.

**`PATCH /customers/{customer_id}` is a write, not a read.** It is audited in `audit_events`
by changed field name, never `LogAccess` — the response is the fields that changed, not a
profile-plus-history open, and the edit dialog's own invalidation makes the next `GET` (which
does log) the read of record. See `update_customer` below.
"""

import re
import uuid
from datetime import UTC, date, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, EmailStr, Field, field_validator
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from auth.capabilities import Requires
from auth.models import User
from core.access_log import LogAccess
from core.audit import record_event
from core.db import SessionDep
from core.models import Business
from customers import retention
from customers.classification import Classification, classify
from customers.models import Customer
from scheduling.models import Appointment

router = APIRouter(prefix="/customers", tags=["customers"])

Viewer = Annotated[User, Depends(Requires("customers.view"))]
Manager = Annotated[User, Depends(Requires("customers.manage"))]

# One screenful by default; a hundred at most — this is a list, not an export.
PAGE_SIZE, MAX_PAGE_SIZE = 50, 100

Name = Annotated[str, Field(min_length=1, max_length=100)]
# The unique index migration 0014 creates on `lower(email)`.
_EMAIL_INDEX = "ux_customers_email"


# Shared with `CustomerPatch` below, so a contact phone on an emergency or secondary contact
# is normalised exactly the way the customer's own is.
def _real_name(value: str) -> str:
    if not value.strip():
        raise ValueError("this cannot be blank")
    return value.strip()


def _blank_is_absent(value: object) -> object:
    # A cleared input sends "", and an empty contact detail is no contact detail. A present
    # one is trimmed here rather than per field — email gets lower-cased after, phone loses
    # everything but its digits, and a name or a note simply keeps the trimmed text.
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    return stripped or None


def _lowered(value: str | None) -> str | None:
    return value.lower() if value else None


def _digits(value: str | None) -> str | None:
    digits = re.sub(r"\D", "", value or "")
    if value is not None and not digits:
        raise ValueError("a phone number needs some digits in it")
    return digits or None


class CustomerIn(BaseModel):
    first_name: Name
    last_name: Name
    email: EmailStr | None = None
    phone: Annotated[str | None, Field(max_length=32)] = None

    @field_validator("first_name", "last_name", mode="after")
    @classmethod
    def _names(cls, value: str) -> str:
        return _real_name(value)

    @field_validator("email", "phone", mode="before")
    @classmethod
    def _blanks(cls, value):
        return _blank_is_absent(value)

    @field_validator("email", mode="after")
    @classmethod
    def _email_lower(cls, value: str | None) -> str | None:
        return _lowered(value)

    @field_validator("phone", mode="after")
    @classmethod
    def _phone_digits(cls, value: str | None) -> str | None:
        return _digits(value)


# --- editing the profile (Task 3, #38) ------------------------------------------------------

_CONTACT_TEXT_FIELDS = (
    "emergency_contact_name",
    "emergency_contact_relationship",
    "secondary_contact_name",
    "notes",
)
_CONTACT_PHONE_FIELDS = ("emergency_contact_phone", "secondary_contact_phone")
_MIN_DOB = date(1900, 1, 1)


class CustomerPatch(BaseModel):
    """Every field the edit dialog can touch. Fields left out of the request body are left
    alone — `exclude_unset=True` at the call site is what tells "not sent" from "sent as
    null" — so a partial edit can never blank a field nobody touched.

    The same normalisation `CustomerIn` applies to the customer's own name, email and phone
    applies here to the two contacts: trimmed names, a lower-cased email, digits-only phones.
    """

    first_name: Name | None = None
    last_name: Name | None = None
    email: EmailStr | None = None
    phone: Annotated[str | None, Field(max_length=32)] = None
    date_of_birth: date | None = None
    emergency_contact_name: Annotated[str | None, Field(max_length=100)] = None
    emergency_contact_phone: Annotated[str | None, Field(max_length=32)] = None
    emergency_contact_relationship: Annotated[str | None, Field(max_length=100)] = None
    secondary_contact_name: Annotated[str | None, Field(max_length=100)] = None
    secondary_contact_phone: Annotated[str | None, Field(max_length=32)] = None
    secondary_contact_email: EmailStr | None = None
    notes: Annotated[str | None, Field(max_length=2000)] = None

    @field_validator("first_name", "last_name", mode="after")
    @classmethod
    def _names(cls, value: str | None) -> str | None:
        return _real_name(value) if value is not None else value

    @field_validator(
        "email",
        "phone",
        "secondary_contact_email",
        *_CONTACT_PHONE_FIELDS,
        *_CONTACT_TEXT_FIELDS,
        mode="before",
    )
    @classmethod
    def _blanks(cls, value):
        return _blank_is_absent(value)

    @field_validator("email", "secondary_contact_email", mode="after")
    @classmethod
    def _emails_lower(cls, value: str | None) -> str | None:
        return _lowered(value)

    @field_validator(*_CONTACT_PHONE_FIELDS, "phone", mode="after")
    @classmethod
    def _phones_digits(cls, value: str | None) -> str | None:
        return _digits(value)

    @field_validator("date_of_birth", mode="after")
    @classmethod
    def _dob_sane(cls, value: date | None) -> date | None:
        if value is None:
            return value
        if value > date.today():
            raise ValueError("date of birth cannot be in the future")
        if value < _MIN_DOB:
            raise ValueError("date of birth cannot be before 1900")
        return value


class CustomerOut(BaseModel):
    id: str
    first_name: str
    last_name: str
    email: str | None
    phone: str | None
    created_at: datetime
    # Derived at read time from completed-appointment counts (pre-flight D8) — not PHI, and
    # never a reason on its own to add `LogAccess`. Carried on every row of the list and the
    # booking dialog's search; DOB, contacts and notes never are (those are the profile's).
    classification: Classification


def customer_out(customer: Customer, classification: Classification) -> CustomerOut:
    return CustomerOut(
        id=str(customer.id),
        first_name=customer.first_name,
        last_name=customer.last_name,
        email=customer.email,
        phone=customer.phone,
        created_at=customer.created_at,
        classification=classification,
    )


async def _classifications(
    db: AsyncSession, customer_ids: list[uuid.UUID]
) -> dict[uuid.UUID, Classification]:
    """Every id's classification, in one grouped count query — never one query per row."""
    if not customer_ids:
        return {}
    threshold = await db.scalar(select(Business.vip_visit_threshold).where(Business.id == 1))
    counts = dict(
        (
            await db.execute(
                select(Appointment.customer_id, func.count())
                .where(Appointment.customer_id.in_(customer_ids), Appointment.status == "completed")
                .group_by(Appointment.customer_id)
            )
        ).all()
    )
    return {cid: classify(counts.get(cid, 0), threshold or 10) for cid in customer_ids}


async def create_customer(db: AsyncSession, payload: CustomerIn, actor_id: uuid.UUID) -> Customer:
    """Stage a customer and its audit event in the caller's transaction. Flushes, so the id
    exists and a duplicate email is a 409 here rather than a surprise at the caller's commit;
    the caller's `commit` is what makes both real."""
    customer = Customer(**payload.model_dump())
    db.add(customer)
    try:
        await db.flush()
    except IntegrityError as error:
        await db.rollback()
        if _EMAIL_INDEX not in str(error.orig):
            raise
        raise HTTPException(
            status_code=409, detail="A customer with that email already exists."
        ) from None
    record_event(
        db,
        "customer.created",
        target_type="customer",
        target_id=str(customer.id),
        actor_user_id=actor_id,
    )
    return customer


@router.post("", status_code=201)
async def add_customer(payload: CustomerIn, actor: Manager, db: SessionDep) -> CustomerOut:
    customer = await create_customer(db, payload, actor.id)
    await db.commit()
    # A customer just made has no appointments at all yet — "new" without a query for it.
    return customer_out(customer, "new")


class CustomerListOut(BaseModel):
    customers: list[CustomerOut]
    total: int


@router.get("")
async def find_customers(
    _: Viewer,
    db: SessionDep,
    q: Annotated[str | None, Field(max_length=100)] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = PAGE_SIZE,
) -> CustomerListOut:
    """Alphabetical by last name then first, `id` breaking ties so two customers who share a
    name never land on both sides of a page boundary and never on neither. A page at a time,
    with the total. `q` narrows it by prefix on either name, the email, or the digits of the
    phone — the booking dialog sends only `q` and reads the first page."""
    query = select(Customer)
    term = (q or "").strip().lower()
    if term:
        digits = re.sub(r"\D", "", term)
        matches = [
            func.lower(Customer.first_name).startswith(term, autoescape=True),
            func.lower(Customer.last_name).startswith(term, autoescape=True),
            func.lower(Customer.email).startswith(term, autoescape=True),
        ]
        if digits:
            matches.append(Customer.phone.startswith(digits, autoescape=True))
        query = query.where(or_(*matches))
    total = await db.scalar(select(func.count()).select_from(query.subquery()))
    rows = list(
        await db.scalars(
            query.order_by(
                func.lower(Customer.last_name), func.lower(Customer.first_name), Customer.id
            )
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    )
    classifications = await _classifications(db, [c.id for c in rows])
    return CustomerListOut(
        customers=[customer_out(c, classifications[c.id]) for c in rows], total=total or 0
    )


# --- the profile: the one PHI read this module has --------------------------------------


class VisitOut(BaseModel):
    """One row of a client's history: when, what, with whom, and how it ended."""

    id: str
    starts_at: datetime
    ends_at: datetime
    status: str
    booking_group_id: str | None
    service: dict[str, str]
    staff: dict[str, str]


class CustomerDetailOut(CustomerOut):
    """The full profile: everything the list carries, plus what only the profile shows.

    Never sent by the list or the booking dialog's search — those return `CustomerOut`,
    which stops at `classification`. DOB, both contacts and notes are PHI; this shape only
    ever leaves the server from the one PHI-logged `GET` below, or from the `PATCH` that
    edited it (ADR-0002's neighbour rule for a mutation that echoes what it changed)."""

    date_of_birth: date | None
    emergency_contact_name: str | None
    emergency_contact_phone: str | None
    emergency_contact_relationship: str | None
    secondary_contact_name: str | None
    secondary_contact_phone: str | None
    secondary_contact_email: str | None
    notes: str | None
    updated_at: datetime


def customer_detail_out(customer: Customer, classification: Classification) -> CustomerDetailOut:
    return CustomerDetailOut(
        **customer_out(customer, classification).model_dump(),
        date_of_birth=customer.date_of_birth,
        emergency_contact_name=customer.emergency_contact_name,
        emergency_contact_phone=customer.emergency_contact_phone,
        emergency_contact_relationship=customer.emergency_contact_relationship,
        secondary_contact_name=customer.secondary_contact_name,
        secondary_contact_phone=customer.secondary_contact_phone,
        secondary_contact_email=customer.secondary_contact_email,
        notes=customer.notes,
        updated_at=customer.updated_at,
    )


class CustomerProfileOut(BaseModel):
    customer: CustomerDetailOut
    # The business's zone, so a screen can print the day each visit was on.
    timezone: str
    # Newest first, upcoming included, cancelled and no-shows too: this is the history.
    appointments: list[VisitOut]


@router.get(
    "/{customer_id}",
    dependencies=[Depends(Requires("customers.view")), Depends(LogAccess("customer_profile"))],
)
async def read_customer(customer_id: uuid.UUID, db: SessionDep) -> CustomerProfileOut:
    """The profile and its visits together, so opening one is one access-log row. `Requires`
    is declared before `LogAccess` on purpose: a refusal is not an access."""
    customer = await db.get(Customer, customer_id)
    if customer is None:
        # The access log already holds the attempt — see `core/access_log.py`.
        raise HTTPException(status_code=404, detail="No such customer.")
    visits = list(
        await db.scalars(
            select(Appointment)
            .where(Appointment.customer_id == customer_id)
            .order_by(Appointment.starts_at.desc())
        )
    )
    business = await db.get(Business, 1)
    completed = sum(1 for a in visits if a.status == "completed")
    classification = classify(completed, (business.vip_visit_threshold if business else None) or 10)
    return CustomerProfileOut(
        customer=customer_detail_out(customer, classification),
        timezone=(business.timezone if business else None) or "UTC",
        appointments=[
            VisitOut(
                id=str(a.id),
                starts_at=a.starts_at,
                ends_at=a.ends_at,
                status=a.status,
                booking_group_id=str(a.booking_group_id) if a.booking_group_id else None,
                service={"id": str(a.service.id), "name": a.service.name},
                staff={
                    "id": str(a.staff.id),
                    "display_name": a.staff.display_name,
                    "colour": a.staff.colour,
                },
            )
            for a in visits
        ],
    )


@router.patch("/{customer_id}")
async def update_customer(
    customer_id: uuid.UUID, payload: CustomerPatch, actor: Manager, db: SessionDep
) -> CustomerDetailOut:
    """Edits whatever profile fields were actually sent (`exclude_unset`), never PHI-logged
    in its own right — ADR-0002 logs record *opens*; this is a write, audited in
    `audit_events` by field name only, never values. Not `LogAccess`: the response is the
    fields that changed, not the profile-plus-history a `GET` returns, and the frontend's
    invalidation makes the next open — which does log — the read of record.
    """
    customer = await db.get(Customer, customer_id)
    if customer is None:
        raise HTTPException(status_code=404, detail="No such customer.")

    sent = payload.model_dump(exclude_unset=True)
    changed = [field for field, value in sent.items() if getattr(customer, field) != value]
    for field in changed:
        setattr(customer, field, sent[field])

    if changed:
        customer.updated_at = datetime.now(UTC)
        try:
            await db.flush()
        except IntegrityError as error:
            await db.rollback()
            if _EMAIL_INDEX not in str(error.orig):
                raise
            raise HTTPException(
                status_code=409, detail="A customer with that email already exists."
            ) from None
        if "date_of_birth" in changed:
            await retention.on_dob_changed(db, customer)
        record_event(
            db,
            "customer.updated",
            target_type="customer",
            target_id=str(customer.id),
            actor_user_id=actor.id,
            metadata={"changed": changed},
        )
    await db.commit()

    completed = await db.scalar(
        select(func.count()).where(
            Appointment.customer_id == customer_id, Appointment.status == "completed"
        )
    )
    threshold = await db.scalar(select(Business.vip_visit_threshold).where(Business.id == 1))
    return customer_detail_out(customer, classify(completed or 0, threshold or 10))
