"""Settings → Billing: discount definitions (#58).

Follows the shape `scheduling/services.py` already established for a small admin CRUD
surface: one router at `/admin/discounts`, gated by `billing.manage` (administrative, so an
Admin Mode window is required on top of the capability). Eligibility is its own endpoint,
replacing the whole set in one transaction — the same argument `replace_eligible_staff` makes
for a service's staff list: a half-applied eligibility set is a discount briefly scoped to
nothing, or to everything, that nobody asked for.

No application logic here at all — nothing in this file ever reads an appointment or a bill.
`billing/discount_resolver.py`'s pure functions are what #63 will call once a real bill exists
to resolve discounts against; this router only lets an administrator define and toggle them.
"""

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from auth.capabilities import Requires
from auth.models import User
from billing.models import Discount, DiscountEligibleItem
from core.audit import record_event
from core.db import SessionDep

router = APIRouter(prefix="/admin/discounts", tags=["billing"])

BillingManager = Annotated[User, Depends(Requires("billing.manage"))]

Kind = Literal["percentage", "fixed"]
EligibilityScope = Literal["all", "selected"]
CommissionBasis = Literal["reduces", "absorbed"]
ItemType = Literal["service", "product", "package"]


# --- what goes over the wire ----------------------------------------------------------------


class EligibleItemOut(BaseModel):
    item_type: ItemType
    item_id: str


class DiscountOut(BaseModel):
    id: str
    name: str
    kind: Kind
    percentage_bp: int | None
    amount_cents: int | None
    eligibility_scope: EligibilityScope
    stackable: bool
    commission_basis: CommissionBasis
    enabled: bool
    eligible_items: list[EligibleItemOut]


def _out(discount: Discount) -> DiscountOut:
    return DiscountOut(
        id=str(discount.id),
        name=discount.name,
        kind=discount.kind,
        percentage_bp=discount.percentage_bp,
        amount_cents=discount.amount_cents,
        eligibility_scope=discount.eligibility_scope,
        stackable=discount.stackable,
        commission_basis=discount.commission_basis,
        enabled=discount.enabled,
        eligible_items=sorted(
            (
                EligibleItemOut(item_type=i.item_type, item_id=str(i.item_id))
                for i in discount.eligible_items
            ),
            key=lambda i: (i.item_type, i.item_id),
        ),
    )


# --- what comes in ---------------------------------------------------------------------------

Name = Annotated[str, Field(min_length=1, max_length=200)]
PercentageBp = Annotated[int, Field(ge=0, le=10_000)]
AmountCents = Annotated[int, Field(ge=0)]


class DiscountFields(BaseModel):
    """Everything an administrator sets, on create and on edit alike. Eligibility itself is
    its own endpoint, below, the same reasoning `RequirementsIn` gets in `services.py`."""

    name: Name
    kind: Kind
    percentage_bp: PercentageBp | None = None
    amount_cents: AmountCents | None = None
    eligibility_scope: EligibilityScope = "all"
    stackable: bool = False
    commission_basis: CommissionBasis

    @model_validator(mode="after")
    def _amount_matches_kind(self) -> "DiscountFields":
        # Mirrors `ck_discounts_amount_matches_kind` so a bad pairing is a 422 with a field
        # name, not a 500 off the database's own CHECK.
        if self.kind == "percentage":
            if self.percentage_bp is None:
                raise ValueError("a percentage discount needs percentage_bp")
            if self.amount_cents is not None:
                raise ValueError("a percentage discount cannot also carry amount_cents")
        else:
            if self.amount_cents is None:
                raise ValueError("a fixed discount needs amount_cents")
            if self.percentage_bp is not None:
                raise ValueError("a fixed discount cannot also carry percentage_bp")
        return self


class DiscountPatch(BaseModel):
    """Every field optional, so "leave it alone" and "set it" can be told apart. `kind` is not
    patchable — switching a discount from percentage to fixed (or back) is a new definition in
    everything but name, so it goes through delete-and-recreate at the screen, not a PATCH that
    would otherwise have to re-validate the whole amount/kind pairing against half-old, half-
    new data."""

    name: Name | None = None
    percentage_bp: PercentageBp | None = None
    amount_cents: AmountCents | None = None
    eligibility_scope: EligibilityScope | None = None
    stackable: bool | None = None
    commission_basis: CommissionBasis | None = None


class EligibleItemIn(BaseModel):
    item_type: ItemType
    item_id: uuid.UUID


class EligibleItemsIn(BaseModel):
    items: list[EligibleItemIn] = []


# --- reading -----------------------------------------------------------------------------------


async def _roster(db: SessionDep, *, include_disabled: bool) -> list[Discount]:
    rows = await db.scalars(
        select(Discount)
        .where(*([] if include_disabled else [Discount.enabled]))
        .order_by(Discount.enabled.desc(), Discount.name)
    )
    return list(rows)


