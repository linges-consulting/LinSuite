"""Manual payment ledger + checkout gate (#66). See `billing/models.py`'s `## manual payment
ledger + checkout gate (#66)` section for the full schema and design rationale — this module
is the one writer of `invoice_payments`/`invoice_balance_authorizations`, and the home of the
derived "checkout complete" predicate every later ticket should import rather than re-deriving.

**The gate, precisely.** `is_checkout_complete(db, invoice)` is true when either:

1. the client's own portion is settled — `grand_total_cents` minus received payments minus
   still-pending approved insurer money is `<= 0` (pending insurer money stays visibly
   unpaid in `outstanding_cents`/`pending_insurer_cents` but does not block checkout), or
2. `has_authorized_exception(db, invoice.id)` — an admin/owner recorded an `Invoice
   BalanceAuthorization` for this invoice (`POST /invoices/{id}/balance-exceptions`).

Nothing here is a stored flag; both halves are computed fresh from the ledger and the
authorization table on every call, per CLAUDE.md and this ticket's own acceptance criteria.

**Capabilities: `billing.view` records payments (front-desk checkout, #65's own precedent for
this same screen); `billing.manage` (Admin Mode) authorizes the outstanding-balance exception
— the same `AdminReviewer` shape `bill_authority.py` already uses for #64's admin decision.**
"""

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.capabilities import Requires
from auth.models import User
from billing.bill_review import BillViewer
from billing.models import (
    Invoice,
    InvoiceBalanceAuthorization,
    InvoiceLine,
    InvoicePayment,
    PackagePurchase,
)
from billing.package_purchase import activate_credits
from core.audit import record_event
from core.db import SessionDep

router = APIRouter(prefix="/invoices", tags=["billing"])

AdminReviewer = Annotated[User, Depends(Requires("billing.manage"))]


# --- the derived gate --------------------------------------------------------------------------


@dataclass(frozen=True)
class Balance:
    """Everything the gate and the follow-up views need, derived from the ledger on read.

    - `outstanding_cents`: `grand_total_cents` minus every *received* payment (client or
      insurer). A `pending` insurer row never reduces it — approval is not money.
    - `pending_insurer_cents`: approved insurer money not yet arrived — pending insurer rows
      minus insurer money actually received (never below zero). A later `received` insurer row
      settles the earlier `pending` one without editing it.
    - `client_outstanding_cents`: the client's own unsettled portion — `outstanding_cents` less
      the pending insurer portion. This is what the checkout gate checks (spec #54: pending
      insurer money does not by itself block checkout once the client portion is settled).
    - `checkout_complete`: client portion settled, or an admin/owner exception exists.
    - `prepaid_cents` (#72): the part of `grand_total_cents` redeemed package credits already
      settled (`InvoiceLine.prepaid_cents`) — never owed, never counted as a payment.
    """

    outstanding_cents: int
    pending_insurer_cents: int
    client_outstanding_cents: int
    checkout_complete: bool
    prepaid_cents: int = 0


async def balances(db: AsyncSession, invoices: list[Invoice]) -> dict[uuid.UUID, Balance]:
    """Batched: three queries regardless of list length (the billing list's own reader)."""
    ids = [i.id for i in invoices]
    if not ids:
        return {}
    amount = InvoicePayment.amount_cents
    received = InvoicePayment.status == "received"
    sums = {
        row[0]: row[1:]
        for row in await db.execute(
            select(
                InvoicePayment.invoice_id,
                func.coalesce(func.sum(amount).filter(received), 0),
                func.coalesce(func.sum(amount).filter(InvoicePayment.status == "pending"), 0),
                func.coalesce(
                    func.sum(amount).filter(received, InvoicePayment.payer_type == "insurer"), 0
                ),
            )
            .where(InvoicePayment.invoice_id.in_(ids))
            .group_by(InvoicePayment.invoice_id)
        )
    }
    excepted = set(
        await db.scalars(
            select(InvoiceBalanceAuthorization.invoice_id)
            .where(InvoiceBalanceAuthorization.invoice_id.in_(ids))
            .distinct()
        )
    )
    prepaid_by_invoice = dict(
        (
            await db.execute(
                select(InvoiceLine.invoice_id, func.sum(InvoiceLine.prepaid_cents))
                .where(InvoiceLine.invoice_id.in_(ids))
                .group_by(InvoiceLine.invoice_id)
            )
        ).all()
    )
    out = {}
    for invoice in invoices:
        received_all, pending, received_insurer = sums.get(invoice.id, (0, 0, 0))
        prepaid = prepaid_by_invoice.get(invoice.id, 0)
        outstanding = invoice.grand_total_cents - prepaid - received_all
        pending_insurer = max(pending - received_insurer, 0)
        client_outstanding = outstanding - pending_insurer
        out[invoice.id] = Balance(
            outstanding_cents=outstanding,
            pending_insurer_cents=pending_insurer,
            client_outstanding_cents=client_outstanding,
            checkout_complete=client_outstanding <= 0 or invoice.id in excepted,
            prepaid_cents=prepaid,
        )
    return out


