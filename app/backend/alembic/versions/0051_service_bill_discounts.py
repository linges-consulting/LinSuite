"""The discount combination applied to a draft service bill, + `billing.view` capability (#63).

Revision ID: 0051
Revises: 0050
Create Date: 2026-09-28

**Migration-number collision, already known and resolved at merge.** This worktree branched
from `feat/m4-billing-inventory` @ 14eb54b, before #61 (stock movements) merged and claimed
`0050` on the shared branch. This file originally picked `0051` with `down_revision = "0049"`
against the head it actually branched from; the orchestrator re-pointed `down_revision` at
`0050` once #61 landed, chaining it correctly on the integration branch.

`service_bill_discounts`: which of #58's discounts staff has chosen to apply to a draft bill,
from the bill review screen. Bill-level, not line-level — see `billing/models.py::
ServiceBillDiscount`'s own docstring for why: a draft bill still accepts new lines as sibling
appointments complete (#59), and a bill-level selection means an applied discount keeps
applying to whatever line shows up next rather than needing to be reapplied. Which lines it
actually reduces is derived at read time (`discount_resolver.is_eligible` against each line's
`service_id`), never stored.

Persisted rather than recomputed from a request every time — #64 (bill review authority,
blocked by this ticket) reads whatever is here to decide what it is approving, so it has to
survive a page reload and outlive the request that set it. A whole-set replace per bill, the
same shape `discount_eligible_items`/`replace_eligibility` already established: staff picks a
combination, `PUT /api/bills/{id}/discounts` deletes this bill's existing rows and inserts the
new selection, all in one transaction, so a half-applied set is never visible in between.

Composite PK (`bill_id`, `discount_id`) — one discount named twice against one bill is one
application, not two, same reasoning as `DiscountEligibleItem`. `discount_id` is
`ON DELETE RESTRICT`: discounts are never hard-deleted (`enabled` going false is the only
retirement path, `billing/models.py::Discount`'s own docstring) specifically so an applied
bill can always keep naming what applied to it — this FK is the database holding that
promise, not just the missing DELETE route.

Ordinary app-role DML, grants inherited from 0001's `ALTER DEFAULT PRIVILEGES` — the same
"operational, not yet a compliance record" call `0049_service_bills.py` already made for the
draft bill itself. Nothing here is immutable; #65 is still what adds the grants+trigger pair
once a bill is issued.

`billing.view`: staff-facing (front-desk, Staff Mode — no `requires_admin_mode`), the key
`0044_discounts.py`'s own docstring reserved for "whichever ticket first needs one". Seeded to
both seeded roles, the same call `0042_queue_manage_capability.py` made for `queue.manage`:
reviewing a visit's own bill before checkout is front-desk work, not an administrative
decision the way defining a discount (`billing.manage`) is.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0051"
down_revision: str | None = "0050"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "service_bill_discounts",
        sa.Column(
            "bill_id",
            sa.Uuid(),
            sa.ForeignKey("service_bills.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "discount_id",
            sa.Uuid(),
            sa.ForeignKey("discounts.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column(
            "applied_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )

    op.execute(
        "INSERT INTO role_capabilities (role_id, capability) "
        "SELECT id, 'billing.view' FROM roles WHERE name IN ('Administrator', 'Staff') "
        "AND is_system ON CONFLICT DO NOTHING"
    )


def downgrade() -> None:
    op.execute("DELETE FROM role_capabilities WHERE capability = 'billing.view'")
    op.drop_table("service_bill_discounts")
