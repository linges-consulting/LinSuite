"""Trigger functions: the one place a domain event turns into a queued notification
(Phase 12 Task 5, #11).

One function per notification type — `notify_booking_confirmed`, `notify_booking_modified`,
`notify_booking_cancelled`, `notify_form_link_issued`, `notify_package_notice` — each a plain
async function taking `db` plus the domain object the event is about. Every one of them:

1. Resolves the client's channel(s): email if `email_ready(business)` (Task 2's gate — "email
   features remain disabled behind a banner until a test send succeeds"), sms if
   `business.sms_enabled` and `sms_ready(business)` (Task 3's gate — "disabling SMS leaves no
   code path attempting to send it"). Both gates are read here, once, in `dispatch` — nothing
   downstream needs to re-check either.
2. Renders the right `(notification_type, channel)` template row through Task 1's `render()`.
3. Enqueues over Task 2/4's Celery tasks (`send_email.delay`/`send_sms.delay`), passing
   `customer_id`/`notification_type` through so a permanent failure surfaces on the profile
   (Task 4).

`load_business`, `template_for`, `dispatch` and `appointment_context` are not private to this
module: `notifications/reminders.py`'s scheduler is not a sixth *event* (a reminder is due at
a moment nothing observes, not raised by one), so it calls this same dispatch surface directly
rather than this file growing a `notify_reminder` that pretends otherwise.

**The booking triggers' real callers are all in `scheduling/public.py`** (Phase 6, Tasks 2-3,
#10): `notify_booking_confirmed` from `book_public`, `notify_booking_modified` from
`manage_reschedule`, `notify_booking_cancelled` from `manage_cancel` — the client-facing
booking portal, never the staff booking endpoint (`scheduling/appointments.py` is explicitly
not gaining a new notification side effect here; see that module's own docstring warning).
`notify_form_link_issued` **is** wired too — `forms/links.py`'s issue endpoint is on `main`,
so there is a real caller today.
`notify_package_notice` has no caller anywhere in this codebase (packages/billing is M4); it
exists and is tested in isolation so that milestone has a function to call rather than a stub
to write around.
"""

import uuid
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.models import Business
from customers.models import Customer
from forms.models import FormLink, FormTemplateVersion
from notifications.models import NotificationTemplate
from notifications.providers import email_ready, sms_ready
from notifications.render import render
from notifications.tasks import send_email, send_sms
from scheduling.models import Appointment


async def load_business(db: AsyncSession) -> Business:
    """The one business row (CLAUDE.md: single-tenant, `CHECK id = 1`)."""
    business = await db.get(Business, 1)
    assert business is not None, "businesses has no row — setup was never completed"
    return business


async def template_for(
    db: AsyncSession, notification_type: str, channel: str
) -> NotificationTemplate | None:
    return await db.scalar(
        select(NotificationTemplate).where(
            NotificationTemplate.notification_type == notification_type,
            NotificationTemplate.channel == channel,
        )
    )


async def dispatch(
    db: AsyncSession,
    business: Business,
    notification_type: str,
    *,
    customer_id: uuid.UUID | str,
    email: str | None,
    phone: str | None,
    context: dict[str, str],
) -> None:
    """Render and enqueue over every channel the client can be reached on and the business has
    turned on and configured. A missing template row, a client with no address/number on that
    channel, or the business not being ready on it are all silent no-ops here — never an
    error, since a trigger fires as a side effect of something that already succeeded (a
    booking, a form link) and must not fail it."""
    customer_id_str = str(customer_id)
    if email:
        template = await template_for(db, notification_type, "email")
        if template is not None and email_ready(business):
            subject = render(template.subject_template or "", context)
            body = render(template.body_template, context)
            send_email.delay(
                email,
                subject,
                body,
                customer_id=customer_id_str,
                notification_type=notification_type,
            )
    if phone and business.sms_enabled:
        template = await template_for(db, notification_type, "sms")
        if template is not None and sms_ready(business):
            body = render(template.body_template, context)
            send_sms.delay(
                phone, body, customer_id=customer_id_str, notification_type=notification_type
            )


def appointment_context(business: Business, appointment: Appointment) -> dict[str, str]:
    """The merge fields migration 0032 seeded every booking-related template with
    (`$business_name`, `$client_name`, `$service_name`, `$staff_name`, `$appointment_time`),
    read off the relationships `Appointment` already loads eagerly (`lazy="joined"`) — no
    extra query. `appointment_time` is rendered in the business's own timezone, never UTC —
    the same "read local, at the boundary" rule as everything else this app shows a client."""
    zone = ZoneInfo(business.timezone)
    local_start = appointment.starts_at.astimezone(zone)
    client_name = f"{appointment.customer.first_name} {appointment.customer.last_name}".strip()
    return {
        "business_name": business.name,
        "client_name": client_name,
        "service_name": appointment.service.name,
        "staff_name": appointment.staff.display_name,
        "appointment_time": local_start.strftime("%A, %B %d, %Y at %I:%M %p"),
    }


async def _notify_appointment(
    db: AsyncSession, appointment: Appointment, notification_type: str
) -> None:
    business = await load_business(db)
    await dispatch(
        db,
        business,
        notification_type,
        customer_id=appointment.customer_id,
        email=appointment.customer.email,
        phone=appointment.customer.phone,
        context=appointment_context(business, appointment),
    )


async def notify_booking_confirmed(db: AsyncSession, appointment: Appointment) -> None:
    await _notify_appointment(db, appointment, "booking_confirmation")


async def notify_booking_modified(db: AsyncSession, appointment: Appointment) -> None:
    await _notify_appointment(db, appointment, "modification")


async def notify_booking_cancelled(db: AsyncSession, appointment: Appointment) -> None:
    await _notify_appointment(db, appointment, "cancellation")


async def notify_form_link_issued(
    db: AsyncSession,
    link: FormLink,
    customer: Customer,
    version: FormTemplateVersion,
    url: str,
) -> None:
    """`forms/links.py::issue_link` already has `customer` and `version` loaded (it needed
    both to build the link itself) — taking them here is one fewer pair of queries than
    re-fetching off `link.customer_id`/`link.version_id`. `version` isn't read yet (no
    per-form template variant), but is accepted now rather than added to the signature later
    the day a `$form_name` merge field is wanted."""
    business = await load_business(db)
    context = {
        "business_name": business.name,
        "client_name": f"{customer.first_name} {customer.last_name}".strip(),
        "form_link": url,
    }
    await dispatch(
        db,
        business,
        "form_link",
        customer_id=link.customer_id,
        email=customer.email,
        phone=customer.phone,
        context=context,
    )


async def notify_package_notice(
    db: AsyncSession, customer: Customer, *, package_name: str, remaining: int
) -> None:
    """No caller anywhere in this codebase yet — packages/billing is M4, not built. A minimal
    signature (a customer, a package name, a remaining-session count) rather than a package
    domain model this milestone has no business inventing; M4 either calls this as-is or grows
    it once a real `Package`/`PackageCredit` row exists to pass instead of these two scalars."""
    business = await load_business(db)
    context = {
        "business_name": business.name,
        "client_name": f"{customer.first_name} {customer.last_name}".strip(),
        "package_name": package_name,
        "package_remaining": str(remaining),
    }
    await dispatch(
        db,
        business,
        "package_notice",
        customer_id=customer.id,
        email=customer.email,
        phone=customer.phone,
        context=context,
    )
