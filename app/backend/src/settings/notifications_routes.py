"""Settings → Notifications: sender, SMS, templates, reminder intervals (Phase 12 Task 6, #11).

A sibling of `settings/routes.py` rather than a new section in it: that file is already the
profile/timezone/branding/security surface, and this one alone covers a sender picker, two
credential sets, a template per `(notification_type, channel)` and the reminder schedule —
enough fields on its own to be its own file, per m3.md's own note that this router may need to
split out. Same router shape as the Security panel: one router at the `/admin` prefix, gated by
the same `Requires("admin")` (an administrative capability, not a new key — reading or changing
how this business sends messages is exactly what "administer the business" already means, and
`auth/capabilities.py`'s own rule is "a capability with no route behind it is a promise the
server does not keep" — there is no route here that needs finer granularity than the rest of
this prefix already has).

**Everything Tasks 1-5 built, made configurable and honestly readable — nothing new dispatched.**
`email_ready`/`sms_ready` (`notifications/providers.py`) are read straight through into the GET
response, unfiltered, so "no sender configured" is a fact the panel states plainly rather than
inferring from which fields happen to be filled in (#11's own acceptance criterion). Nothing
here calls a trigger function or the reminder scanner.

**Write-only secrets.** `resend_api_key`, `smtp_password` and `twilio_auth_token` are never read
back — the GET response reports only whether one is stored (`*_set`), the same shape MFA's
TOTP secret already uses (enrolled, never re-displayed). A PATCH field left out (`None`) means
"leave the stored value alone"; an admin who wants SMS on but is not rotating the Twilio token
does not have to retype it. The three secrets are the only fields with that "None = untouched"
meaning by *necessity* — everything else uses it too, for the same one-endpoint-many-fields
reason `settings/routes.py::SecurityChange` does, but a blank string still clears an optional
text field (`_blank_to_none`, copied rather than imported: it is `routes.py`-private, and two
characters of duplication beats reaching across a module boundary for one).

**A credential edit un-verifies its sender.** Rotating the Resend key or repointing the SMTP
host resets that sender's `*_verified_at` to null — the smallest-reasonable-call this task adds
beyond what was asked: without it, the banner could keep reporting "ready" against a key nobody
has actually tested since it changed, which is the same silent-failure shape #11's acceptance
criterion exists to rule out, just one step later than "unconfigured".

**The test-send actions are the only way `*_verified_at` is ever set — and, since #116, the only
way a *failed* one clears it again.** A credential edit already un-verifies a sender (below); a
test send that fails against unchanged credentials now does the same, so "a sender is configured
and its latest test send succeeded" (the onboarding checklist's and the email banner's own
wording) is never answered from a stale success sitting behind a since-failed retry.

**The test-send actions are the only way `*_verified_at` is ever set**, and they call the real
adapter synchronously (`run_in_threadpool`, the same pattern `core/security.py` uses for a
blocking Argon2 hash from an async handler — `ResendProvider`/`SmtpProvider`/`TwilioProvider`
are synchronous `httpx.Client`/`smtplib` calls, not `httpx.AsyncClient`), not through
`notifications/tasks.py`: an admin pressing "send test" wants to know *now* whether it worked,
not a queued retry three times over the next two minutes. `NOTIFICATION_PROVIDER=recording`
still wins in tests (`get_provider`/`get_sms_provider` below), so the suite never dials a real
API — see `notifications/providers.py`'s docstring for the one-line fix this task made there.

**SMS has no verified concept to ungate.** `sms_ready()` (Task 3) already treats SMS as ready as
soon as it is enabled and configured — there is no `twilio_verified_at` column, and adding one
now would mean also changing `sms_ready()`'s tested semantics, which is Task 3/5's completed
work, not this task's. "Send test SMS" still performs a real send (the adapter is exercised for
real, same as email), it just has no flag left to flip afterwards; the response is only
success-or-error.

**Templates are their own sub-resource.** Twelve rows (six `notification_type`s × two
`channel`s, not six — the GET response's `merge_fields` per type is read off Task 1's actual
seeded bodies, not invented), each with its own id, so each is edited with its own
`PUT .../templates/{id}` rather than folded into the one big PATCH above. The settings screen's
single Save button still reads as one action to an administrator; it is free to issue several
requests to get there.

**Booking-portal policy (Phase 6 Task 4, #10) — a new section in this same panel, not a
second one** (m3.md's owner ruling: portal policy and notifications share one settings
surface). `online_booking_enabled` and the two daily-cap numbers are new columns (migration
0039); `online_cancellation_enabled`/`cancellation_cutoff_hours` are Task 3's existing columns,
made editable here for the first time — Task 3 could only flip them with a raw `UPDATE`
because no admin endpoint existed yet. All five are read at the API layer in
`scheduling/public.py`, not merely hidden in the frontend when off (#10's own acceptance
criterion) — this endpoint only writes the value; `scheduling/public.py` is where each one is
actually enforced. Same PATCH shape as everything else here: omitted means untouched, and a
real change is diffed into the one `record_event` call the endpoint already makes.

**`enable_walk_in_queue` (Phase 7 Task 1, #12) — one more field in this same panel**, per the
layout Task 4 above already reserved for it. Unlike the booking-portal fields, there is no
enforcement anywhere yet to wire this into: Tasks 2/8 (queue CRUD, the nav entry, the display
screen) haven't been built, so this PATCH only makes the toggle itself exist and be readable —
"with the queue disabled, no queue surface exists anywhere in the product" holds trivially
right now, since no surface exists yet regardless of this flag's value.
"""

