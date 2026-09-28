"""Notification SMS sender: Twilio credential storage (Phase 12 Task 3, #11).

Revision ID: 0034
Revises: 0033
Create Date: 2026-09-27

Same shape as 0033's Resend/SMTP columns: direct field encryption under
`NOTIFICATION_CREDENTIAL_KEY` (`notifications/credentials.py`) — reused, not a second key,
since these are just more tenant-owned sender config on the same one-row `businesses` table.
`sms_enabled` (default off) gates whether the trigger layer (Task 5, not built here) ever
constructs an SMS send; "disabling SMS leaves no code path attempting to send it" (#11
acceptance criterion) is enforced by that call site checking this flag first, not by anything
in this migration.

Ordinary app-role DML, like the rest of `businesses` — no append-only trigger. Grants are
inherited from 0001's `ALTER DEFAULT PRIVILEGES`.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0034"
down_revision: str | None = "0033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS = (
    ("twilio_account_sid", sa.String(64)),
    ("twilio_auth_token_encrypted", sa.Text()),
    ("twilio_from_number", sa.String(32)),
)


def upgrade() -> None:
    for name, type_ in _COLUMNS:
        op.add_column("businesses", sa.Column(name, type_, nullable=True))
    op.add_column(
        "businesses",
        sa.Column("sms_enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )


def downgrade() -> None:
    op.drop_column("businesses", "sms_enabled")
    for name, _ in reversed(_COLUMNS):
        op.drop_column("businesses", name)
