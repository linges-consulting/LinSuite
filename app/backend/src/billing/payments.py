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

**Retail invoices share this ledger (#76).** `invoice_payments`/`invoice_refunds` rows reference
exactly one of `invoices` or `retail_invoices`; every function here takes either kind (`AnyInvoice`)
and the ledger math is the same code for both — ids are UUIDs, so one id list never mixes them up.
`retail_router` mounts the same payment/correction/refund handlers under `/retail-invoices/{id}`.

Nothing here is a stored flag; both halves are computed fresh from the ledger and the
authorization table on every call, per CLAUDE.md and this ticket's own acceptance criteria.

**Capabilities: `billing.view` records payments (front-desk checkout, #65's own precedent for
this same screen); `billing.manage` (Admin Mode) authorizes the outstanding-balance exception
— the same `AdminReviewer` shape `bill_authority.py` already uses for #64's admin decision.**
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import ColumnElement, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from auth.capabilities import Requires
from auth.models import User
from billing.bill_review import BillViewer
from billing.models import (
    Invoice,
    InvoiceBalanceAuthorization,
    InvoiceLine,
    InvoicePayment,
    InvoicePaymentTransfer,
    InvoiceRefund,
    PackagePurchase,
    RetailInvoice,
)
from billing.package_purchase import activate_credits
from core.access_log import LogAccessOf
from core.audit import record_event
from core.db import SessionDep

router = APIRouter(prefix="/invoices", tags=["billing"])
retail_router = APIRouter(prefix="/retail-invoices", tags=["billing"])

AnyInvoice = Invoice | RetailInvoice


def _ledger_fk(invoice: AnyInvoice) -> str:
    """Which ledger column points at this invoice (#76)."""
    return "retail_invoice_id" if isinstance(invoice, RetailInvoice) else "invoice_id"


def _target_type(invoice: AnyInvoice) -> str:
    return "retail_invoice" if isinstance(invoice, RetailInvoice) else "invoice"


AdminReviewer = Annotated[User, Depends(Requires("billing.manage"))]


# `invoice_payment_transfers` has a service pair and a retail pair of ends (0064); exactly one
# pair is set, and ids are UUIDs, so the coalesced end is the invoice id either way.
def _transfer_from(t: InvoicePaymentTransfer) -> uuid.UUID:
    return t.from_invoice_id or t.from_retail_invoice_id  # type: ignore[return-value]


def _transfer_to(t: InvoicePaymentTransfer) -> uuid.UUID:
    return t.to_invoice_id or t.to_retail_invoice_id  # type: ignore[return-value]


def _transfer_touches(ids: list[uuid.UUID]) -> ColumnElement[bool]:
    return or_(
        InvoicePaymentTransfer.from_invoice_id.in_(ids),
        InvoicePaymentTransfer.to_invoice_id.in_(ids),
        InvoicePaymentTransfer.from_retail_invoice_id.in_(ids),
        InvoicePaymentTransfer.to_retail_invoice_id.in_(ids),
    )


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
    - `prepaid_cents` (#72): the part of `grand_total_cents` redeemed package credits already
      settled (`InvoiceLine.prepaid_cents`) — never owed, never counted as a payment.
    - `held_credit_cents` (M4 review R12): money a *cancelled* invoice still holds — received
      less refunds, not yet carried to a replacement (or kept after a partial goodwill package
      refund). A cancelled invoice bills nothing, so its outstanding, pending-insurer and client
      figures are all 0 and the retained money is shown here, never as a negative balance that
      reads as "owed to the client". Always 0 on an issued invoice (an overpayment there stays a
      negative `outstanding_cents`, the ordinary credit #67's refund settles).
    """

    outstanding_cents: int
    pending_insurer_cents: int
    client_outstanding_cents: int
    checkout_complete: bool
    refunded_cents: int
    prepaid_cents: int = 0
    held_credit_cents: int = 0


async def ledger_sums(
    db: AsyncSession, ids: list[uuid.UUID]
) -> dict[uuid.UUID, tuple[int, int, int, int]]:
    """`(received, pending, received_insurer, refunded)` per invoice. Only *effective* payment
    rows count (an entry superseded by a #67 correction is ignored). `received` is net of the
    invoice's own #67 refunds, then net of #68's payment transfers — a transfer moves an
    original's net sums onto its replacement, so money is counted on exactly one invoice.
    `refunded` is informational and stays on the invoice it was recorded against. Invoices with
    no ledger activity are absent (read as zeros)."""
    amount = InvoicePayment.amount_cents
    owner = func.coalesce(InvoicePayment.invoice_id, InvoicePayment.retail_invoice_id)
    received = InvoicePayment.status == "received"
    superseding = aliased(InvoicePayment)
    effective = ~exists().where(superseding.corrects_payment_id == InvoicePayment.id)
    sums: dict[uuid.UUID, list[int]] = {
        row[0]: [*row[1:], 0]
        for row in await db.execute(
            select(
                owner,
                func.coalesce(func.sum(amount).filter(received), 0),
                func.coalesce(func.sum(amount).filter(InvoicePayment.status == "pending"), 0),
                func.coalesce(
                    func.sum(amount).filter(received, InvoicePayment.payer_type == "insurer"), 0
                ),
            )
            .where(
                or_(InvoicePayment.invoice_id.in_(ids), InvoicePayment.retail_invoice_id.in_(ids)),
                effective,
            )
            .group_by(owner)
        )
    }
    refund_owner = func.coalesce(InvoiceRefund.invoice_id, InvoiceRefund.retail_invoice_id)
    for invoice_id, refunded in await db.execute(
        select(refund_owner, func.sum(InvoiceRefund.amount_cents))
        .where(or_(InvoiceRefund.invoice_id.in_(ids), InvoiceRefund.retail_invoice_id.in_(ids)))
        .group_by(refund_owner)
    ):
        acc = sums.setdefault(invoice_id, [0, 0, 0, 0])
        acc[0] -= refunded
        acc[3] += refunded
    transfers = await db.scalars(select(InvoicePaymentTransfer).where(_transfer_touches(ids)))
    for t in transfers:
        moved = (t.received_cents, t.pending_insurer_cents, t.received_insurer_cents)
        for invoice_id, sign in ((_transfer_from(t), -1), (_transfer_to(t), 1)):
            acc = sums.setdefault(invoice_id, [0, 0, 0, 0])
            for i, cents in enumerate(moved):
                acc[i] += sign * cents
    return {k: (v[0], v[1], v[2], v[3]) for k, v in sums.items()}


async def balances(db: AsyncSession, invoices: Sequence[AnyInvoice]) -> dict[uuid.UUID, Balance]:
    """Batched: a fixed handful of queries regardless of list length (the billing list's own
    reader).

    A cancelled invoice (#68) bills nothing and owes nothing: whatever money it still holds
    (until a replacement's issue transfers it away, or for good after a partial goodwill
    refund) is `held_credit_cents`, never a negative outstanding (M4 review R12)."""
    ids = [i.id for i in invoices]
    if not ids:
        return {}
    sums = await ledger_sums(db, ids)
    auth_owner = func.coalesce(
        InvoiceBalanceAuthorization.invoice_id, InvoiceBalanceAuthorization.retail_invoice_id
    )
    excepted = set(await db.scalars(select(auth_owner).where(auth_owner.in_(ids)).distinct()))
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
        received_net, pending, received_insurer, refunded = sums.get(invoice.id, (0, 0, 0, 0))
        prepaid = prepaid_by_invoice.get(invoice.id, 0)
        if invoice.status != "issued":
            # Cancelled: bills nothing (its replacement's own lines carry any prepaid share),
            # so the only figure left is the money it still holds.
            out[invoice.id] = Balance(
                outstanding_cents=0,
                pending_insurer_cents=0,
                client_outstanding_cents=0,
                checkout_complete=True,
                refunded_cents=refunded,
                prepaid_cents=prepaid,
                held_credit_cents=received_net,
            )
            continue
        outstanding = invoice.grand_total_cents - prepaid - received_net
        pending_insurer = max(pending - received_insurer, 0)
        client_outstanding = outstanding - pending_insurer
        out[invoice.id] = Balance(
            outstanding_cents=outstanding,
            pending_insurer_cents=pending_insurer,
            client_outstanding_cents=client_outstanding,
            checkout_complete=client_outstanding <= 0 or invoice.id in excepted,
            refunded_cents=refunded,
            prepaid_cents=prepaid,
        )
    return out


# --- the refund cap (#67) ----------------------------------------------------------------------


async def lock_lineage(
    db: AsyncSession,
    invoice_id: uuid.UUID,
    model: type[Invoice] | type[RetailInvoice] = Invoice,
) -> list[uuid.UUID]:
    """Every invoice in `invoice_id`'s replacement lineage (`replaces_invoice_id`, both
    directions), row-locked `FOR UPDATE` in id order. `model=RetailInvoice` walks the retail
    table instead (#76) — the same lock #76's return action takes for its over-return guard.
    Every correction and refund takes this lock first, so the cap check and the write it guards
    are serialized per lineage — the lock is Postgres's, never an app lock (CLAUDE.md
    "Concurrency")."""
    ids = {invoice_id}
    frontier = {invoice_id}
    while frontier:
        rows = await db.execute(
            select(model.id, model.replaces_invoice_id).where(
                or_(model.id.in_(frontier), model.replaces_invoice_id.in_(frontier))
            )
        )
        found = {x for row in rows for x in row if x is not None}
        frontier = found - ids
        ids |= found
    ordered = sorted(ids)
    await db.execute(
        select(model.id).where(model.id.in_(ordered)).order_by(model.id).with_for_update()
    )
    return ordered


async def refundable_cents(db: AsyncSession, lineage: list[uuid.UUID]) -> int:
    """Payments actually received across the lineage minus every refund already made. Call
    only while holding `lock_lineage`'s lock, or the answer can be stale by the time it's used."""
    # `ledger_sums`' received is already net of refunds, and transfers net to zero across a
    # lineage — so the lineage total is exactly money received less money refunded.
    return sum(s[0] for s in (await ledger_sums(db, lineage)).values())


async def record_refund(
    db: AsyncSession,
    invoice: AnyInvoice,
    *,
    amount_cents: int,
    reason: str,
    approver: User,
    reverses_commission: bool = False,
) -> InvoiceRefund:
    """The one refund path (#67; #73/#76 call this too). The caller must already have checked
    the approver holds `billing.manage` in Admin Mode. Stages the refund + audit event without
    committing; raises 422 past the cap."""
    lineage = await lock_lineage(db, invoice.id, type(invoice))
    model = type(invoice)
    if await db.scalar(select(exists().where(model.replaces_invoice_id == invoice.id))):
        # R10: once replaced, the original's money lives on the replacement (#68 transfer) —
        # refunding here would leave the original showing a phantom balance.
        raise HTTPException(
            status_code=422,
            detail="This invoice has been replaced; refund against its live replacement instead.",
        )
    available = await refundable_cents(db, lineage)
    if amount_cents > available:
        raise HTTPException(
            status_code=422,
            detail=f"Refund exceeds money received less prior refunds ({available} cents).",
        )
    refund = InvoiceRefund(
        **{_ledger_fk(invoice): invoice.id},
        amount_cents=amount_cents,
        reason=reason,
        approved_by=approver.id,
        reverses_commission=reverses_commission,
    )
    db.add(refund)
    await db.flush()
    record_event(
        db,
        "invoice.refund_recorded",
        target_type=_target_type(invoice),
        target_id=str(invoice.id),
        actor_user_id=approver.id,
        metadata={"refund_id": str(refund.id), "amount_cents": amount_cents, "reason": reason},
    )
    return refund


async def carry_payments(
    db: AsyncSession, original: AnyInvoice, replacement: AnyInvoice, actor_id: uuid.UUID
) -> None:
    """#68's transfer, for either invoice kind: carry every cent the cancelled `original` holds
    (its own payments plus anything it inherited, net of refunds) onto its just-inserted
    `replacement` — never charged again, never counted twice. An increase is then an ordinary
    new payment; a decrease leaves a credit for an approved refund.

    Takes `lock_lineage` first (R9): a refund or correction racing this reissue either lands
    before the snapshot below (and is carried net) or waits, then finds the original replaced
    and is refused (R10). Stages rows + audit; does not commit."""
    await lock_lineage(db, original.id, type(original))
    received, pending, received_insurer, _refunded = (await ledger_sums(db, [original.id])).get(
        original.id, (0, 0, 0, 0)
    )
    kind = _ledger_fk(original)
    db.add(
        InvoicePaymentTransfer(
            **{f"from_{kind}": original.id, f"to_{kind}": replacement.id},
            received_cents=received,
            received_insurer_cents=received_insurer,
            pending_insurer_cents=pending,
            transferred_by=actor_id,
        )
    )
    record_event(
        db,
        "invoice.payments_transferred",
        target_type=_target_type(replacement),
        target_id=str(replacement.id),
        actor_user_id=actor_id,
        metadata={
            "from_invoice_id": str(original.id),
            "received_cents": received,
            "pending_insurer_cents": pending,
        },
    )


async def balance(db: AsyncSession, invoice: AnyInvoice) -> Balance:
    return (await balances(db, [invoice]))[invoice.id]


async def is_checkout_complete(db: AsyncSession, invoice: AnyInvoice) -> bool:
    """The one predicate a later ticket (#70's receipt-release gate, most directly) should
    import and call — never re-derive it."""
    return (await balance(db, invoice)).checkout_complete


def requeue_receipts(invoice: AnyInvoice) -> None:
    """M4 review R20: a service invoice's treatment-receipt status (released? Paid / Pending
    insurer / ...) follows its ledger, so every ledger write re-queues its render *after*
    commit. The renderer only stores what is missing, so a no-op change costs two reads."""
    if isinstance(invoice, Invoice) and invoice.service_bill_id is not None:
        # Imported here: `billing.documents` reads this module's `balance`.
        from billing.documents import render_invoice_documents

        render_invoice_documents.delay(str(invoice.id))


# --- what goes over the wire -----------------------------------------------------------------


class RecordPaymentIn(BaseModel):
    payer_type: Literal["client", "insurer"]
    method: Literal["cash", "e_transfer", "card", "insurer"]
    amount_cents: Annotated[int, Field(gt=0)]
    status: Literal["pending", "received"] = "received"
    reference: Annotated[str, Field(max_length=500)] | None = None


class PaymentOut(BaseModel):
    id: str
    invoice_id: str | None
    retail_invoice_id: str | None = None
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
    invoice_id: str | None
    retail_invoice_id: str | None = None
    amount_cents: int
    reason: str
    approved_by: str
    refunded_at: datetime


class PaymentTransferOut(BaseModel):
    """#68's transfer history: money carried from a cancelled original to its replacement."""

    id: str
    from_invoice_id: str
    to_invoice_id: str
    received_cents: int
    received_insurer_cents: int
    pending_insurer_cents: int
    transferred_by: str
    transferred_at: datetime


class AuthorizeBalanceIn(BaseModel):
    reason: Annotated[str, Field(min_length=1, max_length=2000)]


class BalanceAuthorizationOut(BaseModel):
    id: str
    invoice_id: str | None
    retail_invoice_id: str | None = None
    authorized_by: str
    reason: str
    outstanding_cents_at_authorization: int
    authorized_at: datetime


def _str(value: uuid.UUID | None) -> str | None:
    return str(value) if value is not None else None


def _payment_out(payment: InvoicePayment) -> PaymentOut:
    return PaymentOut(
        id=str(payment.id),
        invoice_id=_str(payment.invoice_id),
        retail_invoice_id=_str(payment.retail_invoice_id),
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


def refund_out(refund: InvoiceRefund) -> RefundOut:
    return RefundOut(
        id=str(refund.id),
        invoice_id=_str(refund.invoice_id),
        retail_invoice_id=_str(refund.retail_invoice_id),
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
        invoice_id=_str(auth.invoice_id),
        retail_invoice_id=_str(auth.retail_invoice_id),
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


async def _load_retail_invoice(db: SessionDep, invoice_id: uuid.UUID) -> RetailInvoice:
    invoice = await db.get(RetailInvoice, invoice_id, populate_existing=True)
    if invoice is None:
        raise HTTPException(status_code=404, detail="No such retail invoice.")
    return invoice


# --- recording payments (billing.view — checkout, #65's own precedent) ------------------------


@router.post("/{invoice_id}/payments", status_code=201)
async def record_payment(
    invoice_id: uuid.UUID, payload: RecordPaymentIn, actor: BillViewer, db: SessionDep
) -> PaymentOut:
    return await _record_payment(db, await _load_invoice(db, invoice_id), payload, actor)


async def lock_issued(db: AsyncSession, invoice: AnyInvoice, detail: str) -> list[uuid.UUID]:
    """`lock_lineage`, then re-read the invoice's status under that lock and 422 unless it is
    still issued — the check-then-insert every ledger write needs (R7). A cancel racing this
    call either commits first (and is seen here) or waits for this transaction. The 0064
    trigger refuses the same insert again in the database."""
    lineage = await lock_lineage(db, invoice.id, type(invoice))
    await db.refresh(invoice, ["status"])
    if invoice.status != "issued":
        raise HTTPException(status_code=422, detail=detail)
    return lineage


async def _record_payment(
    db: SessionDep, invoice: AnyInvoice, payload: RecordPaymentIn, actor: User
) -> PaymentOut:
    _check_payer(payload.payer_type, payload.method, payload.status)
    await lock_issued(db, invoice, "This invoice is not issued; payments cannot be recorded.")

    payment = InvoicePayment(
        **{_ledger_fk(invoice): invoice.id},
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
        target_type=_target_type(invoice),
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
        isinstance(invoice, Invoice)
        and invoice.package_purchase_id is not None
        and (await balance(db, invoice)).outstanding_cents <= 0
    ):
        purchase = await db.get(PackagePurchase, invoice.package_purchase_id)
        assert purchase is not None
        await activate_credits(db, purchase)
    await db.commit()
    requeue_receipts(invoice)
    return _payment_out(payment)


@router.get(
    "/{invoice_id}/payments",
    dependencies=[
        Depends(Requires("billing.view")),
        Depends(LogAccessOf("invoice_payments", "invoice_id", Invoice.customer_id)),
    ],
)
async def list_payments(
    invoice_id: uuid.UUID, _: BillViewer, db: SessionDep
) -> dict[str, list[PaymentOut] | list[PaymentTransferOut]]:
    return await _list_payments(db, await _load_invoice(db, invoice_id))


async def _list_payments(
    db: SessionDep, invoice: AnyInvoice
) -> dict[str, list[PaymentOut] | list[PaymentTransferOut]]:
    invoice_id = invoice.id
    payments = await db.scalars(
        select(InvoicePayment)
        .where(getattr(InvoicePayment, _ledger_fk(invoice)) == invoice_id)
        .order_by(InvoicePayment.recorded_at)
    )
    transfers = await db.scalars(
        select(InvoicePaymentTransfer)
        .where(_transfer_touches([invoice_id]))
        .order_by(InvoicePaymentTransfer.transferred_at)
    )
    return {
        "payments": [_payment_out(p) for p in payments],
        "transfers": [
            PaymentTransferOut(
                id=str(t.id),
                from_invoice_id=str(_transfer_from(t)),
                to_invoice_id=str(_transfer_to(t)),
                received_cents=t.received_cents,
                received_insurer_cents=t.received_insurer_cents,
                pending_insurer_cents=t.pending_insurer_cents,
                transferred_by=str(t.transferred_by),
                transferred_at=t.transferred_at,
            )
            for t in transfers
        ],
    }


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
    return await _correct_payment(db, invoice, payment_id, payload, actor)


async def _correct_payment(
    db: SessionDep,
    invoice: AnyInvoice,
    payment_id: uuid.UUID,
    payload: CorrectPaymentIn,
    actor: User,
) -> PaymentOut:
    fk = _ledger_fk(invoice)
    lineage = await lock_issued(
        db, invoice, "This invoice is not issued; its payment entries can no longer be corrected."
    )
    original = await db.get(InvoicePayment, payment_id)
    if original is None or getattr(original, fk) != invoice.id:
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
        **{fk: invoice.id},
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
        target_type=_target_type(invoice),
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
    requeue_receipts(invoice)
    return _payment_out(correction)


@router.post("/{invoice_id}/refunds", status_code=201)
async def refund(
    invoice_id: uuid.UUID, payload: RefundIn, actor: AdminReviewer, db: SessionDep
) -> RefundOut:
    invoice = await _load_invoice(db, invoice_id)
    if invoice.package_purchase_id is not None:
        # R13: a package's money leaves only through #73 (standard zero-use refund, or the
        # admin exception with its explicit credit/commission choices) — never this route.
        raise HTTPException(
            status_code=422,
            detail=(
                "Package purchases are refunded through "
                f"POST /api/packages/purchases/{invoice.package_purchase_id}/refund."
            ),
        )
    created = await record_refund(
        db, invoice, amount_cents=payload.amount_cents, reason=payload.reason, approver=actor
    )
    await db.commit()
    requeue_receipts(invoice)
    return refund_out(created)


@router.get(
    "/{invoice_id}/refunds",
    dependencies=[
        Depends(Requires("billing.view")),
        Depends(LogAccessOf("invoice_refunds", "invoice_id", Invoice.customer_id)),
    ],
)
async def list_refunds(
    invoice_id: uuid.UUID, _: BillViewer, db: SessionDep
) -> dict[str, list[RefundOut]]:
    return await _list_refunds(db, await _load_invoice(db, invoice_id))


async def _list_refunds(db: SessionDep, invoice: AnyInvoice) -> dict[str, list[RefundOut]]:
    refunds = await db.scalars(
        select(InvoiceRefund)
        .where(getattr(InvoiceRefund, _ledger_fk(invoice)) == invoice.id)
        .order_by(InvoiceRefund.refunded_at)
    )
    return {"refunds": [refund_out(r) for r in refunds]}


# --- authorizing the outstanding-balance exception (billing.manage, Admin Mode) ----------------


@router.post("/{invoice_id}/balance-exceptions", status_code=201)
async def authorize_outstanding_balance(
    invoice_id: uuid.UUID, payload: AuthorizeBalanceIn, actor: AdminReviewer, db: SessionDep
) -> BalanceAuthorizationOut:
    return await _authorize(db, await _load_invoice(db, invoice_id), payload, actor)


async def _authorize(
    db: SessionDep, invoice: AnyInvoice, payload: AuthorizeBalanceIn, actor: User
) -> BalanceAuthorizationOut:
    await lock_issued(db, invoice, "This invoice is not issued; there is nothing to authorize.")
    outstanding = (await balance(db, invoice)).client_outstanding_cents
    if outstanding <= 0:
        raise HTTPException(
            status_code=422, detail="This invoice has no outstanding client balance to authorize."
        )

    authorization = InvoiceBalanceAuthorization(
        **{_ledger_fk(invoice): invoice.id},
        authorized_by=actor.id,
        reason=payload.reason,
        outstanding_cents_at_authorization=outstanding,
    )
    db.add(authorization)
    await db.flush()
    record_event(
        db,
        "invoice.balance_exception_authorized",
        target_type=_target_type(invoice),
        target_id=str(invoice.id),
        actor_user_id=actor.id,
        metadata={"outstanding_cents": outstanding, "reason": payload.reason},
    )
    await db.commit()
    requeue_receipts(invoice)
    return _authorization_out(authorization)


@router.get(
    "/{invoice_id}/balance-exceptions",
    dependencies=[
        Depends(Requires("billing.view")),
        Depends(LogAccessOf("invoice_balance_exceptions", "invoice_id", Invoice.customer_id)),
    ],
)
async def list_balance_exceptions(
    invoice_id: uuid.UUID, _: BillViewer, db: SessionDep
) -> dict[str, list[BalanceAuthorizationOut]]:
    return await _list_exceptions(db, await _load_invoice(db, invoice_id))


async def _list_exceptions(
    db: SessionDep, invoice: AnyInvoice
) -> dict[str, list[BalanceAuthorizationOut]]:
    authorizations = await db.scalars(
        select(InvoiceBalanceAuthorization)
        .where(getattr(InvoiceBalanceAuthorization, _ledger_fk(invoice)) == invoice.id)
        .order_by(InvoiceBalanceAuthorization.authorized_at)
    )
    return {"exceptions": [_authorization_out(a) for a in authorizations]}


# --- the same ledger, for retail invoices (#76; balance exceptions: M4 review R8) ---------------


@retail_router.post("/{invoice_id}/balance-exceptions", status_code=201)
async def authorize_retail_outstanding_balance(
    invoice_id: uuid.UUID, payload: AuthorizeBalanceIn, actor: AdminReviewer, db: SessionDep
) -> BalanceAuthorizationOut:
    return await _authorize(db, await _load_retail_invoice(db, invoice_id), payload, actor)


@retail_router.get(
    "/{invoice_id}/balance-exceptions",
    dependencies=[
        Depends(Requires("billing.view")),
        Depends(
            LogAccessOf(
                "retail_invoice_balance_exceptions", "invoice_id", RetailInvoice.customer_id
            )
        ),
    ],
)
async def list_retail_balance_exceptions(
    invoice_id: uuid.UUID, _: BillViewer, db: SessionDep
) -> dict[str, list[BalanceAuthorizationOut]]:
    return await _list_exceptions(db, await _load_retail_invoice(db, invoice_id))


@retail_router.post("/{invoice_id}/payments", status_code=201)
async def record_retail_payment(
    invoice_id: uuid.UUID, payload: RecordPaymentIn, actor: BillViewer, db: SessionDep
) -> PaymentOut:
    return await _record_payment(db, await _load_retail_invoice(db, invoice_id), payload, actor)


@retail_router.get(
    "/{invoice_id}/payments",
    dependencies=[
        Depends(Requires("billing.view")),
        Depends(LogAccessOf("retail_invoice_payments", "invoice_id", RetailInvoice.customer_id)),
    ],
)
async def list_retail_payments(
    invoice_id: uuid.UUID, _: BillViewer, db: SessionDep
) -> dict[str, list[PaymentOut] | list[PaymentTransferOut]]:
    return await _list_payments(db, await _load_retail_invoice(db, invoice_id))


@retail_router.post("/{invoice_id}/payments/{payment_id}/corrections", status_code=201)
async def correct_retail_payment(
    invoice_id: uuid.UUID,
    payment_id: uuid.UUID,
    payload: CorrectPaymentIn,
    actor: BillViewer,
    db: SessionDep,
) -> PaymentOut:
    invoice = await _load_retail_invoice(db, invoice_id)
    return await _correct_payment(db, invoice, payment_id, payload, actor)


@retail_router.post("/{invoice_id}/refunds", status_code=201)
async def retail_refund(
    invoice_id: uuid.UUID, payload: RefundIn, actor: AdminReviewer, db: SessionDep
) -> RefundOut:
    invoice = await _load_retail_invoice(db, invoice_id)
    created = await record_refund(
        db, invoice, amount_cents=payload.amount_cents, reason=payload.reason, approver=actor
    )
    await db.commit()
    return refund_out(created)


@retail_router.get(
    "/{invoice_id}/refunds",
    dependencies=[
        Depends(Requires("billing.view")),
        Depends(LogAccessOf("retail_invoice_refunds", "invoice_id", RetailInvoice.customer_id)),
    ],
)
async def list_retail_refunds(
    invoice_id: uuid.UUID, _: BillViewer, db: SessionDep
) -> dict[str, list[RefundOut]]:
    return await _list_refunds(db, await _load_retail_invoice(db, invoice_id))
