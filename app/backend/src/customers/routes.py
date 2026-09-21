"""`/api/customers`: create one, find one, open one.

Two capabilities, one per verb. `customers.view` finds and opens people — the Clients list,
the booking screen's search box, the profile — and `customers.manage` adds them. The booking
endpoint creates a customer inline under its own capability (`scheduling/appointments.py`)
and does it through `create_customer` here, so there is one place a customer is made and one
audit event for it wherever that happens.

**Listing never logs; opening always does** (ADR-0002 §4). The list and the search render
names, and logging every render would bury the access events in noise. Opening a profile is
`GET /customers/{customer_id}`, which carries `LogAccess` and returns the profile *and* its
appointment history in one response — so a profile open is exactly one row, not one per
panel. The history is read straight off `scheduling.models.Appointment` here rather than
through `scheduling.appointments`, which imports this module for `create_customer`.
"""

import re
import uuid
from datetime import datetime
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


class CustomerIn(BaseModel):
    first_name: Name
    last_name: Name
    email: EmailStr | None = None
    phone: Annotated[str | None, Field(max_length=32)] = None

    @field_validator("first_name", "last_name", mode="after")
    @classmethod
    def _real_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("this cannot be blank")
        return value.strip()

    @field_validator("email", "phone", mode="before")
    @classmethod
    def _blank_is_absent(cls, value):
        # A cleared input sends "", and an empty contact detail is no contact detail.
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("email", mode="after")
    @classmethod
    def _lowered(cls, value: str | None) -> str | None:
        return value.lower() if value else None

    @field_validator("phone", mode="after")
    @classmethod
    def _digits(cls, value: str | None) -> str | None:
        digits = re.sub(r"\D", "", value or "")
        if value is not None and not digits:
            raise ValueError("a phone number needs some digits in it")
        return digits or None


class CustomerOut(BaseModel):
    id: str
    first_name: str
    last_name: str
    email: str | None
    phone: str | None
    created_at: datetime


def customer_out(customer: Customer) -> CustomerOut:
    return CustomerOut(
        id=str(customer.id),
        first_name=customer.first_name,
        last_name=customer.last_name,
        email=customer.email,
        phone=customer.phone,
        created_at=customer.created_at,
    )


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
    return customer_out(customer)


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
    rows = await db.scalars(
        query.order_by(func.lower(Customer.last_name), func.lower(Customer.first_name), Customer.id)
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return CustomerListOut(customers=[customer_out(c) for c in rows], total=total or 0)


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


class CustomerProfileOut(BaseModel):
    customer: CustomerOut
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
    visits = await db.scalars(
        select(Appointment)
        .where(Appointment.customer_id == customer_id)
        .order_by(Appointment.starts_at.desc())
    )
    timezone = await db.scalar(select(Business.timezone).where(Business.id == 1))
    return CustomerProfileOut(
        customer=customer_out(customer),
        timezone=timezone or "UTC",
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