import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import AfterValidator, BaseModel, BeforeValidator, EmailStr, Field
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from auth.capabilities import Requires
from auth.models import User
from core.audit import record_event
from core.db import SessionDep
from core.models import Business
from notifications.credentials import encrypt_credential
from notifications.models import NotificationTemplate
from notifications.providers import email_ready, get_provider, get_sms_provider, sms_ready

AdminCapability = Annotated[User, Depends(Requires("admin"))]

router = APIRouter(prefix="/admin", tags=["settings"], dependencies=[Depends(Requires("admin"))])


# --- field types ------------------------------------------------------------------------------


def _blank_to_none(value: object) -> object:
    """A text input cleared to "" is absence, not a shorter value — same rule
    `settings/routes.py` applies to the profile, copied rather than imported (that one is
    module-private)."""
    return None if isinstance(value, str) and not value.strip() else value


OptionalText = Annotated[str | None, BeforeValidator(_blank_to_none)]
OptionalEmail = Annotated[EmailStr | None, BeforeValidator(_blank_to_none)]


def _valid_intervals(value: list[int]) -> list[int]:
    if not value:
        raise ValueError("Set at least one reminder interval.")
    if len(value) != len(set(value)):
        raise ValueError("Reminder intervals must not repeat.")
    if any(hours <= 0 for hours in value):
        raise ValueError("Reminder intervals must be positive numbers of hours.")
    # A month out is already generous for an appointment reminder; the booking horizon itself
    # is capped at a year (`BusinessProfile.booking_horizon_days`), so this is not that cap —
    # just a sanity ceiling against a typo like 2400 (a hundred days) reaching a client.
    if any(hours > 720 for hours in value):
        raise ValueError("A reminder interval of more than 30 days (720 hours) is not allowed.")
    return value


ReminderIntervals = Annotated[list[int], AfterValidator(_valid_intervals)]


def _sane_cutoff_hours(value: int) -> int:
    # Same sanity-ceiling reasoning as `_valid_intervals`'s 720-hour cap, scaled to a
    # cancellation policy: a year's notice is already far past what any real policy needs,
    # and the DB's own CHECK (migration 0038) only rules out negative numbers.
    if value > 8760:
        raise ValueError("A cancellation cutoff of more than 365 days (8760 hours) is not allowed.")
    return value


CancellationCutoffHours = Annotated[int, Field(ge=0), AfterValidator(_sane_cutoff_hours)]
# Same defaults as the `_DAILY_CAP_PER_IP`/`_DAILY_CAP_PER_EMAIL` constants these columns
# replace (`scheduling/public.py`); the upper bound is a sanity ceiling against a typo, not a
# policy limit — 1000 bookings a day from one address or one email is already implausible.
DailyCap = Annotated[int, Field(ge=1, le=1000)]

