"""Settings → Products: receiving a delivery and correcting a stock count (#61), and the
low-stock email alert that a crossing arms in the same transaction (#62).

Two routes, two capabilities — `inventory.receive` and `inventory.adjust` — checked
independently rather than folded into `catalog.manage`: an owner may trust a lead to receive
deliveries without also trusting them to overwrite a count, and the ticket's own acceptance
criteria ask for both to be server-checked and independently grantable. Both default to
admin/owner only (`requires_admin_mode=True`, `auth/capabilities.py`).

Every write here goes through `inventory.stock::record_movement`, which is where the atomic
non-negative guard actually lives (CLAUDE.md "Concurrency") — this module only turns its
outcome into the right HTTP status and appends the matching `record_event`.

**Low-stock alert, queued after commit.** `record_movement` evaluates the crossing inside its
own transaction and returns whether *this* call is the one that just armed it
(`MovementResult.low_stock_alert_armed`); each route commits first, exactly as `scheduling/
public.py::book_public` commits before calling `notify_booking_confirmed`, and only then calls
`notifications/triggers.py::notify_low_stock` — a task queued before the commit could race a
rollback (CLAUDE.md: Celery workers, never inline; #62's own "queued after commit"
requirement).
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from auth.capabilities import Requires
from auth.models import User
from core.audit import record_event
from core.db import SessionDep
from inventory.routes import ProductOut, _load_product, _load_variant, _product_out
from inventory.stock import InsufficientStock, VariantNotFound, record_movement
from notifications.triggers import notify_low_stock

router = APIRouter(prefix="/admin/products", tags=["inventory"])

InventoryReceiver = Annotated[User, Depends(Requires("inventory.receive"))]
InventoryAdjuster = Annotated[User, Depends(Requires("inventory.adjust"))]

Reason = Annotated[str | None, Field(max_length=500)]


class ReceiveBody(BaseModel):
    """A delivery arriving: always adds stock, a reason is a courtesy note, not required."""

    quantity: Annotated[int, Field(ge=1, le=1_000_000)]
    reason: Reason = None

    @field_validator("reason", mode="after")
    @classmethod
    def _trim(cls, value: str | None) -> str | None:
        return value.strip() or None if isinstance(value, str) else value


class AdjustBody(BaseModel):
    """A manual count correction: signed, either direction, reason required (#61's own
    acceptance criterion — the database's `ck_stock_movements_adjustment_reason` enforces it
    too, but a blank-after-trim string should 422, not reach that CHECK as a false pass)."""

    quantity_delta: Annotated[int, Field(ge=-1_000_000, le=1_000_000)]
    reason: Annotated[str, Field(min_length=1, max_length=500)]

    @field_validator("quantity_delta", mode="after")
    @classmethod
    def _nonzero(cls, value: int) -> int:
        if value == 0:
            raise ValueError("this cannot be zero — nothing would change")
        return value

    @field_validator("reason", mode="after")
    @classmethod
    def _real_reason(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("this cannot be blank")
        return value


@router.post("/{product_id}/variants/{variant_id}/receive")
async def receive_stock(
    product_id: uuid.UUID,
    variant_id: uuid.UUID,
    payload: ReceiveBody,
    admin: InventoryReceiver,
    db: SessionDep,
) -> ProductOut:
    await _load_variant(db, product_id, variant_id)  # 404 before anything is written

    try:
        movement = await record_movement(
            db,
            variant_id=variant_id,
            kind="receipt",
            quantity_delta=payload.quantity,
            actor_user_id=admin.id,
            reason=payload.reason,
        )
    except VariantNotFound:
        raise HTTPException(status_code=404, detail="No such variant.") from None

    record_event(
        db,
        "stock_movement.received",
        target_type="product_variant",
        target_id=str(variant_id),
        actor_user_id=admin.id,
        metadata={"quantity": payload.quantity},
    )
    await db.commit()
    if movement.low_stock_alert_armed:
        await notify_low_stock(db, variant_id)
    return _product_out(await _load_product(db, product_id))


@router.post("/{product_id}/variants/{variant_id}/adjust")
async def adjust_stock(
    product_id: uuid.UUID,
    variant_id: uuid.UUID,
    payload: AdjustBody,
    admin: InventoryAdjuster,
    db: SessionDep,
) -> ProductOut:
    await _load_variant(db, product_id, variant_id)  # 404 before anything is written

    try:
        movement = await record_movement(
            db,
            variant_id=variant_id,
            kind="adjustment",
            quantity_delta=payload.quantity_delta,
            actor_user_id=admin.id,
            reason=payload.reason,
        )
    except InsufficientStock:
        raise HTTPException(
            status_code=409, detail="This would take stock on hand below zero."
        ) from None
    except VariantNotFound:
        raise HTTPException(status_code=404, detail="No such variant.") from None

    record_event(
        db,
        "stock_movement.adjusted",
        target_type="product_variant",
        target_id=str(variant_id),
        actor_user_id=admin.id,
        metadata={"quantity_delta": payload.quantity_delta, "reason": payload.reason},
    )
    await db.commit()
    if movement.low_stock_alert_armed:
        await notify_low_stock(db, variant_id)
    return _product_out(await _load_product(db, product_id))
