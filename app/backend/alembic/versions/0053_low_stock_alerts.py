"""Low-stock alerts (#62): armed/alerted flag on `product_variants`, an opt-in business toggle,
and a new `low_stock` notification type.

Revision ID: 0053
Revises: 0052
Create Date: 2026-09-28

Picked 0053 as the next free number after `0052_bill_override_requests.py` (#64, already
merged into `feat/m4-billing-inventory` at the commit this worktree branched from — no
collision with a sibling Wave 3 ticket this time, since #64 already landed).

`product_variants.low_stock_alerted`: not a second ledger, one boolean living beside
`quantity_on_hand`/`low_stock_threshold` on the row `inventory/stock.py::record_movement`
already atomically updates in the same transaction as the stock write (m4.md's `#61` pattern —
"one more thing happens in the same transaction, before commit"). `false` means armed (above
threshold, or never yet crossed); `true` means already alerted for the current crossing.
Restocking back at-or-above the threshold flips it back to `false`, rearming the next crossing.
Server-default `false` so every existing variant (all at or above their own threshold, or not —
the flag is corrected on the very next `record_movement` call regardless) starts armed.

`businesses.low_stock_alert_email_enabled`: the opt-in email toggle, following
`enable_walk_in_queue`'s exact shape (0040) — off by default, no new capability. Reading or
writing it goes through the existing `settings/notifications_routes.py` panel, gated by the
same `Requires("admin")` every other field on that panel already sits behind; a global on/off
switch for a business-wide notification is a setting, not a route-level permission decision (no
route becomes reachable or unreachable based on who holds what — only whether a queued send is
attempted).

`low_stock` joins `NOTIFICATION_TYPES` (`notifications/models.py`) — both CHECK constraints
that enumerate it (`notification_templates`, `notification_failures`) are dropped and
recreated to match, the same invariant every other type already keeps between the ORM's
`CheckConstraint` (computed from the same tuple) and the DB. Seeded with one email row and one
sms row, following 0032's per-type convention exactly (every type gets both channels, even
though `notifications/triggers.py::notify_low_stock` only ever renders the email one for now —
the AC asks for an opt-in *email* alert; a $low_stock_threshold-cents SMS digest is a future
ticket's to wire up, not a reason to leave this type's shape inconsistent with every sibling).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0053"
down_revision: str | None = "0052"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_TYPES = (
    "booking_confirmation",
    "reminder",
    "modification",
    "cancellation",
    "form_link",
    "package_notice",
)
_NEW_TYPES = (*_OLD_TYPES, "low_stock")


def _type_check(name: str, types: tuple[str, ...]) -> sa.CheckConstraint:
    return sa.CheckConstraint(
        "notification_type IN (" + ", ".join(f"'{t}'" for t in types) + ")", name=name
    )


_TEMPLATES_TABLE = sa.table(
    "notification_templates",
    sa.column("notification_type", sa.Text()),
    sa.column("channel", sa.Text()),
    sa.column("subject_template", sa.Text()),
    sa.column("body_template", sa.Text()),
)

_SEEDS = [
    {
        "notification_type": "low_stock",
        "channel": "email",
        "subject_template": "Low stock: $product_name ($variant_name) at $business_name",
        "body_template": (
            "$product_name ($variant_name, SKU $sku) is now at $quantity_on_hand on hand, "
            "at or below its low-stock threshold of $low_stock_threshold.\n\n"
            "Receive more stock or adjust the threshold from Settings > Products.\n\n"
            "$business_name"
        ),
    },
    {
        "notification_type": "low_stock",
        "channel": "sms",
        "subject_template": None,
        "body_template": (
            "$business_name: $product_name ($variant_name) is low — $quantity_on_hand on "
            "hand, threshold $low_stock_threshold."
        ),
    },
]


def upgrade() -> None:
    op.add_column(
        "product_variants",
        sa.Column(
            "low_stock_alerted", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
    )
    op.add_column(
        "businesses",
        sa.Column(
            "low_stock_alert_email_enabled",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )

    op.drop_constraint("ck_notification_templates_type", "notification_templates", type_="check")
    op.create_check_constraint(
        "ck_notification_templates_type",
        "notification_templates",
        "notification_type IN (" + ", ".join(f"'{t}'" for t in _NEW_TYPES) + ")",
    )
    op.drop_constraint("ck_notification_failures_type", "notification_failures", type_="check")
    op.create_check_constraint(
        "ck_notification_failures_type",
        "notification_failures",
        "notification_type IN (" + ", ".join(f"'{t}'" for t in _NEW_TYPES) + ")",
    )

    op.bulk_insert(_TEMPLATES_TABLE, _SEEDS)


def downgrade() -> None:
    op.execute("DELETE FROM notification_templates WHERE notification_type = 'low_stock'")

    op.drop_constraint("ck_notification_failures_type", "notification_failures", type_="check")
    op.create_check_constraint(
        "ck_notification_failures_type",
        "notification_failures",
        "notification_type IN (" + ", ".join(f"'{t}'" for t in _OLD_TYPES) + ")",
    )
    op.drop_constraint("ck_notification_templates_type", "notification_templates", type_="check")
    op.create_check_constraint(
        "ck_notification_templates_type",
        "notification_templates",
        "notification_type IN (" + ", ".join(f"'{t}'" for t in _OLD_TYPES) + ")",
    )

    op.drop_column("businesses", "low_stock_alert_email_enabled")
    op.drop_column("product_variants", "low_stock_alerted")