# The merge fields each notification type's seeded body (migration 0032) actually uses — shown
# beside that type's templates so an administrator editing them knows what `$identifier`s
# render, without reading `notifications/render.py`'s `safe_substitute` behaviour to find out
# an unrecognised one is merely left literal rather than explained on screen.
MERGE_FIELDS: dict[str, list[str]] = {
    "booking_confirmation": [
        "$business_name",
        "$client_name",
        "$service_name",
        "$staff_name",
        "$appointment_time",
    ],
    "reminder": [
        "$business_name",
        "$client_name",
        "$service_name",
        "$staff_name",
        "$appointment_time",
    ],
    "modification": [
        "$business_name",
        "$client_name",
        "$service_name",
        "$staff_name",
        "$appointment_time",
    ],
    "cancellation": [
        "$business_name",
        "$client_name",
        "$service_name",
        "$staff_name",
        "$appointment_time",
    ],
    "form_link": ["$business_name", "$client_name", "$form_link"],
    "package_notice": ["$business_name", "$client_name", "$package_name", "$package_remaining"],
    "low_stock": [
        "$business_name",
        "$product_name",
        "$variant_name",
        "$sku",
        "$quantity_on_hand",
        "$low_stock_threshold",
    ],
}


# --- sender, SMS, reminders -------------------------------------------------------------------


class NotificationTemplateOut(BaseModel):
    id: uuid.UUID
    notification_type: str
    channel: str
    subject_template: str | None
    body_template: str
    updated_at: datetime
    merge_fields: list[str]


class NotificationSettingsOut(BaseModel):
    """What the panel reads. `email_ready`/`sms_ready` are `notifications/providers.py`'s own
    pure checks, not re-derived here — the one place both are computed, so the banner and the
    trigger layer (`notifications/triggers.py::dispatch`) can never disagree about whether a
    send would actually go out."""

    email_sender: Literal["resend", "smtp"] | None
    email_ready: bool
    resend_from_address: str | None
    resend_api_key_set: bool
    resend_domain_verified_at: datetime | None
    smtp_host: str | None
    smtp_port: int | None
    smtp_username: str | None
    smtp_from_address: str | None
    smtp_password_set: bool
    smtp_verified_at: datetime | None
    sms_enabled: bool
    sms_ready: bool
    twilio_account_sid: str | None
    twilio_from_number: str | None
    twilio_auth_token_set: bool
    reminder_intervals_hours: list[int]
    templates: list[NotificationTemplateOut]

    # --- booking-portal policy (Phase 6 Task 4, #10) ------------------------------------
    online_booking_enabled: bool
    online_cancellation_enabled: bool
    cancellation_cutoff_hours: int
    booking_daily_cap_per_ip: int
    booking_daily_cap_per_email: int

    # --- walk-in queue toggle (Phase 7 Task 1, #12) -------------------------------------
    enable_walk_in_queue: bool

    # --- bill review authority toggles (#64) ---------------------------------------------
    enable_bill_override_requests: bool
    enable_inline_admin_bill_edit: bool

    # --- low-stock alert opt-in (#62) -----------------------------------------------------
    low_stock_alert_email_enabled: bool

    # --- CTI demo mode (Phase 14, #16) -----------------------------------------------------
    # Off by default, `enable_walk_in_queue`'s exact shape: with it off, the screen-pop
    # panel's "simulate incoming call" control is unreachable anywhere (`scheduling/cti.py`'s
    # own 404 is the real enforcement; this is only the toggle).
    demo_mode: bool


