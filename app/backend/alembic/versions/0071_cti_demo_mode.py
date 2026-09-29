"""`businesses.demo_mode` and a functional index for phone lookup (Phase 14, #16).

Revision ID: 0071
Revises: 0070
Create Date: 2026-09-29

**`demo_mode`** (`core/models.py::Business`) is the admin toggle CLAUDE.md's CTI note promises:
off by default, so a working clinic never sees the "simulate incoming call" control anywhere
in the product — `scheduling/cti.py`'s simulate endpoint 404s while it is off, the same "whole
surface gated by a business toggle" shape `enable_walk_in_queue` already established (0040).

**The functional index** is what makes phone lookup (`scheduling/cti.py`, `customers/phone.py`)
fast without a second, denormalised phone column that could drift from `customers.phone`.
`customers.phone` is already digits-only (`customers/routes.py::_digits`), but not
consistently: a number entered as `"+1 416 555 0199"` keeps its leading `1` (11 digits) where
one entered as `"416-555-0199"` does not (10) — nothing today reconciles the two, so a caller
ID's `"4165550199"` would miss a chart stored the first way. `customers/phone.py::
normalize_phone` and this index apply the identical NANP `+1`/leading-`1` transform on read
and on write side respectively, so a prefix search on the normalised value matches a stored
number however it was originally entered. The `CASE` expression here must stay byte-for-byte
what `customers/phone.py::normalized_phone_column` compiles to, or Postgres will not recognise
the two as the same expression and the index goes unused (still correct, just unindexed).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0071"
down_revision: str | None = "0070"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Kept in sync with `customers/phone.py::_PHONE_CASE_SQL` — see that module's docstring.
_PHONE_CASE_SQL = (
    "CASE WHEN length(phone) = 11 AND left(phone, 1) = '1' THEN substr(phone, 2) ELSE phone END"
)


def upgrade() -> None:
    op.add_column(
        "businesses",
        sa.Column("demo_mode", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.execute(
        f"CREATE INDEX ix_customers_phone_normalized ON customers (({_PHONE_CASE_SQL})) "
        "WHERE phone IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX ix_customers_phone_normalized")
    op.drop_column("businesses", "demo_mode")
