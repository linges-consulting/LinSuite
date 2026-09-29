"""Package/bundle refunds (#73; spec #54 stories 60-65).

`POST /packages/purchases/{id}/refund` (`billing.manage`, Admin Mode) — two paths, one
transaction each:

- **Standard** (no `exception`): only while no credit has been redeemed. Refunds everything
  the purchase invoice holds (money received less prior refunds) and voids every credit.
- **Manual exception** (`exception` given): an admin/owner goodwill refund after use. Requires
  a reason and an explicit amount; the keep-or-cancel choice for the remaining credits
  (default: cancel on a full refund, keep on a partial one) and the preserve-or-reverse choice
  for commission earned on redeemed sessions (default: preserve) may be sent explicitly.

The generic `POST /invoices/{id}/refunds` refuses package-purchase invoices (M4 review R13), so
this route — and its "nonrefundable once a credit is used" standard policy — is the only way a
package's money goes back.

Either way (owner decision, 2026-09-28) the purchase invoice is first **cancelled** through
#68's `cancel_issued`, so a refunded package bills nothing and the refund never reopens a
balance; the money then leaves through #67's `record_refund`, which caps it at money received
less prior refunds across the lineage. There is deliberately no "paid minus sessions used x
price" formula anywhere — #54 replaced it with the manual exception.

**Race safety.** The purchase row is locked `FOR UPDATE` first — the same lock
`redemption.py::redeem_credit` takes — so a completion racing a refund either lands before
(the standard path then sees a redeemed credit and refuses) or waits and finds the credits
voided (and the 0062 insert guard refuses it regardless).
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import exists, func, select

from billing.invoices import InvoiceOut, cancel_issued, invoice_out, reverse_commission
from billing.models import (
    CommissionPosting,
    Invoice,
    InvoiceLine,
    PackageCreditRedemption,
    PackageCreditVoid,
    PackagePurchase,
)
from billing.payments import (
    AdminReviewer,
    RefundOut,
    lock_lineage,
    record_refund,
    refund_out,
    refundable_cents,
)
from core.audit import record_event
from core.db import SessionDep

router = APIRouter(prefix="/packages", tags=["billing"])


class RefundException(BaseModel):
    """The amount is required; both choices default server-side per spec §154 (M4 review R14):
    `cancel_remaining_credits` defaults to cancel on a *full* refund (the amount equals
    everything the purchase still holds) and keep on a partial one; `reverse_commission`
    defaults to preserve (false) — a goodwill refund keeps earned commission. The resolved
    choices are logged either way."""

    amount_cents: Annotated[int, Field(gt=0)]
    cancel_remaining_credits: bool | None = None
    reverse_commission: bool = False


class PackageRefundIn(BaseModel):
    reason: Annotated[str, Field(min_length=1, max_length=2000)]
    exception: RefundException | None = None


class PackageRefundOut(BaseModel):
    package_purchase_id: str
    invoice: InvoiceOut
    refund: RefundOut | None
    credits_voided: bool
    commission_reversals: int


@router.post("/purchases/{purchase_id}/refund", status_code=201)
async def refund_package(
    purchase_id: uuid.UUID, payload: PackageRefundIn, actor: AdminReviewer, db: SessionDep
) -> PackageRefundOut:
    purchase = await db.scalar(
        select(PackagePurchase).where(PackagePurchase.id == purchase_id).with_for_update()
    )
    if purchase is None:
        raise HTTPException(status_code=404, detail="No such package purchase.")
    invoice = await db.scalar(
        select(Invoice)
        .where(Invoice.package_purchase_id == purchase_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    assert invoice is not None  # created in the purchase's own transaction
    redeemed = await db.scalar(
        select(func.count()).where(PackageCreditRedemption.package_purchase_id == purchase_id)
    )
    voided = await db.scalar(
        select(exists().where(PackageCreditVoid.package_purchase_id == purchase_id))
    )

    exc = payload.exception
    if exc is None:
        if redeemed:
            raise HTTPException(
                status_code=422,
                detail=(
                    "A credit has been used, so this package is no longer refundable under the "
                    "standard policy; only an admin exception can refund it."
                ),
            )
        if invoice.status != "issued":
            raise HTTPException(status_code=409, detail="This package is already refunded.")
        amount = await refundable_cents(db, await lock_lineage(db, invoice.id))
        cancel_credits, reverse = True, False  # nothing redeemed, so no commission to reverse
    else:
        amount, cancel_credits, reverse = (
            exc.amount_cents,
            exc.cancel_remaining_credits,
            exc.reverse_commission,
        )
        if cancel_credits is None:
            held = await refundable_cents(db, await lock_lineage(db, invoice.id))
            cancel_credits = amount >= held

    refund = None
    if amount > 0:
        refund = await record_refund(
            db, invoice, amount_cents=amount, reason=payload.reason, approver=actor
        )
    if invoice.status == "issued":
        cancel_issued(db, invoice, actor.id, payload.reason)
    if cancel_credits and not voided:
        db.add(
            PackageCreditVoid(
                package_purchase_id=purchase_id, reason=payload.reason, voided_by=actor.id
            )
        )
    reversals = 0
    if reverse:
        delivered = select(InvoiceLine.id).where(
            InvoiceLine.appointment_id.in_(
                select(PackageCreditRedemption.appointment_id).where(
                    PackageCreditRedemption.package_purchase_id == purchase_id
                )
            )
        )
        reversals = await reverse_commission(db, CommissionPosting.invoice_line_id.in_(delivered))

    record_event(
        db,
        "package_purchase.refunded",
        target_type="package_purchase",
        target_id=str(purchase_id),
        actor_user_id=actor.id,
        metadata={
            "invoice_id": str(invoice.id),
            "exception": exc is not None,
            "reason": payload.reason,
            "amount_cents": amount,
            "credits_redeemed": redeemed,
            "cancel_remaining_credits": cancel_credits,
            "reverse_commission": reverse,
            "commission_reversals": reversals,
        },
    )
    await db.commit()

    invoice = await db.get(Invoice, invoice.id, populate_existing=True)
    assert invoice is not None
    return PackageRefundOut(
        package_purchase_id=str(purchase_id),
        invoice=await invoice_out(db, invoice),
        refund=refund_out(refund) if refund else None,
        credits_voided=cancel_credits or bool(voided),
        commission_reversals=reversals,
    )