class NotificationSettingsChange(BaseModel):
    """A field left out (or sent `null`) is left alone — the Security panel's own rule, applied
    here to every field including the three secrets, which is the only way "rotate the Twilio
    token without retyping everything else" and "leave a credential untouched" can both be
    expressed. `"none"` is `email_sender`'s explicit clear: `null` already means "untouched", so
    turning email off needs a value of its own."""

    email_sender: Literal["resend", "smtp", "none"] | None = None
    resend_from_address: OptionalEmail = None
    resend_api_key: str | None = None
    smtp_host: OptionalText = None
    smtp_port: Annotated[int, Field(ge=1, le=65535)] | None = None
    smtp_username: OptionalText = None
    smtp_password: str | None = None
    smtp_from_address: OptionalEmail = None
    sms_enabled: bool | None = None
    twilio_account_sid: OptionalText = None
    twilio_auth_token: str | None = None
    twilio_from_number: OptionalText = None
    reminder_intervals_hours: ReminderIntervals | None = None

    # --- booking-portal policy (Phase 6 Task 4, #10) ------------------------------------
    online_booking_enabled: bool | None = None
    online_cancellation_enabled: bool | None = None
    cancellation_cutoff_hours: CancellationCutoffHours | None = None
    booking_daily_cap_per_ip: DailyCap | None = None
    booking_daily_cap_per_email: DailyCap | None = None

    # --- walk-in queue toggle (Phase 7 Task 1, #12) -------------------------------------
    enable_walk_in_queue: bool | None = None

    # --- bill review authority toggles (#64) ---------------------------------------------
    enable_bill_override_requests: bool | None = None
    enable_inline_admin_bill_edit: bool | None = None

    # --- low-stock alert opt-in (#62) -----------------------------------------------------
    low_stock_alert_email_enabled: bool | None = None

    # --- CTI demo mode (Phase 14, #16) -----------------------------------------------------
    demo_mode: bool | None = None


class TestEmailRequest(BaseModel):
    to: EmailStr


class TestSmsRequest(BaseModel):
    to: Annotated[str, Field(min_length=7, max_length=20)]


class TestSendResult(BaseModel):
    sent: bool


async def _business(db: SessionDep) -> Business:
    business = await db.scalar(select(Business).where(Business.id == 1))
    if business is None:
        raise HTTPException(status_code=404, detail="This instance has not been set up.")
    return business


async def _templates(db: SessionDep) -> list[NotificationTemplate]:
    return list(
        (
            await db.scalars(
                select(NotificationTemplate).order_by(
                    NotificationTemplate.notification_type, NotificationTemplate.channel
                )
            )
        ).all()
    )


def _template_out(template: NotificationTemplate) -> NotificationTemplateOut:
    return NotificationTemplateOut(
        id=template.id,
        notification_type=template.notification_type,
        channel=template.channel,
        subject_template=template.subject_template,
        body_template=template.body_template,
        updated_at=template.updated_at,
        merge_fields=MERGE_FIELDS.get(template.notification_type, []),
    )


def _settings_out(
    business: Business, templates: list[NotificationTemplate]
) -> NotificationSettingsOut:
    return NotificationSettingsOut(
        email_sender=business.email_sender,  # type: ignore[arg-type]
        email_ready=email_ready(business),
        resend_from_address=business.resend_from_address,
        resend_api_key_set=bool(business.resend_api_key_encrypted),
        resend_domain_verified_at=business.resend_domain_verified_at,
        smtp_host=business.smtp_host,
        smtp_port=business.smtp_port,
        smtp_username=business.smtp_username,
        smtp_from_address=business.smtp_from_address,
        smtp_password_set=bool(business.smtp_password_encrypted),
        smtp_verified_at=business.smtp_verified_at,
        sms_enabled=business.sms_enabled,
        sms_ready=sms_ready(business),
        twilio_account_sid=business.twilio_account_sid,
        twilio_from_number=business.twilio_from_number,
        twilio_auth_token_set=bool(business.twilio_auth_token_encrypted),
        reminder_intervals_hours=business.reminder_intervals_hours,
        templates=[_template_out(t) for t in templates],
        online_booking_enabled=business.online_booking_enabled,
        online_cancellation_enabled=business.online_cancellation_enabled,
        cancellation_cutoff_hours=business.cancellation_cutoff_hours,
        booking_daily_cap_per_ip=business.booking_daily_cap_per_ip,
        booking_daily_cap_per_email=business.booking_daily_cap_per_email,
        enable_walk_in_queue=business.enable_walk_in_queue,
        enable_bill_override_requests=business.enable_bill_override_requests,
        enable_inline_admin_bill_edit=business.enable_inline_admin_bill_edit,
        low_stock_alert_email_enabled=business.low_stock_alert_email_enabled,
        demo_mode=business.demo_mode,
    )


