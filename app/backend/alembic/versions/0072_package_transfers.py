"""Package transfer (#111, spec #96): append-only `package_transfers`.

Records one hop of a package purchase's remaining credits from one client to another: the
purchase, the from/to clients, a required reason, whether the transfer overrode a
non-transferable definition, the transferring user and the instant. `package_purchases.
customer_id` is untouched by this ticket and keeps its existing meaning — the purchaser,
permanently; the current holder is derived from this table's latest row per purchase, never
stored (`billing/package_holder.py`).

**Grants and guard follow the `commission_postings` (0057) / `package_purchase_credits`
(0055) append-only shape exactly, minus the purge-role bypass those two still carry**: this
table is created after 0067 ("purge-role default-deny"), so `ALTER DEFAULT PRIVILEGES`
already hands `linsuite_purge` `SELECT` alone on it — nothing to revoke, nothing to grant, the
same "ships closed by construction" guarantee 0067's own `test_a_table_created_after_the_
migration_grants_the_purge_role_select_only` proves for any table nobody thinks about. The
app role gets `SELECT, INSERT` from the baseline default and loses `UPDATE, DELETE` here; the
trigger's own bypass is schema-owner only, the shape every guard 0067 touched was brought to
resemble.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0072"
down_revision: str | None = "0071"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "package_transfers"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "package_purchase_id",
            sa.Uuid(),
            sa.ForeignKey("package_purchases.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "from_customer_id",
            sa.Uuid(),
            sa.ForeignKey("customers.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "to_customer_id",
            sa.Uuid(),
            sa.ForeignKey("customers.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("override", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "transferred_by",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "transferred_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "from_customer_id != to_customer_id", name="ck_package_transfers_distinct_parties"
        ),
    )
    op.create_index(
        "ix_package_transfers_purchase_transferred",
        TABLE,
        ["package_purchase_id", "transferred_at"],
    )

    # --- append-only: the app role may INSERT and SELECT, nothing else -------------------------
    op.execute(f"REVOKE UPDATE, DELETE ON {TABLE} FROM linsuite_app")
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION {TABLE}_no_rewrite() RETURNS trigger
        SET search_path = pg_catalog, pg_temp
        AS $$
        BEGIN
            IF current_user = (
               SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid = TG_RELID
            ) THEN
                RETURN CASE TG_OP WHEN 'DELETE' THEN OLD ELSE NEW END;
            END IF;
            RAISE EXCEPTION '{TABLE} is append-only: % is not permitted for %',
                TG_OP, current_user
                USING ERRCODE = 'insufficient_privilege';
        END $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER {TABLE}_no_rewrite
        BEFORE UPDATE OR DELETE ON {TABLE}
        FOR EACH ROW EXECUTE FUNCTION {TABLE}_no_rewrite();
        """
    )


def downgrade() -> None:
    op.execute(f"DROP TRIGGER IF EXISTS {TABLE}_no_rewrite ON {TABLE}")
    op.execute(f"DROP FUNCTION IF EXISTS {TABLE}_no_rewrite()")
    op.drop_index("ix_package_transfers_purchase_transferred", table_name=TABLE)
    op.drop_table(TABLE)