@router.get("")
async def list_discounts(
    _: BillingManager, db: SessionDep, include_disabled: bool = False
) -> dict[str, list[DiscountOut]]:
    return {"discounts": [_out(d) for d in await _roster(db, include_disabled=include_disabled)]}


async def _load(db: SessionDep, discount_id: uuid.UUID) -> Discount:
    discount = await db.scalar(
        select(Discount).where(Discount.id == discount_id).execution_options(populate_existing=True)
    )
    if discount is None:
        raise HTTPException(status_code=404, detail="No such discount.")
    return discount


# --- creating ------------------------------------------------------------------------------


@router.post("", status_code=201)
async def create_discount(
    payload: DiscountFields, admin: BillingManager, db: SessionDep
) -> DiscountOut:
    discount = Discount(**payload.model_dump(), enabled=True)
    db.add(discount)
    await db.flush()

    record_event(
        db,
        "discount.created",
        target_type="discount",
        target_id=str(discount.id),
        actor_user_id=admin.id,
        metadata={"name": discount.name, "kind": discount.kind},
    )
    await db.commit()
    return _out(await _load(db, discount.id))


# --- editing ------------------------------------------------------------------------------


@router.patch("/{discount_id}")
async def update_discount(
    discount_id: uuid.UUID, payload: DiscountPatch, admin: BillingManager, db: SessionDep
) -> DiscountOut:
    discount = await _load(db, discount_id)
    sent = payload.model_dump(exclude_unset=True)

    changed = []
    for field_name, value in sent.items():
        if getattr(discount, field_name) != value:
            setattr(discount, field_name, value)
            changed.append(field_name)

    if changed:
        try:
            await db.flush()
        except IntegrityError as error:
            await db.rollback()
            if "ck_discounts_amount_matches_kind" not in str(error.orig):
                raise
            raise HTTPException(
                status_code=422,
                detail="That percentage/amount no longer matches this discount's kind.",
            ) from None
        record_event(
            db,
            "discount.updated",
            target_type="discount",
            target_id=str(discount.id),
            actor_user_id=admin.id,
            metadata={"changed": sorted(changed)},
        )
    await db.commit()
    return _out(await _load(db, discount_id))


@router.put("/{discount_id}/eligibility")
async def replace_eligibility(
    discount_id: uuid.UUID, payload: EligibleItemsIn, admin: BillingManager, db: SessionDep
) -> DiscountOut:
    """The whole set, in one transaction — meaningful only when `eligibility_scope ==
    "selected"`; stored but unread while the discount is scoped to `"all"`, the same as a
    service's requirements are stored and ignored for a kind nothing needs them for."""
    discount = await _load(db, discount_id)
    wanted = list(dict.fromkeys((i.item_type, i.item_id) for i in payload.items))

    await db.execute(
        delete(DiscountEligibleItem).where(DiscountEligibleItem.discount_id == discount_id)
    )
    db.add_all(
        [
            DiscountEligibleItem(discount_id=discount_id, item_type=item_type, item_id=item_id)
            for item_type, item_id in wanted
        ]
    )
    await db.flush()

    record_event(
        db,
        "discount.eligibility_replaced",
        target_type="discount",
        target_id=str(discount.id),
        actor_user_id=admin.id,
        metadata={"items": len(wanted)},
    )
    await db.commit()
    return _out(await _load(db, discount_id))


# --- enabling, and disabling -----------------------------------------------------------------


@router.post("/{discount_id}/enable")
async def enable_discount(
    discount_id: uuid.UUID, admin: BillingManager, db: SessionDep
) -> DiscountOut:
    return await _set_enabled(db, discount_id, admin, enabled=True)


@router.post("/{discount_id}/disable")
async def disable_discount(
    discount_id: uuid.UUID, admin: BillingManager, db: SessionDep
) -> DiscountOut:
    return await _set_enabled(db, discount_id, admin, enabled=False)


async def _set_enabled(
    db: SessionDep, discount_id: uuid.UUID, admin: User, *, enabled: bool
) -> DiscountOut:
    """No hard delete, ever: once #63/#65 exist, an issued invoice line must be able to keep
    naming the discount that applied to it. `enabled` going false is the only way a discount
    stops being offered."""
    discount = await _load(db, discount_id)
    if discount.enabled == enabled:
        return _out(discount)

    discount.enabled = enabled
    record_event(
        db,
        "discount.enabled" if enabled else "discount.disabled",
        target_type="discount",
        target_id=str(discount.id),
        actor_user_id=admin.id,
        metadata={"name": discount.name},
    )
    await db.commit()
    return _out(await _load(db, discount_id))
