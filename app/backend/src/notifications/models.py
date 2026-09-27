"""Notification templates: one row per `(notification_type, channel)` (Phase 12 Task 1, #11).

Owner decision (`m3.md`): rendered with stdlib `string.Template.safe_substitute` (see
`notifications/render.py`), never Jinja2 — the substitutions needed (`$client_name`,
`$appointment_time`, ...) are flat variable interpolation with no loops or conditionals, and a
template that cannot execute an expression is the right amount of power for text an admin can
edit later (Task 6 — not built here).

Seeded with sensible defaults by migration 0032, so an untouched deployment still sends
something coherent for every type/channel pair before anybody has opened the settings panel.
"""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Text, UniqueConstraint, func, text
from sqlalchemy.orm import Mapped, mapped_column

from core.db import Base

NOTIFICATION_TYPES = (
    "booking_confirmation",
    "reminder",
    "modification",
    "cancellation",
    "form_link",
    "package_notice",
)
CHANNELS = ("email", "sms")


class NotificationTemplate(Base):
    __tablename__ = "notification_templates"
    __table_args__ = (
        UniqueConstraint(
            "notification_type", "channel", name="uq_notification_templates_type_channel"
        ),
        CheckConstraint(
            "notification_type IN (" + ", ".join(f"'{t}'" for t in NOTIFICATION_TYPES) + ")",
            name="ck_notification_templates_type",
        ),
        CheckConstraint(
            "channel IN (" + ", ".join(f"'{c}'" for c in CHANNELS) + ")",
            name="ck_notification_templates_channel",
        ),
        # SMS has no subject line; email needs one to render a coherent message.
        CheckConstraint(
            "channel = 'sms' OR subject_template IS NOT NULL",
            name="ck_notification_templates_email_subject",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    notification_type: Mapped[str] = mapped_column(Text)
    channel: Mapped[str] = mapped_column(Text)
    # Nullable for sms (checked above); the raw template text, substituted by `render()`.
    subject_template: Mapped[str | None] = mapped_column(Text)
    body_template: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class NotificationFailure(Base):
    """A terminal delivery failure against a customer (Task 4, #11).

    Written only for a `PermanentDeliveryError` (`notifications/providers.py`) — a 4xx from
    Resend/Twilio, a bad SMTP recipient or a rejected SMTP login: the kind retrying will not
    fix. A transient failure (network error, a 5xx) just keeps retrying under
    `notifications/tasks.py`'s existing backoff and never reaches this table. Surfaced as a
    "delivery failed" indicator on the client profile (`customers/routes.py`); ordinary
    app-role DML, ordinary grants — nothing here is immutable or append-only, since a failure
    row is diagnostic, not a compliance record.
    """

    __tablename__ = "notification_failures"
    __table_args__ = (
        CheckConstraint("channel IN ('email', 'sms')", name="ck_notification_failures_channel"),
        CheckConstraint(
            "notification_type IN (" + ", ".join(f"'{t}'" for t in NOTIFICATION_TYPES) + ")",
            name="ck_notification_failures_type",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    # Cascades: a purged customer's diagnostic delivery history goes with them — there is
    # nothing here that outlives the record it describes (contrast `audit_events`, which is
    # append-only and never cascades).
    customer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"), index=True
    )
    channel: Mapped[str] = mapped_column(Text)
    notification_type: Mapped[str] = mapped_column(Text)
    recipient: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
