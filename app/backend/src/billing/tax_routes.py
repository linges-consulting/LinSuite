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
from datetime import UTC, datetime
from datetime import date as Date
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from auth.capabilities import Requires
from auth.models import User
from billing.bill_review import business_or_404
from billing.models import MAX_TAX_RATE_PPM, TaxComponent, TaxComponentRate
from billing.tax import resolve_rate_ppm
from billing.tax_table import find_component, rate_as_of
from core.audit import record_event
from core.db import SessionDep
from core.forms import blank_to_none
from core.models import PROVINCE_CODES
from scheduling.clock import today_in

BillingManager = Annotated[User, Depends(Requires("billing.manage"))]

TaxConvention = Literal["inclusive", "exclusive"]


async def normalize_tax_component_keys(db: AsyncSession, keys: list[str]) -> list[str]:
    """A catalog item's component toggles (review R1), upper-cased, de-duplicated and sorted;
    every code must name an existing component (422 otherwise). Called by the service,
    package-definition and product-variant admin routes."""
    codes = sorted({key.strip().upper() for key in keys if key.strip()})
    if codes:
        known = set(await db.scalars(select(TaxComponent.code).where(TaxComponent.code.in_(codes))))
        unknown = [code for code in codes if code not in known]
        if unknown:
            raise HTTPException(
                status_code=422, detail=f"No such tax component: {', '.join(unknown)}."
            )
    return codes


router = APIRouter(
    prefix="/admin/billing", tags=["billing"], dependencies=[Depends(Requires("billing.manage"))]
)

RatePpm = Annotated[int, Field(ge=0, le=MAX_TAX_RATE_PPM)]
Code = Annotated[str, Field(min_length=1, max_length=16)]
Name = Annotated[str, Field(min_length=1, max_length=100)]


class TaxRateOut(BaseModel):
    id: uuid.UUID
    rate_ppm: int
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
    current_rate_ppm: int | None
    # What acceptance criterion 1 asks the panel to show: whether this business's own
    # province would pick this component up. A hint only — see the module docstring.
    applicable_to_business: bool
    # `'manual'` or `'prefill'` (#118) — `billing/models.py::TaxComponent.origin`'s own
    # docstring. Read-only here: pre-fill is the only writer of `'prefill'`, and it only ever
    # runs once.
    origin: str


class TaxComponentCreate(BaseModel):
    """A component is created with its first rate in one request — a component with no
    price is not usable yet, and splitting that into two calls would let one exist briefly
    that no line could ever be taxed against."""

    code: Code
    name: Name
    province: Annotated[str | None, Field(max_length=2)] = None
    rate_ppm: RatePpm
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
        return blank_to_none(value)

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
        return blank_to_none(value)

    @field_validator("province", mode="before")
    @classmethod
    def _blank(cls, value: str | None) -> str | None:
        return blank_to_none(value)


class TaxRateCreate(BaseModel):
    rate_ppm: RatePpm
    effective_from: Date


def _duplicate_code(code: str) -> HTTPException:
    return HTTPException(status_code=409, detail=f"A tax component coded “{code}” already exists.")


_CODE_INDEX = "uq_tax_components_code"


def _is_duplicate_code(error: IntegrityError) -> bool:
    for candidate in (error.orig, getattr(error.orig, "__cause__", None)):
        if getattr(candidate, "constraint_name", None) == _CODE_INDEX:
            return True
    return _CODE_INDEX in str(error.orig)


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
    history = [(r.effective_from, r.effective_to, r.rate_ppm) for r in rates]
    return TaxComponentOut(
        id=component.id,
        code=component.code,
        name=component.name,
        province=component.province,
        active=component.active,
        rates=[
            TaxRateOut(
                id=r.id,
                rate_ppm=r.rate_ppm,
                effective_from=r.effective_from,
                effective_to=r.effective_to,
            )
            for r in rates
        ],
        current_rate_ppm=resolve_rate_ppm(history, today),
        applicable_to_business=component.province is None
        or component.province == business_province,
        origin=component.origin,
    )


