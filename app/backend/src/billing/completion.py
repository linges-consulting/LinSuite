"""The second hook on `scheduling/appointments.py::complete_appointment` (#59), the same
shape as `_clear_queue_entry` (M3 Phase 7): called once, right before that transaction's one
commit, never a transaction of its own.

Not a router — nothing here is an endpoint. `complete_appointment` already holds the
appointment (and, for a group, every sibling) locked `FOR UPDATE` before it ever reaches
`completed`, and a retried request on an already-`completed` row never gets past that
function's own status guard to call this at all. So the one-line-per-appointment guarantee is
inherited from that lock, not re-implemented here — see `ServiceBillLine`'s docstring.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from billing.models import ServiceBill, ServiceBillLine
from scheduling.models import Appointment


async def record_draft_bill_line(db: AsyncSession, appointment: Appointment) -> None:
    """Finds the visit's open draft by `booking_group_id` and appends to it, or starts a new
    one — for an ungrouped appointment (`booking_group_id is None`) that is always a fresh
    bill, since there is no shared tag for a second appointment to ever match it by. A visit
    whose existing bill has moved past `draft` (#65 sets that) is not matched either: the
    `status == "draft"` filter is what makes a later sibling completion open a new draft
    rather than append to a document that's supposed to be immutable once issued."""
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
    db.add(
        ServiceBillLine(
            bill_id=bill_id,
            appointment_id=appointment.id,
            service_id=appointment.service_id,
            staff_id=appointment.staff_id,
            price_cents=appointment.price_cents,
            commission_rate_bp=appointment.staff.commission_rate_services_bp,
        )
    )
