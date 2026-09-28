"""Notification templates: one row per (notification_type, channel) (Phase 12 Task 1, #11).

Revision ID: 0032
Revises: 0031
Create Date: 2026-09-27

`notification_templates`: ordinary app-role DML (no append-only trigger — Task 6's settings
panel edits these in place). Seeded here with one email row and one sms row for each of the
six notification types, so an untouched deployment still sends something coherent before an
administrator has opened the templates panel. `$identifier` placeholders are rendered by
`notifications/render.py::render` (stdlib `string.Template.safe_substitute`); an unrecognised
one is left literal, so listing more merge fields here than a given trigger ever supplies is
harmless.

Grants are inherited from 0001's `ALTER DEFAULT PRIVILEGES`; nothing is repeated here.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0032"
down_revision: str | None = "0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = sa.table(
    "notification_templates",
    sa.column("notification_type", sa.Text()),
    sa.column("channel", sa.Text()),
    sa.column("subject_template", sa.Text()),
    sa.column("body_template", sa.Text()),
)

# One email + one sms default per notification type. Deliberately generic merge fields
# ($client_name, $business_name, ...) rather than type-specific ones, since `safe_substitute`
# renders whatever a trigger doesn't supply literally rather than raising.
_SEEDS = [
    {
        "notification_type": "booking_confirmation",
        "channel": "email",
        "subject_template": "Your appointment with $business_name is confirmed",
        "body_template": (
            "Hi $client_name,\n\n"
            "Your $service_name appointment with $staff_name is confirmed for "
            "$appointment_time.\n\n"
            "See you soon,\n$business_name"
        ),
    },
    {
        "notification_type": "booking_confirmation",
        "channel": "sms",
        "subject_template": None,
        "body_template": (
            "Hi $client_name, your $service_name appointment with $staff_name is confirmed "
            "for $appointment_time. - $business_name"
        ),
    },
    {
        "notification_type": "reminder",
        "channel": "email",
        "subject_template": "Reminder: your appointment with $business_name",
        "body_template": (
            "Hi $client_name,\n\n"
            "This is a reminder of your $service_name appointment with $staff_name at "
            "$appointment_time.\n\n$business_name"
        ),
    },
    {
        "notification_type": "reminder",
        "channel": "sms",
        "subject_template": None,
        "body_template": (
            "Reminder: $service_name with $staff_name at $appointment_time. - $business_name"
        ),
    },
    {
        "notification_type": "modification",
        "channel": "email",
        "subject_template": "Your appointment with $business_name has changed",
        "body_template": (
            "Hi $client_name,\n\n"
            "Your $service_name appointment has been updated to $appointment_time with "
            "$staff_name.\n\n$business_name"
        ),
    },
    {
        "notification_type": "modification",
        "channel": "sms",
        "subject_template": None,
        "body_template": (
            "Your $service_name appointment is now $appointment_time with $staff_name. "
            "- $business_name"
        ),
    },
    {
        "notification_type": "cancellation",
        "channel": "email",
        "subject_template": "Your appointment with $business_name was cancelled",
        "body_template": (
            "Hi $client_name,\n\n"
            "Your $service_name appointment with $staff_name on $appointment_time has been "
            "cancelled.\n\n$business_name"
        ),
    },
    {
        "notification_type": "cancellation",
        "channel": "sms",
        "subject_template": None,
        "body_template": (
            "Your $service_name appointment on $appointment_time has been cancelled. "
            "- $business_name"
        ),
    },
    {
        "notification_type": "form_link",
        "channel": "email",
        "subject_template": "Please complete your form for $business_name",
        "body_template": (
            "Hi $client_name,\n\n"
            "Please complete your form using the secure link below before it expires:\n"
            "$form_link\n\n$business_name"
        ),
    },
    {
        "notification_type": "form_link",
        "channel": "sms",
        "subject_template": None,
        "body_template": "$business_name: please complete your form: $form_link",
    },
    {
        "notification_type": "package_notice",
        "channel": "email",
        "subject_template": "Update on your package with $business_name",
        "body_template": (
            "Hi $client_name,\n\n"
            "You have $package_remaining session(s) remaining on your $package_name "
            "package.\n\n$business_name"
        ),
    },
    {
        "notification_type": "package_notice",
        "channel": "sms",
        "subject_template": None,
        "body_template": (
            "$business_name: $package_remaining session(s) remaining on your $package_name package."
        ),
    },
]


def upgrade() -> None:
    op.create_table(
        "notification_templates",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("notification_type", sa.Text(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("subject_template", sa.Text()),
        sa.Column("body_template", sa.Text(), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint(
            "notification_type", "channel", name="uq_notification_templates_type_channel"
        ),
        sa.CheckConstraint(
            "notification_type IN ('booking_confirmation', 'reminder', 'modification', "
            "'cancellation', 'form_link', 'package_notice')",
            name="ck_notification_templates_type",
        ),
        sa.CheckConstraint("channel IN ('email', 'sms')", name="ck_notification_templates_channel"),
        sa.CheckConstraint(
            "channel = 'sms' OR subject_template IS NOT NULL",
            name="ck_notification_templates_email_subject",
        ),
    )
    op.bulk_insert(_TABLE, _SEEDS)


def downgrade() -> None:
    op.drop_table("notification_templates")
