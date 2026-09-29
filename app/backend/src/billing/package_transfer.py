"""Package transfer (#111, spec #96): an admin, Admin Mode, `billing.manage`, moves a package
purchase's whole remaining balance to another client, as a deliberate, audited exception.

`POST /packages/purchases/{id}/transfer` — the body carries the target client, the *expected
current holder* and a required reason; a non-transferable definition (`package_definitions.
transferable = false`, the default) additionally needs an explicit `override`. Refused when:
the purchase is not active (unpaid, fully used, refunded or expired); the target is the
current holder, suppressed or missing; the request's `from_customer_id` is not the current
holder — the one check that makes two concurrent transfers serialise into "one wins, one gets
a clear refusal", not two silently-applied hops.

**Race safety.** The purchase row is locked `FOR UPDATE` before the current holder is read, so
a second transfer racing the first waits, re-reads the *post-transfer* holder under the same
lock, and is refused because the `from_customer_id` it was given no longer matches (spec #96
story 17). `billing/redemption.py::redeem_credit` and `billing/package_refund.py::
refund_package` take the identical lock for the identical reason.

**The refund path is untouched.** `billing/package_refund.py` refunds the purchase's own
`customer_id` — the purchaser, permanently — regardless of any transfer; nothing here changes
that route, only the frontend refund dialog's wording (spec #96 story 26) needs the current
holder, read the same way this module derives it.

Audit: `package.transferred` `{purchase_id, from_customer_id, to_customer_id, override}` —
never `reason`, which lives only in `package_transfers` (spec #96 story 8).
"""

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import exists, func, select

from billing.bill_review import business_or_404
from billing.models import (
    Invoice,
    PackageCreditRedemption,
    PackageDefinition,
    PackagePurchase,
    PackageTransfer,
)
from billing.package_holder import holder_id_for, transfer_chain
from billing.package_purchase import TransferHopOut
from billing.payments import AdminReviewer
from billing.redemption import spendable
from core.audit import record_event
from core.db import SessionDep
from customers.models import Customer
from scheduling.clock import today_in
from scheduling.models import Appointment

router = APIRouter(prefix="/packages", tags=["billing"])


def _name(customer: Customer) -> str:
    return f"{customer.first_name} {customer.last_name}"


class PackageTransferIn(BaseModel):
    to_customer_id: uuid.UUID
    # The current holder, as the admin believes it to be — checked against the row-locked
    # truth, which is what turns a stale UI (or a second admin's concurrent transfer) into a
    # refusal instead of a silently-wrong transfer.
    from_customer_id: uuid.UUID
    reason: Annotated[str, Field(min_length=1, max_length=2000)]
    override: bool = False


class PackageTransferOut(BaseModel):
    package_purchase_id: str
    current_holder_id: str
    current_holder_name: str
    transfers: list[TransferHopOut]


