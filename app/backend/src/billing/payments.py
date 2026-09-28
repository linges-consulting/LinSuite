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

**Corrections and refunds (#67).** A correction is a new `invoice_payments` row naming the
entry it supersedes (`corrects_payment_id`); `balances()` counts only unsuperseded rows. A
refund is an `invoice_refunds` row (admin/owner only), added back onto the outstanding balance.
Both take `lock_lineage`'s row lock before checking the cap (refunds <= received, across the
invoice's replacement lineage), so concurrent attempts serialize in Postgres.

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
from sqlalchemy import exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from auth.capabilities import Requires
from auth.models import User
from billing.bill_review import BillViewer
from billing.models import (
    Invoice,
    InvoiceBalanceAuthorization,
    InvoicePayment,
    InvoiceRefund,
)
from core.audit import record_event
from core.db import SessionDep

router = APIRouter(prefix="/invoices", tags=["billing"])

AdminReviewer = Annotated[User, Depends(Requires("billing.manage"))]


# --- the derived gate --------------------------------------------------------------------------


@dataclass(frozen=True)
class Balance:
    """Everything the gate and the follow-up views need, derived from the ledger on read.

    Only *effective* payment rows count — an entry superseded by a correction (#67) is
    ignored, its correction counts instead.

    - `outstanding_cents`: `grand_total_cents` minus every *received* payment (client or
      insurer), plus every refund (#67) — money handed back is no longer collected. A
      `pending` insurer row never reduces it — approval is not money.
    - `pending_insurer_cents`: approved insurer money not yet arrived — pending insurer rows
      minus insurer money actually received (never below zero). A later `received` insurer row
      settles the earlier `pending` one without editing it.
    - `client_outstanding_cents`: the client's own unsettled portion — `outstanding_cents` less
      the pending insurer portion. This is what the checkout gate checks (spec #54: pending
      insurer money does not by itself block checkout once the client portion is settled).
    - `checkout_complete`: client portion settled, or an admin/owner exception exists.
    - `refunded_cents`: total admin-approved refunds against this invoice (#67).
    """

    outstanding_cents: int
    pending_insurer_cents: int
    client_outstanding_cents: int
    checkout_complete: bool
    refunded_cents: int


async def _ledger_sums(
    db: AsyncSession, ids: list[uuid.UUID]
) -> dict[uuid.UUID, tuple[int, int, int, int]]:
    """Per invoice: (received, pending, received from insurer, refunded) — effective rows only."""
    amount = InvoicePayment.amount_cents
    received = InvoicePayment.status == "received"
    superseding = aliased(InvoicePayment)
    effective = ~exists().where(superseding.corrects_payment_id == InvoicePayment.id)
    sums = {
        row[0]: (*row[1:], 0)
        for row in await db.execute(
            select(
                InvoicePayment.invoice_id,
                func.coalesce(func.sum(amount).filter(received), 0),
                func.coalesce(func.sum(amount).filter(InvoicePayment.status == "pending"), 0),
                func.coalesce(
                    func.sum(amount).filter(received, InvoicePayment.payer_type == "insurer"), 0
                ),
            )
            .where(InvoicePayment.invoice_id.in_(ids), effective)
            .group_by(InvoicePayment.invoice_id)
        )
    }
    for invoice_id, refunded in await db.execute(
        select(InvoiceRefund.invoice_id, func.sum(InvoiceRefund.amount_cents))
        .where(InvoiceRefund.invoice_id.in_(ids))
        .group_by(InvoiceRefund.invoice_id)
    ):
        sums[invoice_id] = (*sums.get(invoice_id, (0, 0, 0, 0))[:3], refunded)
    return sums


async def balances(db: AsyncSession, invoices: list[Invoice]) -> dict[uuid.UUID, Balance]:
    """Batched: three queries regardless of list length (the billing list's own reader)."""
    ids = [i.id for i in invoices]
    if not ids:
        return {}
    sums = await _ledger_sums(db, ids)
    excepted = set(
        await db.scalars(
            select(InvoiceBalanceAuthorization.invoice_id)
            .where(InvoiceBalanceAuthorization.invoice_id.in_(ids))
            .distinct()
        )
    )
    out = {}
    for invoice in invoices:
        received_all, pending, received_insurer, refunded = sums.get(invoice.id, (0, 0, 0, 0))
        outstanding = invoice.grand_total_cents - received_all + refunded
        pending_insurer = max(pending - received_insurer, 0)
        client_outstanding = outstanding - pending_insurer
        out[invoice.id] = Balance(
            outstanding_cents=outstanding,
            pending_insurer_cents=pending_insurer,
            client_outstanding_cents=client_outstanding,
            checkout_complete=client_outstanding <= 0 or invoice.id in excepted,
            refunded_cents=refunded,
        )
    return out


# --- the refund cap (#67) ----------------------------------------------------------------------


async def lock_lineage(db: AsyncSession, invoice_id: uuid.UUID) -> list[uuid.UUID]:
    """Every invoice in `invoice_id`'s replacement lineage (`replaces_invoice_id`, both
    directions), row-locked `FOR UPDATE` in id order. Every correction and refund takes this
    lock first, so the cap check and the write it guards are serialized per lineage — the lock
    is Postgres's, never an app lock (CLAUDE.md "Concurrency")."""
    ids = {invoice_id}
    frontier = {invoice_id}
    while frontier:
        rows = await db.execute(
            select(Invoice.id, Invoice.replaces_invoice_id).where(
                or_(Invoice.id.in_(frontier), Invoice.replaces_invoice_id.in_(frontier))
            )
        )
        found = {x for row in rows for x in row if x is not None}
        frontier = found - ids
        ids |= found
    ordered = sorted(ids)
    await db.execute(
        select(Invoice.id).where(Invoice.id.in_(ordered)).order_by(Invoice.id).with_for_update()
    )
    return ordered


async def refundable_cents(db: AsyncSession, lineage: list[uuid.UUID]) -> int:
    """Payments actually received across the lineage minus every refund already made. Call
    only while holding `lock_lineage`'s lock, or the answer can be stale by the time it's used."""
    sums = (await _ledger_sums(db, lineage)).values()
    return sum(s[0] for s in sums) - sum(s[3] for s in sums)


async def record_refund(
    db: AsyncSession, invoice: Invoice, *, amount_cents: int, reason: str, approver: User
) -> InvoiceRefund:
    """The one refund path (#67; #73/#76 call this too). The caller must already have checked
    the approver holds `billing.manage` in Admin Mode. Stages the refund + audit event without
    committing; raises 422 past the cap."""
    lineage = await lock_lineage(db, invoice.id)
    available = await refundable_cents(db, lineage)
    if amount_cents > available:
        raise HTTPException(
            status_code=422,
            detail=f"Refund exceeds money received less prior refunds ({available} cents).",
        )
    refund = InvoiceRefund(
        invoice_id=invoice.id, amount_cents=amount_cents, reason=reason, approved_by=approver.id
    )
    db.add(refund)
    await db.flush()
    record_event(
        db,
        "invoice.refund_recorded",
        target_type="invoice",
        target_id=str(invoice.id),
        actor_user_id=approver.id,
        metadata={"refund_id": str(refund.id), "amount_cents": amount_cents, "reason": reason},
    )
    return refund


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
    corrects_payment_id: str | None
    correction_reason: str | None


class CorrectPaymentIn(BaseModel):
    """Only the fields sent change; the rest carry over from the entry being corrected."""

    reason: Annotated[str, Field(min_length=1, max_length=2000)]
    payer_type: Literal["client", "insurer"] | None = None
    method: Literal["cash", "e_transfer", "card", "insurer"] | None = None
    amount_cents: Annotated[int, Field(ge=0)] | None = None
    reference: Annotated[str, Field(max_length=500)] | None = None


class RefundIn(BaseModel):
    amount_cents: Annotated[int, Field(gt=0)]
    reason: Annotated[str, Field(min_length=1, max_length=2000)]


class RefundOut(BaseModel):
    id: str
    invoice_id: str
    amount_cents: int
    reason: str
    approved_by: str
    refunded_at: datetime


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
        corrects_payment_id=(
            str(payment.corrects_payment_id) if payment.corrects_payment_id else None
        ),
        correction_reason=payment.correction_reason,
    )


