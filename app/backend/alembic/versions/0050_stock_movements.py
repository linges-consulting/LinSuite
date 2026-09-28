"""Stock movements: append-only ledger + `inventory.receive`/`inventory.adjust` (#61).

Revision ID: 0050
Revises: 0049
Create Date: 2026-09-28

Picked `0050`: `0049` (`service_bills`, #59) was the head at the time this ticket's worktree
started (checked `ls alembic/versions/` first, per `m4.md`'s parallel-merge hazard note — the
orchestrator resolves any collision with a sibling wave ticket at merge time, not this one).

`stock_movements` is the ledger `product_variants.quantity_on_hand` (#56, migration 0046)
promised: every receipt, sale, return and manual adjustment writes one row, signed whole
units, and nothing but `inventory/stock.py::record_movement`'s single atomic
`UPDATE product_variants SET quantity_on_hand = quantity_on_hand + :delta WHERE id = :id AND
quantity_on_hand + :delta >= 0` ever changes the count — CLAUDE.md's "Concurrency" section:
the statement is the lock, never an app-level one.

Append-only by grant and trigger, the exact `audit_events` (0004) shape: `linsuite_app` loses
UPDATE/DELETE, a trigger blocks both for anyone but the schema owner or `linsuite_purge` (no
job purges this table in v1 — nothing here is personal data needing a retention clock — but
tests reset state between runs the same way `test_inventory.py` already clears `audit_events`,
via `get_purge_engine()`).

`ck_stock_movements_adjustment_reason` is the database's own copy of "reason is required for
a manual adjustment" — the API checks it too, but a migration, an import or a psql session
should not be able to write a reasonless adjustment either.

`inventory.receive` and `inventory.adjust` are two distinct capabilities (new "Inventory"
group — #56 reused `catalog.manage` and never created one), both Administrator-only and
`requires_admin_mode=True`, independently grantable to trusted staff later via the roles
screen. A future sale-driven deduction (#75, building on #21) calls `record_movement`
directly with `kind="sale"`, never through a capability-gated route — nothing in this
migration or `inventory/stock_routes.py` gates that path, because it doesn't exist yet.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0050"
down_revision: str | None = "0049"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "stock_movements",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("variant_id", sa.Uuid(), sa.ForeignKey("product_variants.id"), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("quantity_delta", sa.Integer(), nullable=False),
        sa.Column("reason", sa.Text()),
        # Not a foreign key — see the model's own docstring: the ledger outlives the account
        # that made the entry, the same reasoning `audit_events.actor_user_id` (0004) gives.
        sa.Column("actor_user_id", sa.Uuid()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("quantity_delta <> 0", name="ck_stock_movements_quantity_delta"),
        sa.CheckConstraint(
            "kind IN ('receipt', 'sale', 'return', 'adjustment')", name="ck_stock_movements_kind"
        ),
        sa.CheckConstraint(
            "kind <> 'adjustment' OR reason IS NOT NULL",
            name="ck_stock_movements_adjustment_reason",
        ),
    )
    op.create_index(
        "ix_stock_movements_variant_created", "stock_movements", ["variant_id", "created_at"]
    )

    op.execute("REVOKE UPDATE, DELETE ON stock_movements FROM linsuite_app")
    op.execute(
        """
        CREATE OR REPLACE FUNCTION stock_movements_append_only() RETURNS trigger
        SET search_path = pg_catalog, pg_temp
        AS $$
        BEGIN
            -- The purge role runs retention jobs (nothing here purges this table in v1, but
            -- the test suite resets it via `get_purge_engine()`, the same way it already
            -- resets `audit_events`); the schema owner is let through so migrations and an
            -- operator's recovery are not blocked. The application never connects as either.
            IF current_user = 'linsuite_purge'
               OR current_user = (
                   SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid = TG_RELID
               ) THEN
                RETURN CASE TG_OP WHEN 'DELETE' THEN OLD ELSE NEW END;
            END IF;
            RAISE EXCEPTION 'stock_movements is append-only: % is not permitted for %',
                TG_OP, current_user
                USING ERRCODE = 'insufficient_privilege';
        END $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER stock_movements_no_rewrite
        BEFORE UPDATE OR DELETE ON stock_movements
        FOR EACH ROW EXECUTE FUNCTION stock_movements_append_only();
        """
    )

    for capability in ("inventory.receive", "inventory.adjust"):
        op.execute(
            "INSERT INTO role_capabilities (role_id, capability) "
            f"SELECT id, '{capability}' FROM roles WHERE name = 'Administrator' AND is_system "
            "ON CONFLICT DO NOTHING"
        )


def downgrade() -> None:
    op.execute(
        "DELETE FROM role_capabilities "
        "WHERE capability IN ('inventory.receive', 'inventory.adjust')"
    )
    op.execute("DROP TRIGGER IF EXISTS stock_movements_no_rewrite ON stock_movements")
    op.execute("DROP FUNCTION IF EXISTS stock_movements_append_only()")
    op.drop_index("ix_stock_movements_variant_created", table_name="stock_movements")
    op.drop_table("stock_movements")
