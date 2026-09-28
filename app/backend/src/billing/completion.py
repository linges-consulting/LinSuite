"""The second hook on `scheduling/appointments.py::complete_appointment` (#59), the same
shape as `_clear_queue_entry` (M3 Phase 7): called once, right before that transaction's one
commit, never a transaction of its own.

Not a router — nothing here is an endpoint. `complete_appointment` already holds the
appointment (and, for a group, every sibling) locked `FOR UPDATE` before it ever reaches
`completed`, and a retried request on an already-`completed` row never gets past that
function's own status guard to call this at all. So the one-line-per-appointment guarantee is
inherited from that lock, not re-implemented here — see `ServiceBillLine`'s docstring.
"""

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import func

from billing.models import ServiceBill, ServiceBillLine
from scheduling.models import Appointment


async def record_draft_bill_line(
    db: AsyncSession, appointment: Appointment, *, prepaid_cents: int | None = None
) -> None:
    """Finds the visit's open draft by `booking_group_id` and appends to it, or starts a new
    one — for an ungrouped appointment (`booking_group_id is None`) that is always a fresh
    bill, since there is no shared tag for a second appointment to ever match it by. A visit
    whose existing bill has moved past `draft` (#65 sets that) is not matched either: the
    `status == "draft"` filter is what makes a later sibling completion open a new draft
    rather than append to a document that's supposed to be immutable once issued.

    `prepaid_cents` (#72): the redeemed credit's frozen session value — the line's price
    becomes that value and is marked settled by it, instead of the appointment's own price."""
    bill_id = None
    if appointment.booking_group_id is not None:
        bill_id = await db.scalar(
            select(ServiceBill.id).where(
                ServiceBill.booking_group_id == appointment.booking_group_id,
                ServiceBill.status == "draft",
            )
        )
    if bill_id is None:
        bill = ServiceBill(
            customer_id=appointment.customer_id,
            booking_group_id=appointment.booking_group_id,
        )
        db.add(bill)
        await db.flush()
        bill_id = bill.id
    else:
        # #64: a sibling appointment completing onto an *existing* draft changes what a
        # pending staff override request (`bill_override_requests`) was reviewed against —
        # bump `updated_at` with a plain `UPDATE`, no need to load the row, so
        # `bill_authority.py`'s stale-approval guard sees this the same way it already sees a
        # discount-selection change (`bill_review.py::apply_discounts`). A brand-new bill
        # (the `if` branch above) needs no bump: its own `server_default=func.now()` already
        # postdates every request, since nothing could have targeted a bill before it existed.
        await db.execute(
            update(ServiceBill).where(ServiceBill.id == bill_id).values(updated_at=func.now())
        )
    db.add(
        ServiceBillLine(
            bill_id=bill_id,
            appointment_id=appointment.id,
            service_id=appointment.service_id,
            staff_id=appointment.staff_id,
            price_cents=appointment.price_cents if prepaid_cents is None else prepaid_cents,
            prepaid_cents=prepaid_cents or 0,
            commission_rate_bp=appointment.staff.commission_rate_services_bp,
        )
    )
