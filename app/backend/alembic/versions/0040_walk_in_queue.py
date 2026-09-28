"""`queue_entries` + `enable_walk_in_queue` toggle (Phase 7 Task 1, #12).

Revision ID: 0040
Revises: 0039
Create Date: 2026-09-27

Two different things are called "walk-in" (CLAUDE.md, tech-stack §22): the always-on "next
available" search shortcut (Phase 7 Task 3 — no new entity, no config) and this — the *opt-in*
"take a number" queue, `enable_walk_in_queue` (default off) on `businesses`. This task only
builds the table and the toggle; no route reads either yet (Tasks 2/8 build the CRUD, the nav
entry and the display screen, all gated on this column), so "with the queue disabled, no queue
surface exists anywhere in the product" (#12's acceptance criterion) holds trivially — there is
no surface at all yet, on or off.

`queue_entries`: `id, customer_id NULL, bare_name NULL, requested_service_id,
preferred_staff_id NULL, arrived_at, status`. **Exactly one of `customer_id`/`bare_name`** — a
walk-in may be a known returning client or a name-only quick-entry, never both, never neither
(m3.md's own wording) — enforced with `num_nonnulls(customer_id, bare_name) = 1`, the plain
Postgres idiom for the invariant rather than a hand-rolled XOR of two `IS NULL` checks. No FK
cascades on `customer_id`/`requested_service_id`/`preferred_staff_id`, the same as
`appointments.customer_id`/`staff_id`/`service_id`: a customer, a staff member and a service
are each never hard-deleted (every one of those modules' own "no hard delete" rule), so there
is nothing here a cascade would ever actually need to reach.

Grants are inherited from 0001's `ALTER DEFAULT PRIVILEGES`; this table is ordinary app-role
DML, nothing immutable or append-only — a queue entry is operational, not a compliance record
(the `Appointment` it converts into, Task 5, is where the durable history lives).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0040"
down_revision: str | None = "0039"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "businesses",
        sa.Column(
            "enable_walk_in_queue", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
    )
    op.create_table(
        "queue_entries",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("customer_id", sa.Uuid(), sa.ForeignKey("customers.id"), nullable=True),
        sa.Column("bare_name", sa.String(200), nullable=True),
        sa.Column("requested_service_id", sa.Uuid(), sa.ForeignKey("services.id"), nullable=False),
        sa.Column("preferred_staff_id", sa.Uuid(), sa.ForeignKey("staff.id"), nullable=True),
        sa.Column(
            "arrived_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("status", sa.String(16), server_default=sa.text("'waiting'"), nullable=False),
        sa.CheckConstraint(
            "num_nonnulls(customer_id, bare_name) = 1", name="ck_queue_entries_identity"
        ),
        sa.CheckConstraint(
            "status IN ('waiting', 'in_service', 'done', 'abandoned')",
            name="ck_queue_entries_status",
        ),
    )


def downgrade() -> None:
    op.drop_table("queue_entries")
    op.drop_column("businesses", "enable_walk_in_queue")