@router.post("/purchases/{purchase_id}/transfer", status_code=201)
async def transfer_package(
    purchase_id: uuid.UUID, payload: PackageTransferIn, actor: AdminReviewer, db: SessionDep
) -> PackageTransferOut:
    purchase = await db.scalar(
        select(PackagePurchase).where(PackagePurchase.id == purchase_id).with_for_update()
    )
    if purchase is None:
        raise HTTPException(status_code=404, detail="No such package purchase.")

    # --- active: not unpaid, not expired, not voided (`spendable`, the same rule redemption
    # and the liability report use), and not refunded, and not fully used -------------------
    today = today_in((await business_or_404(db)).timezone)
    active = await db.scalar(
        select(exists().where(PackagePurchase.id == purchase_id, *spendable(today)))
    )
    invoice_issued = await db.scalar(
        select(Invoice.status).where(Invoice.package_purchase_id == purchase_id)
    )
    total_credits = sum(c.credits_total for c in purchase.credits)
    total_redeemed = await db.scalar(
        select(func.count()).where(PackageCreditRedemption.package_purchase_id == purchase_id)
    )
    if not active or invoice_issued != "issued" or total_redeemed >= total_credits:
        raise HTTPException(
            status_code=422,
            detail=(
                "This package isn't active, so its credits can't be transferred — it must be "
                "paid, not fully used, not refunded and not expired."
            ),
        )

    holder_id = await holder_id_for(db, purchase)
    if payload.from_customer_id != holder_id:
        raise HTTPException(
            status_code=409,
            detail="Someone else already transferred this package's credits.",
        )
    if payload.to_customer_id == holder_id:
        raise HTTPException(
            status_code=422, detail="This client already holds this package's credits."
        )

    target = await db.get(Customer, payload.to_customer_id)
    if target is None:
        raise HTTPException(status_code=404, detail="No such client.")
    if target.suppressed_at is not None:
        raise HTTPException(status_code=422, detail="A suppressed client can't receive a transfer.")

    definition = await db.get(PackageDefinition, purchase.package_definition_id)
    assert definition is not None  # RESTRICT: a package definition is never hard-deleted
    if not definition.transferable and not payload.override:
        raise HTTPException(
            status_code=422,
            detail=(
                "This package is non-transferable; transferring it needs an explicit override."
            ),
        )

    db.add(
        PackageTransfer(
            package_purchase_id=purchase_id,
            from_customer_id=payload.from_customer_id,
            to_customer_id=payload.to_customer_id,
            reason=payload.reason,
            override=payload.override,
            transferred_by=actor.id,
        )
    )
    record_event(
        db,
        "package.transferred",
        target_type="package_purchase",
        target_id=str(purchase_id),
        actor_user_id=actor.id,
        metadata={
            "purchase_id": str(purchase_id),
            "from_customer_id": str(payload.from_customer_id),
            "to_customer_id": str(payload.to_customer_id),
            "override": payload.override,
        },
    )
    await db.commit()

    hops = (await transfer_chain(db, [purchase_id])).get(purchase_id, [])
    customer_ids = {payload.to_customer_id, *(h.from_customer_id for h in hops)}
    customer_ids |= {h.to_customer_id for h in hops}
    names = {
        row.id: _name(row)
        for row in await db.scalars(select(Customer).where(Customer.id.in_(customer_ids)))
    }
    return PackageTransferOut(
        package_purchase_id=str(purchase_id),
        current_holder_id=str(payload.to_customer_id),
        current_holder_name=names.get(payload.to_customer_id, "—"),
        transfers=[
            TransferHopOut(
                id=str(h.id),
                from_customer_id=str(h.from_customer_id),
                from_customer_name=names.get(h.from_customer_id, "—"),
                to_customer_id=str(h.to_customer_id),
                to_customer_name=names.get(h.to_customer_id, "—"),
                reason=h.reason,
                override=h.override,
                transferred_at=h.transferred_at,
            )
            for h in hops
        ],
    )


# --- the transfer dialog's own preview reads ----------------------------------------------


class UpcomingAppointmentOut(BaseModel):
    id: str
    starts_at: datetime
    service_name: str
    staff_name: str


class UpcomingAppointments(BaseModel):
    appointments: list[UpcomingAppointmentOut]


@router.get("/purchases/{purchase_id}/upcoming-appointments")
async def upcoming_appointments(
    purchase_id: uuid.UUID, _: AdminReviewer, db: SessionDep
) -> UpcomingAppointments:
    """The current holder's upcoming confirmed appointments for the services this purchase
    credits — what the transfer dialog warns about (spec #96 story 18): those visits won't
    find the credits at checkout once the holder changes, since credits deduct on completion,
    not booking. Not a refusal — a warning only."""
    purchase = await db.get(PackagePurchase, purchase_id, populate_existing=True)
    if purchase is None:
        raise HTTPException(status_code=404, detail="No such package purchase.")
    service_ids = [c.service_id for c in purchase.credits]
    if not service_ids:
        return UpcomingAppointments(appointments=[])
    holder_id = await holder_id_for(db, purchase)
    rows = list(
        await db.scalars(
            select(Appointment)
            .where(
                Appointment.customer_id == holder_id,
                Appointment.status == "confirmed",
                Appointment.starts_at > datetime.now(UTC),
                Appointment.service_id.in_(service_ids),
            )
            .order_by(Appointment.starts_at)
        )
    )
    return UpcomingAppointments(
        appointments=[
            UpcomingAppointmentOut(
                id=str(a.id),
                starts_at=a.starts_at,
                service_name=a.service.name,
                staff_name=a.staff.display_name,
            )
            for a in rows
        ]
    )
