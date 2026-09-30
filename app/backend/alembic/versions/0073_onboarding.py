"""Onboarding checklist (#116, spec #113): `businesses.onboarding_dismissed_at`.

Revision ID: 0073
Revises: 0072
Create Date: 2026-09-29

One column. The checklist's seven `done` flags are all computed from data that already has
somewhere to live — an address and phone on `businesses`, `working_hours` rows, active
`tax_components` with a rate, active `services`, active `staff`, a `branding_assets` logo row,
and the email sender's existing `resend_domain_verified_at`/`smtp_verified_at` columns (Task 6,
#11) for "a sender is configured and its latest test send succeeded". Nothing new to store for
any of those.

**The email step's "latest" is the one gap, and the fix is in `settings/notifications_routes.py`,
not a new column.** Before this ticket, a *failed* test send left a previously-set
`*_verified_at` untouched — only a credential edit cleared it — so a business that verified once
and then failed a later test would still read as "ready". `send_test_email` now clears the
sender's own `*_verified_at` on a failed send too, the same invalidation a credential edit
already does one function up. That is a behaviour change to an existing route, not a schema
change, so it needed no migration of its own.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0073"
down_revision: str | None = "0072"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("businesses", sa.Column("onboarding_dismissed_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    op.drop_column("businesses", "onboarding_dismissed_at")