@router.get("/business/notifications")
async def read_notification_settings(db: SessionDep) -> NotificationSettingsOut:
    business = await _business(db)
    return _settings_out(business, await _templates(db))


@router.patch("/business/notifications")
async def update_notification_settings(
    payload: NotificationSettingsChange, admin: AdminCapability, db: SessionDep
) -> NotificationSettingsOut:
    business = await _business(db)

    candidates: dict[str, object] = {}
    if payload.email_sender is not None:
        candidates["email_sender"] = (
            None if payload.email_sender == "none" else payload.email_sender
        )
    if payload.resend_from_address is not None:
        candidates["resend_from_address"] = payload.resend_from_address
    if payload.resend_api_key:
        candidates["resend_api_key_encrypted"] = encrypt_credential(payload.resend_api_key)
    if payload.smtp_host is not None:
        candidates["smtp_host"] = payload.smtp_host
    if payload.smtp_port is not None:
        candidates["smtp_port"] = payload.smtp_port
    if payload.smtp_username is not None:
        candidates["smtp_username"] = payload.smtp_username
    if payload.smtp_password:
        candidates["smtp_password_encrypted"] = encrypt_credential(payload.smtp_password)
    if payload.smtp_from_address is not None:
        candidates["smtp_from_address"] = payload.smtp_from_address
    if payload.sms_enabled is not None:
        candidates["sms_enabled"] = payload.sms_enabled
    if payload.twilio_account_sid is not None:
        candidates["twilio_account_sid"] = payload.twilio_account_sid
    if payload.twilio_auth_token:
        candidates["twilio_auth_token_encrypted"] = encrypt_credential(payload.twilio_auth_token)
    if payload.twilio_from_number is not None:
        candidates["twilio_from_number"] = payload.twilio_from_number
    if payload.reminder_intervals_hours is not None:
        candidates["reminder_intervals_hours"] = payload.reminder_intervals_hours
    if payload.online_booking_enabled is not None:
        candidates["online_booking_enabled"] = payload.online_booking_enabled
    if payload.online_cancellation_enabled is not None:
        candidates["online_cancellation_enabled"] = payload.online_cancellation_enabled
    if payload.cancellation_cutoff_hours is not None:
        candidates["cancellation_cutoff_hours"] = payload.cancellation_cutoff_hours
    if payload.booking_daily_cap_per_ip is not None:
        candidates["booking_daily_cap_per_ip"] = payload.booking_daily_cap_per_ip
    if payload.booking_daily_cap_per_email is not None:
        candidates["booking_daily_cap_per_email"] = payload.booking_daily_cap_per_email
    if payload.enable_walk_in_queue is not None:
        candidates["enable_walk_in_queue"] = payload.enable_walk_in_queue
    if payload.enable_bill_override_requests is not None:
        candidates["enable_bill_override_requests"] = payload.enable_bill_override_requests
    if payload.enable_inline_admin_bill_edit is not None:
        candidates["enable_inline_admin_bill_edit"] = payload.enable_inline_admin_bill_edit
    if payload.low_stock_alert_email_enabled is not None:
        candidates["low_stock_alert_email_enabled"] = payload.low_stock_alert_email_enabled
    if payload.demo_mode is not None:
        candidates["demo_mode"] = payload.demo_mode

    changed = [field for field, value in candidates.items() if getattr(business, field) != value]
    for field in changed:
        setattr(business, field, candidates[field])

    # A credential or address edit invalidates the verification it used to certify (module
    # docstring) — never silently kept "ready" against a value nobody has tested since.
    if business.resend_domain_verified_at is not None and (
        "resend_api_key_encrypted" in changed or "resend_from_address" in changed
    ):
        business.resend_domain_verified_at = None
        changed.append("resend_domain_verified_at")
    if business.smtp_verified_at is not None and {
        "smtp_host",
        "smtp_port",
        "smtp_username",
        "smtp_password_encrypted",
        "smtp_from_address",
    } & set(changed):
        business.smtp_verified_at = None
        changed.append("smtp_verified_at")

    if changed:
        # Field names, not values: three of these are secrets, and the rest have no reason to
        # be easier to reconstruct from an audit trail than from the settings screen itself —
        # the same reasoning `settings/routes.py::update_business` already uses.
        record_event(
            db,
            "business.notification_settings_updated",
            target_type="business",
            target_id=str(business.id),
            actor_user_id=admin.id,
            metadata={"changed": changed},
        )
    await db.commit()
    return _settings_out(business, await _templates(db))


