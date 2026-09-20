"""`/api/customers`: create one, find one.

Two capabilities, one per verb. `customers.view` finds people — the booking screen's search
box — and `customers.manage` adds them. The booking endpoint creates a customer inline under
its own capability (`scheduling/appointments.py`) and does it through `create_customer` here,
so there is one place a customer is made and one audit event for it wherever that happens.
"""

import re
import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr, Field, field_validator
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from auth.capabilities import Requires
from auth.models import User
from core.audit import record_event
from core.db import SessionDep
from customers.models import Customer

router = APIRouter(prefix="/customers", tags=["customers"])

Viewer = Annotated[User, Depends(Requires("customers.view"))]
Manager = Annotated[User, Depends(Requires("customers.manage"))]

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


@router.get("")
async def find_customers(
    _: Viewer, db: SessionDep, q: Annotated[str | None, Field(max_length=100)] = None
) -> dict[str, list[CustomerOut]]:
    """Prefix matches on either name, the email, or the digits of the phone. Twenty at most:
    this is a picker's search box, not a report."""
    query = select(Customer).limit(20)
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
        query = query.where(or_(*matches)).order_by(Customer.last_name, Customer.first_name)
    else:
        query = query.order_by(Customer.created_at.desc())
    return {"customers": [customer_out(c) for c in await db.scalars(query)]}
