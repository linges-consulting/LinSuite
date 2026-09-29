"""Commission ledger writes shared by service issue/cancel, package refunds and retail
issue/cancel/return (#69, M4 review T5). `billing/commission.py` stays the pure arithmetic;
this module is the part that reads and appends `commission_postings`.

**Never double-earn (R28).** Every correction is a dated `reversal` naming the `earned` row it
corrects, and the reversals naming one row never sum past it — so a cancel/reissue pair nets
to one earning (the cancel reverses the original, the replacement earns afresh), and a package
refund's "reverse commission" choice is persisted on its refund row
(`InvoiceRefund.reverses_commission`) so a session redeemed from that package never earns when
its bill issues later or is reissued (`forfeited_appointments`).

**Retail returns reverse proportionally (R27).** Returning `r` of a line's `q` units reverses
`r/q` of what that line earned, cumulatively and half-up: after all returns the line's reversed
total is `round(earned * returned / q)`, so a full return nets to exactly zero and several
partial returns never drift. The rule follows the goods, not the money: a return with no
refund (an exchange, store goodwill) still undoes the sale of those units, and a refund with
no return (a price adjustment) reverses nothing — commission is on the sale that stands.
"""

import uuid
from collections.abc import Iterable

from sqlalchemy import ColumnElement, exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from billing.models import CommissionPosting, Invoice, InvoiceRefund, PackageCreditRedemption


def _half_up(numerator: int, denominator: int) -> int:
    """`numerator / denominator` rounded half-up, for non-negative integers."""
    return (2 * numerator + denominator) // (2 * denominator)


async def reverse_commission(
    db: AsyncSession, *where: ColumnElement[bool], share: tuple[int, int] = (1, 1)
) -> int:
    """Bring every matching `earned` posting's cumulative reversal up to `share` (returned /
    sold; `(1, 1)` = reverse whatever is left) with one dated `reversal` row each — posted
    today, never backdated (#69). A posting already reversed that far gets nothing, so #68's
    cancel and #73's refund reverse a line at most once between them. Returns how many rows it
    posted; does not commit."""
    num, den = share
    reversal = aliased(CommissionPosting)
    mine = reversal.reverses_posting_id == CommissionPosting.id
    done_amount = (
        select(func.coalesce(func.sum(reversal.amount_cents), 0)).where(mine).scalar_subquery()
    )
    done_basis = (
        select(func.coalesce(func.sum(reversal.basis_cents), 0)).where(mine).scalar_subquery()
    )
    rows = await db.execute(
        select(CommissionPosting, done_amount, done_basis, exists().where(mine)).where(
            CommissionPosting.kind == "earned", *where
        )
    )
    posted = 0
    for posting, reversed_amount, reversed_basis, touched in rows.all():
        amount = max(_half_up(posting.amount_cents * num, den) + reversed_amount, 0)
        basis = max(_half_up(posting.basis_cents * num, den) - reversed_basis, 0)
        # A zero-rate full reversal still posts once (it marks the line reversed); anything
        # already reversed to this share, or a partial share rounding to nothing, posts nothing.
        if amount == 0 and basis == 0 and (touched or num < den):
            continue
        db.add(
            CommissionPosting(
                invoice_line_id=posting.invoice_line_id,
                invoice_id=posting.invoice_id,
                retail_invoice_line_id=posting.retail_invoice_line_id,
                retail_invoice_id=posting.retail_invoice_id,
                staff_id=posting.staff_id,
                kind="reversal",
                commission_rate_bp=posting.commission_rate_bp,
                basis_cents=basis,
                amount_cents=-amount,
                reverses_posting_id=posting.id,
            )
        )
        posted += 1
    return posted


async def forfeited_appointments(
    db: AsyncSession, appointment_ids: Iterable[uuid.UUID]
) -> set[uuid.UUID]:
    """Appointments whose redeemed package was refunded with "reverse commission" chosen —
    their delivery never earns, however late (or often) its bill is issued (R28)."""
    ids = list(appointment_ids)
    if not ids:
        return set()
    redemption = PackageCreditRedemption
    return set(
        await db.scalars(
            select(redemption.appointment_id)
            .join(Invoice, Invoice.package_purchase_id == redemption.package_purchase_id)
            .join(InvoiceRefund, InvoiceRefund.invoice_id == Invoice.id)
            .where(
                redemption.appointment_id.in_(ids),
                InvoiceRefund.reverses_commission,
            )
        )
    )