@router.get("/tax-components")
async def list_tax_components(
    _: BillingManager, db: SessionDep
) -> dict[str, list[TaxComponentOut]]:
    business = await business_or_404(db)
    today = today_in(business.timezone)
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
    business = await business_or_404(db)
    component = TaxComponent(
        code=payload.code, name=payload.name, province=payload.province, active=True
    )
    component.rates = [
        TaxComponentRate(rate_ppm=payload.rate_ppm, effective_from=payload.effective_from)
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
        metadata={"code": component.code, "rate_ppm": payload.rate_ppm},
    )
    await db.commit()
    await db.refresh(component, attribute_names=["rates"])
    return _out(component, today=today_in(business.timezone), business_province=business.province)


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
    business = await business_or_404(db)
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
    return _out(component, today=today_in(business.timezone), business_province=business.province)


@router.post("/tax-components/{component_id}/rates", status_code=201)
async def add_tax_component_rate(
    component_id: uuid.UUID, payload: TaxRateCreate, admin: BillingManager, db: SessionDep
) -> TaxComponentOut:
    business = await business_or_404(db)
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
            rate_ppm=payload.rate_ppm,
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
            "rate_ppm": payload.rate_ppm,
            "effective_from": payload.effective_from.isoformat(),
        },
    )
    await db.commit()
    await db.refresh(component, attribute_names=["rates"])
    return _out(component, today=today_in(business.timezone), business_province=business.province)


# --- pre-fill status and confirmation (#118, spec #113 "Tax pre-fill") ---------------------
#
# Everything the Tax panel needs to decide between three states — nothing to confirm yet (no
# pre-filled components), "Looks right" pending, and confirmed — plus the two conditions spec
# #113 says get a prompt instead of a silent change: the business's own province moved since
# pre-fill ran, or `billing/tax_table.py` now lists a different current rate than what pre-fill
# gave a component. Both compare only `origin == "prefill"` rows against the table — an owner's
# own manually-added component in a second province (`test_billing_tax_settings.py`'s `ab_lct`)
# is not pre-fill going stale, so it is never part of this comparison.


class TaxStatusOut(BaseModel):
    confirmed_at: datetime | None
    # Whether there is anything to confirm at all — the panel's "Looks right" only ever shows
    # when this is true and `confirmed_at` is not.
    prefilled: bool
    province_changed: bool
    newer_rate_available: bool


def _component_up_to_date(component: TaxComponent, as_of: Date) -> bool:
    """A pre-filled component is current if the table still lists the exact rate it was given
    — same `rate_ppm`, same `effective_from` — among its own rate rows. A component whose
    `province` the table no longer recognises (should not happen with today's thirteen, but a
    future table edit could narrow one) counts as up to date: there is nothing newer to compare
    against, only a component the table no longer covers, which is a different conversation."""
    if component.province is None:
        return True
    table_component = find_component(component.code, component.province)
    if table_component is None:
        return True
    table_rate = rate_as_of(table_component, as_of)
    if table_rate is None:
        return True
    return any(
        r.effective_from == table_rate.effective_from and r.rate_ppm == table_rate.rate_ppm
        for r in component.rates
    )


async def _tax_status(db: SessionDep, business) -> TaxStatusOut:
    prefilled = list(
        await db.scalars(
            select(TaxComponent)
            .options(selectinload(TaxComponent.rates))
            .where(TaxComponent.origin == "prefill")
        )
    )
    today = today_in(business.timezone)
    return TaxStatusOut(
        confirmed_at=business.tax_confirmed_at,
        prefilled=bool(prefilled),
        province_changed=any(c.province != business.province for c in prefilled),
        newer_rate_available=any(not _component_up_to_date(c, today) for c in prefilled),
    )


@router.get("/tax-status")
async def read_tax_status(_: BillingManager, db: SessionDep) -> TaxStatusOut:
    business = await business_or_404(db)
    return await _tax_status(db, business)


@router.post("/tax-confirmation")
async def confirm_tax(admin: BillingManager, db: SessionDep) -> TaxStatusOut:
    """The owner's "Looks right" on the checklist's Tax step. Sets the timestamp
    unconditionally — re-confirming after editing a rate is allowed and harmless, the same way
    `settings/routes.py::update_security`'s retention save can be re-run."""
    business = await business_or_404(db)
    business.tax_confirmed_at = datetime.now(UTC)
    record_event(
        db,
        "billing.tax_confirmed",
        target_type="business",
        target_id=str(business.id),
        actor_user_id=admin.id,
    )
    await db.commit()
    return await _tax_status(db, business)
