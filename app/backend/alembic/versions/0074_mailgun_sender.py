"""Mailgun email sender: credential storage alongside Resend/SMTP (M7, #115).

Revision ID: 0074
Revises: 0072
Create Date: 2026-09-29

Same shape as 0033's Resend/SMTP columns: `mailgun_api_key_encrypted` is direct field
encryption under `NOTIFICATION_CREDENTIAL_KEY` (`notifications/credentials.py`), and
`mailgun_verified_at` is set only by a successful test send
(`settings/notifications_routes.py`), gating `notifications/providers.py::email_ready` the
same way `resend_domain_verified_at`/`smtp_verified_at` already do. `mailgun_region` picks the
US (`api.mailgun.net`) or EU (`api.eu.mailgun.net`) API host — Mailgun EU accounts only work
against the EU host.

`ck_businesses_email_sender` (0033) is dropped and recreated to admit `'mailgun'`; a second
CHECK constrains the region to the two Mailgun regions that exist.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0074"
down_revision: str | None = "0072"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS = (
    ("mailgun_api_key_encrypted", sa.Text()),
    ("mailgun_domain", sa.String(255)),
    ("mailgun_region", sa.String(2)),
    ("mailgun_from_address", sa.String(320)),
    ("mailgun_verified_at", sa.DateTime(timezone=True)),
)


def upgrade() -> None:
    for name, type_ in _COLUMNS:
        op.add_column("businesses", sa.Column(name, type_, nullable=True))
    op.drop_constraint("ck_businesses_email_sender", "businesses", type_="check")
    op.create_check_constraint(
        "ck_businesses_email_sender",
        "businesses",
        "email_sender IS NULL OR email_sender IN ('resend', 'smtp', 'mailgun')",
    )
    op.create_check_constraint(
        "ck_businesses_mailgun_region",
        "businesses",
        "mailgun_region IS NULL OR mailgun_region IN ('us', 'eu')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_businesses_mailgun_region", "businesses", type_="check")
    op.drop_constraint("ck_businesses_email_sender", "businesses", type_="check")
    op.create_check_constraint(
        "ck_businesses_email_sender",
        "businesses",
        "email_sender IS NULL OR email_sender IN ('resend', 'smtp')",
    )
    for name, _ in reversed(_COLUMNS):
        op.drop_column("businesses", name)
