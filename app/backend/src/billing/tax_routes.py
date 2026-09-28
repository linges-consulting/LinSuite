"""Settings → Billing → Tax: the admin CRUD over `billing/models.py`'s tax components and
their effective-dated rates (#57).

Same shape as every other small admin CRUD panel in this codebase (module docstring
convention noted in `.superpowers/sdd/m4.md`: diff/`record_event`/commit on the backend,
fetch/mutate/toast on the frontend) — nothing invented here that `scheduling/services.py` or
`settings/notifications_routes.py` did not already establish.

**A rate is never edited in place.** `POST .../rates` always *adds* a row; it never accepts an
id to update, because the whole point of `billing/models.py::TaxComponentRate` is that a past
rate stays exactly what it was when #65 later snapshots it onto an issued invoice. Opening a
new rate closes whichever one was still open (`effective_to IS NULL`) at the new rate's
`effective_from` — one request, so a component can never be left with two open rates, which
the database's own `EXCLUDE` constraint would refuse anyway, just as a 500 instead of a
sentence.

**No selection logic here.** Which of a business's components actually apply to one catalog
item, and at what date, is #63's job (bill review) built on top of `billing/tax.py`'s pure
functions — this file only lets an administrator define and price the components that exist.
`applicable_to_business` on the list response is the one exception: a read-only hint (not an
enforcement) telling the panel which components this business's own province would pick, per
acceptance criterion 1's "selected from the business's jurisdiction, not hardcoded."
"""

import uuid
from datetime import date as Date
from datetime import datetime
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from auth.capabilities import Requires
from auth.models import User
from billing.models import MAX_TAX_RATE_BP, TaxComponent, TaxComponentRate
from billing.tax import resolve_rate_bp
from core.audit import record_event
from core.db import SessionDep
from core.models import PROVINCE_CODES, Business

BillingManager = Annotated[User, Depends(Requires("billing.manage"))]

router = APIRouter(
    prefix="/admin/billing", tags=["billing"], dependencies=[Depends(Requires("billing.manage"))]
)

RateBp = Annotated[int, Field(ge=0, le=MAX_TAX_RATE_BP)]
Code = Annotated[str, Field(min_length=1, max_length=16)]
Name = Annotated[str, Field(min_length=1, max_length=100)]


def _blank_to_none(value: str | None) -> str | None:
    """A cleared text input sends `""`, which is not a shorter province code. Duplicated from
    `scheduling/_admin_forms.py` rather than imported — that module is scheduling-private, per
    its own docstring, and this is two lines."""
    return value.strip() or None if isinstance(value, str) else value


class TaxRateOut(BaseModel):
    id: uuid.UUID
    rate_bp: int
    effective_from: Date
    effective_to: Date | None


class TaxComponentOut(BaseModel):
    id: uuid.UUID
    code: str
    name: str
    province: str | None
    active: bool
    rates: list[TaxRateOut]
    # The rate in effect today, in this business's own timezone (CLAUDE.md "Time": a date
    # boundary is read locally, never off a raw UTC instant). `None` when the component has
    # no rate covering today — a component just created with a future `effective_from`.
    current_rate_bp: int | None
    # What acceptance criterion 1 asks the panel to show: whether this business's own
    # province would pick this component up. A hint only — see the module docstring.
    applicable_to_business: bool