@router.post("/business/notifications/test-email")
async def send_test_email(
    payload: TestEmailRequest, admin: AdminCapability, db: SessionDep
) -> NotificationSettingsOut:
    business = await _business(db)
    if not business.email_sender:
        raise HTTPException(400, "Choose an email sender before sending a test.")
    try:
        provider = get_provider(business)
        await run_in_threadpool(
            provider.send_email,
            to=payload.to,
            subject="Test email from LinSuite",
            text=(
                "This is a test message from your LinSuite settings panel. If you received "
                "it, this business's email sender is configured correctly."
            ),
        )
    except Exception as error:
        # #116: the most recent test send failed, so a prior success no longer speaks for this
        # sender — the same invalidation a credential edit already does in the PATCH above,
        # applied to "credentials unchanged but the send itself failed" too.
        if business.email_sender == "resend" and business.resend_domain_verified_at is not None:
            business.resend_domain_verified_at = None
            await db.commit()
        elif business.email_sender == "smtp" and business.smtp_verified_at is not None:
            business.smtp_verified_at = None
            await db.commit()
        raise HTTPException(400, f"The test email could not be sent: {error}") from error

    now = datetime.now(UTC)
    if business.email_sender == "resend":
        business.resend_domain_verified_at = now
    else:
        business.smtp_verified_at = now
    record_event(
        db,
        "business.notification_email_verified",
        target_type="business",
        target_id=str(business.id),
        actor_user_id=admin.id,
        metadata={"email_sender": business.email_sender},
    )
    await db.commit()
    return _settings_out(business, await _templates(db))


@router.post("/business/notifications/test-sms")
async def send_test_sms(
    payload: TestSmsRequest, admin: AdminCapability, db: SessionDep
) -> TestSendResult:
    """No `*_verified_at` to set afterwards (module docstring) — a real send is still attempted,
    so "the adapter works" is answered for real rather than assumed from the fields being
    filled in."""
    business = await _business(db)
    if not sms_ready(business):
        raise HTTPException(
            400, "Turn on SMS and add your Twilio credentials before sending a test."
        )
    try:
        provider = get_sms_provider(business)
        await run_in_threadpool(
            provider.send_sms,
            to=payload.to,
            text="This is a test message from your LinSuite settings panel.",
        )
    except Exception as error:
        raise HTTPException(400, f"The test text could not be sent: {error}") from error
    return TestSendResult(sent=True)


# --- templates ----------------------------------------------------------------------------


class NotificationTemplateChange(BaseModel):
    subject_template: OptionalText = None
    body_template: Annotated[str, Field(min_length=1)]


@router.put("/business/notifications/templates/{template_id}")
async def update_notification_template(
    template_id: uuid.UUID,
    payload: NotificationTemplateChange,
    admin: AdminCapability,
    db: SessionDep,
) -> NotificationTemplateOut:
    template = await db.get(NotificationTemplate, template_id)
    if template is None:
        raise HTTPException(404, "No such notification template.")

    subject = None if template.channel == "sms" else payload.subject_template
    if template.channel == "email" and not subject:
        raise HTTPException(422, "An email template needs a subject line.")

    changed = []
    if subject != template.subject_template:
        changed.append("subject_template")
        template.subject_template = subject
    if payload.body_template != template.body_template:
        changed.append("body_template")
        template.body_template = payload.body_template

    if changed:
        record_event(
            db,
            "business.notification_template_updated",
            target_type="notification_template",
            target_id=str(template.id),
            actor_user_id=admin.id,
            metadata={
                "notification_type": template.notification_type,
                "channel": template.channel,
                "changed": changed,
            },
        )
    await db.commit()
    return _template_out(template)