async def balance(db: AsyncSession, invoice: Invoice) -> Balance:
    return (await balances(db, [invoice]))[invoice.id]


async def is_checkout_complete(db: AsyncSession, invoice: Invoice) -> bool:
    """The one predicate a later ticket (#70's receipt-release gate, most directly) should
    import and call — never re-derive it."""
    return (await balance(db, invoice)).checkout_complete


# --- what goes over the wire -----------------------------------------------------------------


class RecordPaymentIn(BaseModel):
    payer_type: Literal["client", "insurer"]
    method: Literal["cash", "e_transfer", "card", "insurer"]
    amount_cents: Annotated[int, Field(gt=0)]
    status: Literal["pending", "received"] = "received"
    reference: Annotated[str, Field(max_length=500)] | None = None


class PaymentOut(BaseModel):
    id: str
    invoice_id: str
    payer_type: str
    method: str
    status: str
    amount_cents: int
    reference: str | None
    collected_by: str
    recorded_at: datetime


class AuthorizeBalanceIn(BaseModel):
    reason: Annotated[str, Field(min_length=1, max_length=2000)]


class BalanceAuthorizationOut(BaseModel):
    id: str
    invoice_id: str
    authorized_by: str
    reason: str
    outstanding_cents_at_authorization: int
    authorized_at: datetime


def _payment_out(payment: InvoicePayment) -> PaymentOut:
    return PaymentOut(
        id=str(payment.id),
        invoice_id=str(payment.invoice_id),
        payer_type=payment.payer_type,
        method=payment.method,
        status=payment.status,
        amount_cents=payment.amount_cents,
        reference=payment.reference,
        collected_by=str(payment.collected_by),
        recorded_at=payment.recorded_at,
    )


def _authorization_out(auth: InvoiceBalanceAuthorization) -> BalanceAuthorizationOut:
    return BalanceAuthorizationOut(
        id=str(auth.id),
        invoice_id=str(auth.invoice_id),
        authorized_by=str(auth.authorized_by),
        reason=auth.reason,
        outstanding_cents_at_authorization=auth.outstanding_cents_at_authorization,
        authorized_at=auth.authorized_at,
    )


async def _load_invoice(db: SessionDep, invoice_id: uuid.UUID) -> Invoice:
    invoice = await db.get(Invoice, invoice_id, populate_existing=True)
    if invoice is None:
        raise HTTPException(status_code=404, detail="No such invoice.")
    return invoice


# --- recording payments (billing.view — checkout, #65's own precedent) ------------------------


