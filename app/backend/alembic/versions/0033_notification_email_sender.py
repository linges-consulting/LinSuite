"""Notification email sender: credential storage for Resend and SMTP (Phase 12 Task 2, #11).

Revision ID: 0033
Revises: 0032
Create Date: 2026-09-27

Direct field encryption under `NOTIFICATION_CREDENTIAL_KEY` (core/config.py,
notifications/credentials.py), not a per-record wrapped-key scheme (owner decision, m3.md) —
one business row, no crypto-shredding requirement here (contrast `customer_document_keys`,
migration 0024). `resend_domain_verified_at`/`smtp_verified_at` are set only by a real test
send (Task 6, not built here); until one is set for the chosen `email_sender`, Task 5's
trigger functions must not attempt a real send — "email features remain disabled behind a
banner until a test send succeeds" (#11 acceptance criterion).

Ordinary app-role DML, like the rest of `businesses` — no append-only trigger. Grants are
inherited from 0001's `ALTER DEFAULT PRIVILEGES`.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0033"
down_revision: str | None = "0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS = (
    ("email_sender", sa.String(16)),
    ("resend_api_key_encrypted", sa.Text()),
    ("resend_from_address", sa.String(320)),
    ("resend_domain_verified_at", sa.DateTime(timezone=True)),
    ("smtp_host", sa.String(255)),
    ("smtp_port", sa.Integer()),
    ("smtp_username", sa.String(255)),
    ("smtp_password_encrypted", sa.Text()),
    ("smtp_from_address", sa.String(320)),
    ("smtp_verified_at", sa.DateTime(timezone=True)),
)


def upgrade() -> None:
    for name, type_ in _COLUMNS:
        op.add_column("businesses", sa.Column(name, type_, nullable=True))
    op.create_check_constraint(
        "ck_businesses_email_sender",
        "businesses",
        "email_sender IS NULL OR email_sender IN ('resend', 'smtp')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_businesses_email_sender", "businesses", type_="check")
    for name, _ in reversed(_COLUMNS):
        op.drop_column("businesses", name)
