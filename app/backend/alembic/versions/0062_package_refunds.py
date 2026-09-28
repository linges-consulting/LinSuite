"""Package/bundle refunds (#73).

Revision ID: 0062
Revises: 0061
Create Date: 2026-09-28

`package_credit_voids`: one append-only row per purchase whose unspent credits a refund
cancelled (`billing/package_refund.py`). The 0061 redemption insert guard is replaced to refuse
a credit of a voided purchase too — the app's `FOR UPDATE` on the purchase row makes the race
readable, the guard makes it impossible.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0062"
down_revision: str | None = "0061"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_GUARD = """
CREATE OR REPLACE FUNCTION package_credit_redemptions_guard() RETURNS trigger
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    total smallint;
    activated boolean;
BEGIN
    IF TG_OP = 'INSERT' THEN
        SELECT c.credits_total, p.credits_activated INTO total, activated
          FROM public.package_purchase_credits c
          JOIN public.package_purchases p ON p.id = c.package_purchase_id
         WHERE c.package_purchase_id = NEW.package_purchase_id
           AND c.service_id = NEW.service_id;
        IF NOT activated THEN
            RAISE EXCEPTION 'package purchase % is not activated', NEW.package_purchase_id
                USING ERRCODE = 'check_violation';
        END IF;
        IF NEW.sequence > total THEN
            RAISE EXCEPTION 'no credit left to redeem (% of %)', NEW.sequence, total
                USING ERRCODE = 'check_violation';
        END IF;
        {void_check}
        RETURN NEW;
    END IF;
    IF current_user = 'linsuite_purge'
       OR current_user = (
           SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid = TG_RELID
       ) THEN
        RETURN CASE TG_OP WHEN 'DELETE' THEN OLD ELSE NEW END;
    END IF;
    RAISE EXCEPTION 'package_credit_redemptions is append-only: % is not permitted for %',
        TG_OP, current_user
        USING ERRCODE = 'insufficient_privilege';
END $$ LANGUAGE plpgsql;
"""

_VOID_CHECK = """IF EXISTS (
            SELECT 1 FROM public.package_credit_voids v
             WHERE v.package_purchase_id = NEW.package_purchase_id
        ) THEN
            RAISE EXCEPTION 'package purchase % credits are voided', NEW.package_purchase_id
                USING ERRCODE = 'check_violation';
        END IF;"""


def upgrade() -> None:
    op.create_table(
        "package_credit_voids",
        sa.Column(
            "package_purchase_id",
            sa.Uuid(),
            sa.ForeignKey("package_purchases.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "voided_by", sa.Uuid(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column(
            "voided_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.execute("REVOKE UPDATE, DELETE ON package_credit_voids FROM linsuite_app")
    op.execute(
        """
        CREATE OR REPLACE FUNCTION package_credit_voids_append_only() RETURNS trigger
        SET search_path = pg_catalog, pg_temp
        AS $$
        BEGIN
            IF current_user = 'linsuite_purge'
               OR current_user = (
                   SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid = TG_RELID
               ) THEN
                RETURN CASE TG_OP WHEN 'DELETE' THEN OLD ELSE NEW END;
            END IF;
            RAISE EXCEPTION 'package_credit_voids is append-only: % is not permitted for %',
                TG_OP, current_user
                USING ERRCODE = 'insufficient_privilege';
        END $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER package_credit_voids_no_rewrite
        BEFORE UPDATE OR DELETE ON package_credit_voids
        FOR EACH ROW EXECUTE FUNCTION package_credit_voids_append_only();
        """
    )
    op.execute(_GUARD.replace("{void_check}", _VOID_CHECK))


def downgrade() -> None:
    op.execute(_GUARD.replace("{void_check}", ""))
    op.execute("DROP TRIGGER IF EXISTS package_credit_voids_no_rewrite ON package_credit_voids")
    op.execute("DROP FUNCTION IF EXISTS package_credit_voids_append_only()")
    op.drop_table("package_credit_voids")