@router.post("/{invoice_id}/payments", status_code=201)
async def record_payment(
    invoice_id: uuid.UUID, payload: RecordPaymentIn, actor: BillViewer, db: SessionDep
) -> PaymentOut:
    invoice = await _load_invoice(db, invoice_id)
    if invoice.status != "issued":
        raise HTTPException(
            status_code=422, detail="This invoice is not issued; payments cannot be recorded."
        )
    # Defense-in-depth ahead of the DB's own CHECK constraints (`ck_invoice_payments_payer_
    # method_match`/`ck_invoice_payments_pending_only_insurer`) — a clear 422 here beats a raw
    # constraint-violation 500 for the same refusal.
    if (payload.payer_type == "insurer") != (payload.method == "insurer"):
        raise HTTPException(
            status_code=422,
            detail="An insurer payment must use the 'insurer' method, and vice versa.",
        )
    if payload.status == "pending" and payload.payer_type != "insurer":
        raise HTTPException(
            status_code=422, detail="Only an insurer payment can be recorded as pending."
        )

    payment = InvoicePayment(
        invoice_id=invoice.id,
        payer_type=payload.payer_type,
        method=payload.method,
        status=payload.status,
        amount_cents=payload.amount_cents,
        reference=payload.reference,
        collected_by=actor.id,
    )
    db.add(payment)
    await db.flush()
    record_event(
        db,
        "invoice.payment_recorded",
        target_type="invoice",
        target_id=str(invoice.id),
        actor_user_id=actor.id,
        metadata={
            "payment_id": str(payment.id),
            "payer_type": payload.payer_type,
            "method": payload.method,
            "status": payload.status,
            "amount_cents": payload.amount_cents,
        },
    )
    # #71/#72: a package purchase's credits activate the moment its invoice is fully paid —
    # money actually received, never a pending insurer allocation or a balance exception.
    if (
        invoice.package_purchase_id is not None
        and (await balance(db, invoice)).outstanding_cents <= 0
    ):
        purchase = await db.get(PackagePurchase, invoice.package_purchase_id)
        assert purchase is not None
        await activate_credits(db, purchase)
    await db.commit()
    return _payment_out(payment)


@router.get("/{invoice_id}/payments")
async def list_payments(
    invoice_id: uuid.UUID, _: BillViewer, db: SessionDep
) -> dict[str, list[PaymentOut]]:
    await _load_invoice(db, invoice_id)
    payments = await db.scalars(
        select(InvoicePayment)
        .where(InvoicePayment.invoice_id == invoice_id)
        .order_by(InvoicePayment.recorded_at)
    )
    return {"payments": [_payment_out(p) for p in payments]}


# --- authorizing the outstanding-balance exception (billing.manage, Admin Mode) ----------------


@router.post("/{invoice_id}/balance-exceptions", status_code=201)
async def authorize_outstanding_balance(
    invoice_id: uuid.UUID, payload: AuthorizeBalanceIn, actor: AdminReviewer, db: SessionDep
) -> BalanceAuthorizationOut:
    invoice = await _load_invoice(db, invoice_id)
    if invoice.status != "issued":
        raise HTTPException(
            status_code=422, detail="This invoice is not issued; there is nothing to authorize."
        )
    outstanding = (await balance(db, invoice)).client_outstanding_cents
    if outstanding <= 0:
        raise HTTPException(
            status_code=422, detail="This invoice has no outstanding client balance to authorize."
        )

    authorization = InvoiceBalanceAuthorization(
        invoice_id=invoice.id,
        authorized_by=actor.id,
        reason=payload.reason,
        outstanding_cents_at_authorization=outstanding,
    )
    db.add(authorization)
    await db.flush()
    record_event(
        db,
        "invoice.balance_exception_authorized",
        target_type="invoice",
        target_id=str(invoice.id),
        actor_user_id=actor.id,
        metadata={"outstanding_cents": outstanding, "reason": payload.reason},
    )
    await db.commit()
    return _authorization_out(authorization)


@router.get("/{invoice_id}/balance-exceptions")
async def list_balance_exceptions(
    invoice_id: uuid.UUID, _: BillViewer, db: SessionDep
) -> dict[str, list[BalanceAuthorizationOut]]:
    await _load_invoice(db, invoice_id)
    authorizations = await db.scalars(
        select(InvoiceBalanceAuthorization)
        .where(InvoiceBalanceAuthorization.invoice_id == invoice_id)
        .order_by(InvoiceBalanceAuthorization.authorized_at)
    )
    return {"exceptions": [_authorization_out(a) for a in authorizations]}
