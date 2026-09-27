"""`queue_entries.bare_phone` (Phase 7 Task 2, #12).

Revision ID: 0041
Revises: 0040
Create Date: 2026-09-27

Issue #12's own acceptance text for quick-create identity: "name only, phone optional" — a
walk-in with no `customer_id` may still leave a phone number without becoming a full customer
record (CLAUDE.md: "a walk-in may never become a full customer record"). Task 1's migration
(0040) only added `bare_name`; this is the field its own text promised. Nullable, no CHECK of
its own — it only ever accompanies `bare_name` (enforced at the API layer, `scheduling/queue.py`
— a customer's own phone already lives on `customers.phone`, so there is nothing to enforce
between this column and `customer_id` at the database, the same way the API layer, not a CHECK,
is what enforces `PublicBookingIn`'s "an email or a phone" in `scheduling/public.py`).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0041"
down_revision: str | None = "0040"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("queue_entries", sa.Column("bare_phone", sa.String(32), nullable=True))


def downgrade() -> None:
    op.drop_column("queue_entries", "bare_phone")
