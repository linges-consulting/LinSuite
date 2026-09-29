"""Package credit redemption at appointment completion (#72).

Staff pick an eligible purchase (`GET /appointments/{id}/package-credits`) and pass its id to
`POST /appointments/{id}/complete`; `complete_appointment` calls `redeem_credit` inside its own
transaction, before `record_draft_bill_line`, so completion, the redemption row and the
prepaid draft line commit together or not at all. Booking, cancelling and no-show never reach
this module — only completion spends a credit (CLAUDE.md "package credits deduct on
completion").

**Eligible** = the appointment's own customer is the purchase's *current holder* (#111: a
purchase's latest transfer, else the purchaser — `billing/package_holder.py`; a transferred
package's credits are offered to whoever holds it now, not to the purchaser or an earlier
holder), credits activated (#71: the purchase invoice is fully paid), not voided by a refund
(#73, `package_credit_voids`), a credit for the appointment's service with one left, and not
past `expires_at` in the business's timezone. `_eligible` is the one place that rule lives.

**Race safety.** The purchase row is locked `FOR UPDATE` before counting what is left, so a
second completion racing for the last credit waits, recounts, and gets a clean 409. The
database refuses it anyway (migration 0061's unique `sequence` + insert guard) — the lock only
makes the refusal readable.

**Value.** A credit's `allocated_price_cents` (#71) is the value of all `credits_total`
sessions of that service; one session is its share across `credits_total` equal weights
(`allocate_bundle_price`, so the sessions always sum back to the allocation exactly).
"""

import uuid
from dataclasses import dataclass
from datetime import date as Date
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import ColumnElement, exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.capabilities import Requires
from auth.models import User
from billing.allocation import allocate_bundle_price
from billing.bill_review import business_or_404
from billing.models import (
    PackageCreditRedemption,
    PackageCreditVoid,
    PackagePurchase,
    PackagePurchaseCredit,
)
from billing.package_holder import current_holder_id
from core.audit import record_event
from core.db import SessionDep
from scheduling.clock import today_in
from scheduling.models import Appointment

router = APIRouter(prefix="/appointments", tags=["billing"])

# Whoever may complete an appointment may see what it could redeem.
Scheduler = Annotated[User, Depends(Requires("schedule.manage"))]


@dataclass(frozen=True)
class Eligible:
    purchase: PackagePurchase
    credit: PackagePurchaseCredit
    redeemed: int

    @property
    def remaining(self) -> int:
        return self.credit.credits_total - self.redeemed

    @property
    def next_value_cents(self) -> int:
        shares = allocate_bundle_price(
            [1] * self.credit.credits_total, self.credit.allocated_price_cents
        )
        return shares[self.redeemed]


def spendable(today: Date) -> list[ColumnElement[bool]]:
    """The purchase-level half of eligibility: activated (paid), not voided by a refund, not
    past `expires_at` (inclusive, `today` in the business's timezone). `_eligible` adds the
    appointment's customer and service; the liability report (#74) uses it as-is."""
    return [
        PackagePurchase.credits_activated,
        (PackagePurchase.expires_at.is_(None)) | (PackagePurchase.expires_at >= today),
        ~exists().where(PackageCreditVoid.package_purchase_id == PackagePurchase.id),
    ]


async def _eligible(
    db: AsyncSession, appointment: Appointment, today: Date, purchase_id: uuid.UUID | None = None
) -> list[Eligible]:
    query = (
        select(PackagePurchase, PackagePurchaseCredit)
        .join(PackagePurchaseCredit)
        .where(
            # #111: eligibility follows the *current holder* (a purchase's latest transfer,
            # else the purchaser), not `PackagePurchase.customer_id` directly — a transferred
            # purchase's credits are offered to the new holder and no longer to the old one.
            current_holder_id() == appointment.customer_id,
            *spendable(today),
            PackagePurchaseCredit.service_id == appointment.service_id,
        )
        .order_by(PackagePurchase.purchased_at)
    )
    if purchase_id is not None:
        query = query.where(PackagePurchase.id == purchase_id)
    rows = list(await db.execute(query))
    if not rows:
        return []
    used = dict(
        (
            await db.execute(
                select(PackageCreditRedemption.package_purchase_id, func.count())
                .where(
                    PackageCreditRedemption.package_purchase_id.in_([p.id for p, _ in rows]),
                    PackageCreditRedemption.service_id == appointment.service_id,
                )
                .group_by(PackageCreditRedemption.package_purchase_id)
            )
        ).all()
    )
    return [Eligible(p, c, used.get(p.id, 0)) for p, c in rows]


async def redeem_credit(
    db: AsyncSession, appointment: Appointment, purchase_id: uuid.UUID, actor_id: uuid.UUID
) -> int:
    """Spend one credit of `purchase_id` on `appointment`; returns the session's value. Caller
    owns the transaction (`complete_appointment`); nothing here commits."""
    locked = await db.scalar(
        select(PackagePurchase.id).where(PackagePurchase.id == purchase_id).with_for_update()
    )
    if locked is None:
        raise HTTPException(status_code=404, detail="No such package purchase.")
    business = await business_or_404(db)
    found = await _eligible(db, appointment, today_in(business.timezone), purchase_id)
    if not found:
        raise HTTPException(
            status_code=422,
            detail=(
                "This package can't be used for this appointment — it must belong to this "
                "client, be fully paid, not refunded, unexpired and include this service."
            ),
        )
    eligible = found[0]
    if eligible.remaining <= 0:
        raise HTTPException(status_code=409, detail="This package has no credits left.")
    value_cents = eligible.next_value_cents
    db.add(
        PackageCreditRedemption(
            package_purchase_id=purchase_id,
            service_id=appointment.service_id,
            sequence=eligible.redeemed + 1,
            appointment_id=appointment.id,
            value_cents=value_cents,
            redeemed_by=actor_id,
        )
    )
    record_event(
        db,
        "package_credit.redeemed",
        target_type="package_purchase",
        target_id=str(purchase_id),
        actor_user_id=actor_id,
        metadata={
            "appointment_id": str(appointment.id),
            "service_id": str(appointment.service_id),
            "sequence": eligible.redeemed + 1,
            "value_cents": value_cents,
        },
    )
    return value_cents


class PackageCreditOut(BaseModel):
    package_purchase_id: str
    name: str
    purchased_at: datetime
    expires_at: Date | None
    credits_total: int
    credits_remaining: int
    value_cents: int


@router.get("/{appointment_id}/package-credits")
async def list_package_credits(
    appointment_id: uuid.UUID, _: Scheduler, db: SessionDep
) -> dict[str, list[PackageCreditOut]]:
    """What this appointment could redeem on completion — only purchases with a credit left."""
    appointment = await db.get(Appointment, appointment_id)
    if appointment is None:
        raise HTTPException(status_code=404, detail="No such appointment.")
    business = await business_or_404(db)
    return {
        "credits": [
            PackageCreditOut(
                package_purchase_id=str(e.purchase.id),
                name=e.purchase.name,
                purchased_at=e.purchase.purchased_at,
                expires_at=e.purchase.expires_at,
                credits_total=e.credit.credits_total,
                credits_remaining=e.remaining,
                value_cents=e.next_value_cents,
            )
            for e in await _eligible(db, appointment, today_in(business.timezone))
            if e.remaining > 0
        ]
    }
