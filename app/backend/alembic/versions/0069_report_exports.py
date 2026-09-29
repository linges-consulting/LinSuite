"""Generalise `commission_exports` into `report_exports` (#86, M5 spec #82).

The commission CSV export queue becomes the shared mechanism any report kind uses:
`kind` + `params` (jsonb) replace the commission-specific `from_date`/`to_date`/`staff_id`
columns, and `expires_at` gives every export a 7-day lifetime (ADR-0001 amendment). Existing
rows migrate to `kind='commission'` with their date range and staff filter moved into
`params`, and `expires_at = created_at + 7 days`.

**No CHECK constrains `kind`.** The ticket's own acceptance criterion is "adding a new kind
needs only a builder and its registration" (`core/exports.py`) — a DB CHECK would force a
migration alongside every future kind, which is the simpler alternative the ticket allows
choosing against. Validity is enforced in the app layer: a request for an unregistered kind
is refused before any row is written.

Grants are unchanged by this migration: the baseline (0001) already gives `linsuite_app`
SELECT/INSERT/UPDATE/DELETE on every table by default, and nothing here revokes DELETE — an
export is a working copy, not a record under retention.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0069"
down_revision: str | None = "0068"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD = "commission_exports"
NEW = "report_exports"


def upgrade() -> None:
    op.rename_table(OLD, NEW)
    op.execute(f"ALTER TABLE {NEW} RENAME CONSTRAINT {OLD}_pkey TO {NEW}_pkey")
    op.execute(
        f"ALTER TABLE {NEW} RENAME CONSTRAINT {OLD}_requested_by_fkey TO {NEW}_requested_by_fkey"
    )
    op.execute(f"ALTER TABLE {NEW} RENAME CONSTRAINT ck_{OLD}_status TO ck_{NEW}_status")

    op.add_column(NEW, sa.Column("kind", sa.Text(), nullable=True))
    op.add_column(NEW, sa.Column("params", JSONB(), nullable=True))
    op.add_column(NEW, sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True))

    op.execute(
        f"""
        UPDATE {NEW} SET
            kind = 'commission',
            params = jsonb_build_object(
                'from_date', to_char(from_date, 'YYYY-MM-DD'),
                'to_date', to_char(to_date, 'YYYY-MM-DD'),
                'staff_id', staff_id
            ),
            expires_at = created_at + interval '7 days'
        """
    )

    op.alter_column(NEW, "kind", nullable=False)
    op.alter_column(NEW, "params", nullable=False)
    op.alter_column(NEW, "expires_at", nullable=False)
    # Only new rows going forward use the server default; existing rows keep the value the
    # UPDATE above just gave them.
    op.alter_column(NEW, "expires_at", server_default=sa.text("now() + interval '7 days'"))

    op.drop_column(NEW, "from_date")
    op.drop_column(NEW, "to_date")
    op.drop_column(NEW, "staff_id")


def downgrade() -> None:
    op.add_column(NEW, sa.Column("from_date", sa.Date(), nullable=True))
    op.add_column(NEW, sa.Column("to_date", sa.Date(), nullable=True))
    op.add_column(NEW, sa.Column("staff_id", sa.Uuid(), nullable=True))
    op.execute(
        f"""
        UPDATE {NEW} SET
            from_date = (params ->> 'from_date')::date,
            to_date = (params ->> 'to_date')::date,
            staff_id = NULLIF(params ->> 'staff_id', '')::uuid
        WHERE kind = 'commission'
        """
    )
    # A row of a kind this ticket never shipped (`access_log`/`package_liability`, #87/#88)
    # has no date range to reconstruct into the columns downgrade is putting back — it is a
    # working copy, not a record, so the downgrade drops it rather than leave a NOT NULL
    # column null on the way back to the old shape.
    op.execute(f"DELETE FROM {NEW} WHERE kind != 'commission'")
    op.alter_column(NEW, "from_date", nullable=False)
    op.alter_column(NEW, "to_date", nullable=False)

    op.drop_column(NEW, "expires_at")
    op.drop_column(NEW, "params")
    op.drop_column(NEW, "kind")

    op.execute(f"ALTER TABLE {NEW} RENAME CONSTRAINT ck_{NEW}_status TO ck_{OLD}_status")
    op.execute(
        f"ALTER TABLE {NEW} RENAME CONSTRAINT {NEW}_requested_by_fkey TO {OLD}_requested_by_fkey"
    )
    op.execute(f"ALTER TABLE {NEW} RENAME CONSTRAINT {NEW}_pkey TO {OLD}_pkey")
    op.rename_table(NEW, OLD)