class TaxComponentCreate(BaseModel):
    """A component is created with its first rate in one request — a component with no
    price is not usable yet, and splitting that into two calls would let one exist briefly
    that no line could ever be taxed against."""

    code: Code
    name: Name
    province: Annotated[str | None, Field(max_length=2)] = None
    rate_bp: RateBp
    effective_from: Date

    @field_validator("code", mode="after")
    @classmethod
    def _upper(cls, value: str) -> str:
        value = value.strip().upper()
        if not value:
            raise ValueError("this cannot be blank")
        return value

    @field_validator("name", mode="after")
    @classmethod
    def _trimmed(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("this cannot be blank")
        return value

    @field_validator("province", mode="before")
    @classmethod
    def _blank(cls, value: str | None) -> str | None:
        return _blank_to_none(value)

    @field_validator("province", mode="after")
    @classmethod
    def _known_province(cls, value: str | None) -> str | None:
        if value is not None:
            value = value.upper()
            if value not in PROVINCE_CODES:
                raise ValueError("not one of the thirteen provinces or territories")
        return value


class TaxComponentPatch(BaseModel):
    """`code` is not here: it is the stable key a catalog item and a rate resolution will
    reference (module docstring), never renamed once it exists — the same rule
    `auth/capabilities.py` already holds its keys to."""

    name: Annotated[str | None, Field(min_length=1, max_length=100)] = None
    province: Annotated[str | None, Field(max_length=2)] = None
    active: bool | None = None

    @field_validator("name", mode="after")
    @classmethod
    def _trimmed(cls, value: str | None) -> str | None:
        return _blank_to_none(value)

    @field_validator("province", mode="before")
    @classmethod
    def _blank(cls, value: str | None) -> str | None:
        return _blank_to_none(value)


class TaxRateCreate(BaseModel):
    rate_bp: RateBp
    effective_from: Date


def _duplicate_code(code: str) -> HTTPException:
    return HTTPException(status_code=409, detail=f"A tax component coded “{code}” already exists.")


_CODE_INDEX = "uq_tax_components_code"


def _is_duplicate_code(error: IntegrityError) -> bool:
    for candidate in (error.orig, getattr(error.orig, "__cause__", None)):
        if getattr(candidate, "constraint_name", None) == _CODE_INDEX:
            return True
    return _CODE_INDEX in str(error.orig)


async def _business(db: SessionDep) -> Business:
    business = await db.scalar(select(Business).where(Business.id == 1))
    if business is None:
        raise HTTPException(status_code=404, detail="This instance has not been set up.")
    return business


def _today_in(business: Business) -> Date:
    return datetime.now(ZoneInfo(business.timezone)).date()


async def _load(db: SessionDep, component_id: uuid.UUID) -> TaxComponent:
    component = await db.scalar(
        select(TaxComponent)
        .options(selectinload(TaxComponent.rates))
        .where(TaxComponent.id == component_id)
    )
    if component is None:
        raise HTTPException(status_code=404, detail="No such tax component.")
    return component


def _out(component: TaxComponent, *, today: Date, business_province: str | None) -> TaxComponentOut:
    rates = sorted(component.rates, key=lambda r: r.effective_from)
    history = [(r.effective_from, r.effective_to, r.rate_bp) for r in rates]
    return TaxComponentOut(
        id=component.id,
        code=component.code,
        name=component.name,
        province=component.province,
        active=component.active,
        rates=[
            TaxRateOut(
                id=r.id,
                rate_bp=r.rate_bp,
                effective_from=r.effective_from,
                effective_to=r.effective_to,
            )
            for r in rates
        ],
        current_rate_bp=resolve_rate_bp(history, today),
        applicable_to_business=component.province is None
        or component.province == business_province,
    )


@router.get("/tax-components")
async def list_tax_components(
    _: BillingManager, db: SessionDep
) -> dict[str, list[TaxComponentOut]]:
    business = await _business(db)
    today = _today_in(business)
    components = await db.scalars(
        select(TaxComponent).options(selectinload(TaxComponent.rates)).order_by(TaxComponent.code)
    )
    return {
        "tax_components": [
            _out(c, today=today, business_province=business.province) for c in components
        ]
    }


@router.post("/tax-components", status_code=201)
async def create_tax_component(
    payload: TaxComponentCreate, admin: BillingManager, db: SessionDep
) -> TaxComponentOut:
    business = await _business(db)
    component = TaxComponent(
        code=payload.code, name=payload.name, province=payload.province, active=True
    )
    component.rates = [
        TaxComponentRate(rate_bp=payload.rate_bp, effective_from=payload.effective_from)
    ]
    db.add(component)
    try:
        await db.flush()
    except IntegrityError as error:
        await db.rollback()
        if not _is_duplicate_code(error):
            raise
        raise _duplicate_code(payload.code) from None

    record_event(
        db,
        "billing.tax_component_created",
        target_type="tax_component",
        target_id=str(component.id),
        actor_user_id=admin.id,
        metadata={"code": component.code, "rate_bp": payload.rate_bp},
    )
    await db.commit()
    await db.refresh(component, attribute_names=["rates"])
    return _out(component, today=_today_in(business), business_province=business.province)


@router.patch("/tax-components/{component_id}")
async def update_tax_component(
    component_id: uuid.UUID,
    payload: TaxComponentPatch,
    admin: BillingManager,
    db: SessionDep,
) -> TaxComponentOut:
    """`model_dump(exclude_unset=True)` is what tells a field left out of the request body
    apart from one explicitly sent `null` — the ordinary PATCH shape, and simpler than the
    sentinel-string trick `settings/notifications_routes.py` needs for `email_sender`: that
    endpoint reads its payload with `is not None` instead, which is what makes "omitted" and
    "sent null" collide there and force a sentinel. Sending `province: null` here really does
    clear it to federal; leaving the key out really does leave it alone."""
    business = await _business(db)
    component = await _load(db, component_id)

    sent = payload.model_dump(exclude_unset=True)
    changed: list[str] = []
    if "name" in sent and payload.name is not None and payload.name != component.name:
        component.name = payload.name
        changed.append("name")
    if "province" in sent:
        province = payload.province
        if province is not None and province not in PROVINCE_CODES:
            raise HTTPException(
                status_code=422, detail="Not one of the thirteen provinces or territories."
            )
        if province != component.province:
            component.province = province
            changed.append("province")
    if payload.active is not None and payload.active != component.active:
        component.active = payload.active
        changed.append("active")

    if changed:
        record_event(
            db,
            "billing.tax_component_updated",
            target_type="tax_component",
            target_id=str(component.id),
            actor_user_id=admin.id,
            metadata={"changed": changed},
        )
    await db.commit()
    await db.refresh(component, attribute_names=["rates"])
    return _out(component, today=_today_in(business), business_province=business.province)


@router.post("/tax-components/{component_id}/rates", status_code=201)
async def add_tax_component_rate(
    component_id: uuid.UUID, payload: TaxRateCreate, admin: BillingManager, db: SessionDep
) -> TaxComponentOut:
    business = await _business(db)
    component = await _load(db, component_id)

    open_rate = next((r for r in component.rates if r.effective_to is None), None)
    if open_rate is not None:
        if payload.effective_from <= open_rate.effective_from:
            raise HTTPException(
                status_code=422,
                detail=(
                    "The new rate must take effect after the current one started "
                    f"({open_rate.effective_from.isoformat()})."
                ),
            )
        open_rate.effective_to = payload.effective_from
        # Flushed on its own, before the new row exists: the exclusion constraint checks
        # each statement as it runs (it is not deferred), so closing the old range first is
        # what keeps an INSERT that lands before this UPDATE from ever seeing two open,
        # still-overlapping ranges for the same component in between.
        await db.flush()

    db.add(
        TaxComponentRate(
            component_id=component.id,
            rate_bp=payload.rate_bp,
            effective_from=payload.effective_from,
        )
    )
    await db.flush()

    record_event(
        db,
        "billing.tax_component_rate_added",
        target_type="tax_component",
        target_id=str(component.id),
        actor_user_id=admin.id,
        metadata={
            "rate_bp": payload.rate_bp,
            "effective_from": payload.effective_from.isoformat(),
        },
    )
    await db.commit()
    await db.refresh(component, attribute_names=["rates"])
    return _out(component, today=_today_in(business), business_province=business.province)