def _refund_out(refund: InvoiceRefund) -> RefundOut:
    return RefundOut(
        id=str(refund.id),
        invoice_id=str(refund.invoice_id),
        amount_cents=refund.amount_cents,
        reason=refund.reason,
        approved_by=str(refund.approved_by),
        refunded_at=refund.refunded_at,
    )


def _check_payer(payer_type: str, method: str, status: str) -> None:
    # Defense-in-depth ahead of the DB's own CHECK constraints (`ck_invoice_payments_payer_
    # method_match`/`ck_invoice_payments_pending_only_insurer`) — a clear 422 here beats a raw
    # constraint-violation 500 for the same refusal.
    if (payer_type == "insurer") != (method == "insurer"):
        raise HTTPException(
            status_code=422,
            detail="An insurer payment must use the 'insurer' method, and vice versa.",
        )
    if status == "pending" and payer_type != "insurer":
        raise HTTPException(
            status_code=422, detail="Only an insurer payment can be recorded as pending."
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
    _check_payer(payload.payer_type, payload.method, payload.status)

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


# --- correcting an entry (billing.view) and refunding (billing.manage, Admin Mode) — #67 --------


@router.post("/{invoice_id}/payments/{payment_id}/corrections", status_code=201)
async def correct_payment(
    invoice_id: uuid.UUID,
    payment_id: uuid.UUID,
    payload: CorrectPaymentIn,
    actor: BillViewer,
    db: SessionDep,
) -> PaymentOut:
    """A clerical fix: a new entry superseding `payment_id`, which is never touched. Moves no
    money back to the client — that is only ever `POST /refunds`. Refused if it would leave the
    lineage's received money below what has already been refunded."""
    invoice = await _load_invoice(db, invoice_id)
    lineage = await lock_lineage(db, invoice.id)
    original = await db.get(InvoicePayment, payment_id)
    if original is None or original.invoice_id != invoice.id:
        raise HTTPException(status_code=404, detail="No such payment on this invoice.")
    if await db.scalar(select(exists().where(InvoicePayment.corrects_payment_id == payment_id))):
        raise HTTPException(
            status_code=409,
            detail="This entry has already been corrected; correct the latest correction.",
        )
    changes = payload.model_dump(exclude_unset=True, exclude={"reason"})
    if any(v is None for k, v in changes.items() if k != "reference"):
        raise HTTPException(status_code=422, detail="Only reference may be cleared.")
    fields = {
        k: getattr(original, k) for k in ("payer_type", "method", "amount_cents", "reference")
    }
    if all(fields[k] == v for k, v in changes.items()):
        raise HTTPException(status_code=422, detail="The correction changes nothing.")
    fields |= changes
    _check_payer(fields["payer_type"], fields["method"], original.status)

    correction = InvoicePayment(
        invoice_id=invoice.id,
        status=original.status,
        collected_by=actor.id,
        corrects_payment_id=original.id,
        correction_reason=payload.reason,
        **fields,
    )
    db.add(correction)
    await db.flush()
    if await refundable_cents(db, lineage) < 0:
        raise HTTPException(
            status_code=422,
            detail="This correction would leave less received than has already been refunded.",
        )
    record_event(
        db,
        "invoice.payment_corrected",
        target_type="invoice",
        target_id=str(invoice.id),
        actor_user_id=actor.id,
        metadata={
            "payment_id": str(correction.id),
            "corrects_payment_id": str(original.id),
            "reason": payload.reason,
            "before": {k: getattr(original, k) for k in changes},
            "after": changes,
        },
    )
    await db.commit()
    return _payment_out(correction)


@router.post("/{invoice_id}/refunds", status_code=201)
async def refund(
    invoice_id: uuid.UUID, payload: RefundIn, actor: AdminReviewer, db: SessionDep
) -> RefundOut:
    invoice = await _load_invoice(db, invoice_id)
    created = await record_refund(
        db, invoice, amount_cents=payload.amount_cents, reason=payload.reason, approver=actor
    )
    await db.commit()
    return _refund_out(created)


@router.get("/{invoice_id}/refunds")
async def list_refunds(
    invoice_id: uuid.UUID, _: BillViewer, db: SessionDep
) -> dict[str, list[RefundOut]]:
    await _load_invoice(db, invoice_id)
    refunds = await db.scalars(
        select(InvoiceRefund)
        .where(InvoiceRefund.invoice_id == invoice_id)
        .order_by(InvoiceRefund.refunded_at)
    )
    return {"refunds": [_refund_out(r) for r in refunds]}


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
