"""Discount definitions + `billing.manage` capability (#58, M4 spec #54 stories 18/19/21/25).

Revision ID: 0044
Revises: 0043
Create Date: 2026-09-28

`discounts`: fixed-amount or percentage (basis points, `Staff.commission_rate_services_bp`'s
own convention — CHECK 0-10000), eligible against all or selected catalog items, a stackable
flag, and a commission-basis choice. `ck_discounts_amount_matches_kind` is the database's own
copy of the either/or `percentage_bp`/`amount_cents` invariant the API layer also checks, the
same belt-and-suspenders `ServiceRequirement`'s kind-matches-resource rule follows.

`discount_eligible_items`: one row per `(discount_id, item_type, item_id)` when a discount is
scoped to `"selected"` rather than `"all"`. No foreign key on `item_id` — `products`/
`packages` don't exist in this branch yet (#56/#60 are sibling tickets in the same wave), and
this follows `AuditEvent.target_type`/`target_id`'s own precedent for a polymorphic reference
that must not couple across domains.

Ordinary app-role DML on both tables, grants inherited from 0001's `ALTER DEFAULT
PRIVILEGES`: a discount definition is operational, not an immutable or append-only compliance
record — the same reasoning `0040_walk_in_queue.py` gives for `queue_entries`. Nothing here is
"the resolved amount on an issued invoice line"; that snapshot (and its own immutability) is
#65's job.

`billing.manage` goes to the Administrator role only, the same call `0022_audit_view_
capability.py` made for `audit.view`: defining what a discount is worth is an administrative
decision, not front-desk work the way `forms.issue`/`queue.manage` are.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0044"
down_revision: str | None = "0043"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "discounts",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("percentage_bp", sa.Integer(), nullable=True),
        sa.Column("amount_cents", sa.Integer(), nullable=True),
        sa.Column(
            "eligibility_scope", sa.String(16), server_default=sa.text("'all'"), nullable=False
        ),
        sa.Column("stackable", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("commission_basis", sa.String(16), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("kind IN ('percentage', 'fixed')", name="ck_discounts_kind"),
        sa.CheckConstraint(
            "eligibility_scope IN ('all', 'selected')", name="ck_discounts_eligibility_scope"
        ),
        sa.CheckConstraint(
            "commission_basis IN ('reduces', 'absorbed')", name="ck_discounts_commission_basis"
        ),
        sa.CheckConstraint(
            "(kind = 'percentage' AND percentage_bp IS NOT NULL "
            "AND percentage_bp BETWEEN 0 AND 10000 AND amount_cents IS NULL) "
            "OR (kind = 'fixed' AND amount_cents IS NOT NULL AND amount_cents >= 0 "
            "AND percentage_bp IS NULL)",
            name="ck_discounts_amount_matches_kind",
        ),
    )
    op.create_table(
        "discount_eligible_items",
        sa.Column(
            "discount_id",
            sa.Uuid(),
            sa.ForeignKey("discounts.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("item_type", sa.String(16), primary_key=True),
        sa.Column("item_id", sa.Uuid(), primary_key=True),
        sa.CheckConstraint(
            "item_type IN ('service', 'product', 'package')", name="ck_discount_items_item_type"
        ),
    )

    op.execute(
        "INSERT INTO role_capabilities (role_id, capability) "
        "SELECT id, 'billing.manage' FROM roles WHERE name = 'Administrator' AND is_system "
        "ON CONFLICT DO NOTHING"
    )


def downgrade() -> None:
    op.execute("DELETE FROM role_capabilities WHERE capability = 'billing.manage'")
    op.drop_table("discount_eligible_items")
    op.drop_table("discounts")
