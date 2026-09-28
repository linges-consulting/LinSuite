"""Admin-only commission report (#69, M4 spec #54 stories 97-100): what every staff member has
earned in commission, separate from whether it has been paid, filterable by date range and
staff, exportable as CSV.

**`commission.view`, Administrator-only, Admin Mode.** `audit.view`'s own shape (0022): a
report that names a staff member and what they earned is exactly the kind of thing that
capability's own docstring already argues belongs behind the short window. `m4.md`'s own note
already suggested this exact key name rather than a near-duplicate of `billing.manage`.

**Reads only the frozen `commission_postings` ledger** (`billing/models.py`'s `## commission
posting + report (#69)` section), joined to `Staff`/`Invoice`/`InvoiceLine` for display context
only (name, invoice number, service) — never recomputes a commission amount from `Staff.
commission_rate_services_bp` or anything else live. Every number here is exactly what `billing
/invoices.py::issue_invoice` posted (CLAUDE.md "Commission earns on delivery... rates are
snapshotted... reporting only").

**"Received/pending" is best-effort, not #66's real answer.** No payment ledger exists yet —
#66 (manual payment ledger + checkout gate) is running in parallel, in this same wave, and may
not be merged. `Invoice` (#65) carries no payment field at all, only `status IN ('issued',
'cancelled')`. So this report can only ever report a posting against a non-cancelled invoice as
`"pending"`, and one against a cancelled invoice (unreachable today — #65 built no `/cancel`
route yet) as `"voided"`; there is no way, with what exists right now, to ever report
`"received"`. `_payment_status` is the one place this simplification lives — once #66 lands,
replace its body with a real join against the payment ledger; nothing else in this file should
need to change. Flagged here for reconciliation once that happens.
"""

import csv
import io
import uuid
from datetime import date, datetime, time, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import Select, select

from auth.capabilities import Requires
from billing.models import CommissionPosting, Invoice, InvoiceLine
from core.db import SessionDep
from scheduling._admin_forms import refuse
from scheduling.clock import localize
from scheduling.models import Staff
from scheduling.time_off import business_zone

router = APIRouter(prefix="/admin/reports", tags=["billing"])

DEFAULT_DAYS = 90
PaymentStatus = Literal["pending", "voided"]


# --- what goes over the wire -----------------------------------------------------------------


class CommissionRowOut(BaseModel):
    id: str
    posted_at: datetime
    kind: str
    staff_id: str
    staff_name: str
    invoice_id: str
    invoice_number: int
    service_id: str
    commission_rate_bp: int
    basis_cents: int
    amount_cents: int
    # Best-effort — see the module docstring's "received/pending" note.
    payment_status: PaymentStatus


class CommissionReportOut(BaseModel):
    rows: list[CommissionRowOut]
    total_earned_cents: int
    # Always 0/the whole total until #66 lands — see the module docstring.
    total_received_cents: int
    total_pending_cents: int
    from_: date = Field(serialization_alias="from")
    to: date
    timezone: str


def _payment_status(invoice_status: str) -> PaymentStatus:
    """The one place the "no payment ledger yet" simplification lives — replace this body,
    nothing else, once #66 exists."""
    return "voided" if invoice_status == "cancelled" else "pending"


def _window_query(start: datetime, end: datetime, staff_id: uuid.UUID | None) -> Select:
    conditions = [CommissionPosting.posted_at >= start, CommissionPosting.posted_at < end]
    if staff_id is not None:
        conditions.append(CommissionPosting.staff_id == staff_id)
    return (
        select(
            CommissionPosting,
            Staff.display_name,
            Invoice.invoice_number,
            Invoice.status,
            InvoiceLine.service_id,
        )
        .join(Staff, Staff.id == CommissionPosting.staff_id)
        .join(Invoice, Invoice.id == CommissionPosting.invoice_id)
        .join(InvoiceLine, InvoiceLine.id == CommissionPosting.invoice_line_id)
        .where(*conditions)
        .order_by(CommissionPosting.posted_at.desc())
    )


async def _rows(
    db: SessionDep, from_: date | None, to: date | None, staff_id: uuid.UUID | None
) -> tuple[list[CommissionRowOut], date, date, str]:
    zone = await business_zone(db)
    to = to or datetime.now(zone).date()
    from_ = from_ or to - timedelta(days=DEFAULT_DAYS)
    if to < from_:
        raise refuse("to", "The last day cannot come before the first.", where="query")
    start = localize(datetime.combine(from_, time.min), zone)
    end = localize(datetime.combine(to + timedelta(days=1), time.min), zone)

    result = await db.execute(_window_query(start, end, staff_id))
    rows = [
        CommissionRowOut(
            id=str(posting.id),
            posted_at=posting.posted_at,
            kind=posting.kind,
            staff_id=str(posting.staff_id),
            staff_name=staff_name,
            invoice_id=str(posting.invoice_id),
            invoice_number=invoice_number,
            service_id=str(service_id),
            commission_rate_bp=posting.commission_rate_bp,
            basis_cents=posting.basis_cents,
            amount_cents=posting.amount_cents,
            payment_status=_payment_status(invoice_status),
        )
        for posting, staff_name, invoice_number, invoice_status, service_id in result.all()
    ]
    return rows, from_, to, zone.key


# --- reading -----------------------------------------------------------------------------------


@router.get("/commission", dependencies=[Depends(Requires("commission.view"))])
async def commission_report(
    db: SessionDep,
    from_: Annotated[date | None, Query(alias="from")] = None,
    to: date | None = None,
    staff_id: uuid.UUID | None = None,
) -> CommissionReportOut:
    rows, resolved_from, resolved_to, tz = await _rows(db, from_, to, staff_id)
    # `amount_cents` is already signed (positive "earned", negative "reversal") — summing it
    # is the net standing for the window, the same convention any append-only ledger reads.
    total_earned = sum(r.amount_cents for r in rows)
    return CommissionReportOut(
        rows=rows,
        total_earned_cents=total_earned,
        total_received_cents=0,
        total_pending_cents=total_earned,
        from_=resolved_from,
        to=resolved_to,
        timezone=tz,
    )


_CSV_COLUMNS = (
    "posted_at",
    "kind",
    "staff_id",
    "staff_name",
    "invoice_id",
    "invoice_number",
    "service_id",
    "commission_rate_bp",
    "basis_cents",
    "amount_cents",
    "payment_status",
)


@router.get("/commission/csv", dependencies=[Depends(Requires("commission.view"))])
async def commission_report_csv(
    db: SessionDep,
    from_: Annotated[date | None, Query(alias="from")] = None,
    to: date | None = None,
    staff_id: uuid.UUID | None = None,
) -> Response:
    rows, resolved_from, resolved_to, _tz = await _rows(db, from_, to, staff_id)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(_CSV_COLUMNS)
    for r in rows:
        writer.writerow(
            [
                r.posted_at.isoformat() if column == "posted_at" else getattr(r, column)
                for column in _CSV_COLUMNS
            ]
        )
    filename = f"commission_{resolved_from.isoformat()}_{resolved_to.isoformat()}.csv"
    return Response(
        content=buffer.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
