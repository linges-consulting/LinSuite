"""Stillwater brand defaults: a new business starts on the shipped theme's colours.

Revision ID: 0077
Revises: 0076
Create Date: 2026-09-30

The shipped theme moved to tweakcn's Stillwater (docs/DESIGN.md), so the default brand pair
moves with it: primary `#1d4ed8` -> `#1a6289`, secondary `#0f766e` -> `#2f7a5c`.

A business still on the *untouched* old pair (both colours exactly the old defaults) is moved
too, so an install that never opened Settings -> Branding matches the new theme. One that chose
either colour itself keeps both: a deliberate choice is never overwritten.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0077"
down_revision: str | None = "0076"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD = ("#1d4ed8", "#0f766e")
NEW = ("#1a6289", "#2f7a5c")


def _move(frm: tuple[str, str], to: tuple[str, str]) -> None:
    op.execute(f"ALTER TABLE businesses ALTER COLUMN brand_primary SET DEFAULT '{to[0]}'")
    op.execute(f"ALTER TABLE businesses ALTER COLUMN brand_secondary SET DEFAULT '{to[1]}'")
    op.execute(
        f"UPDATE businesses SET brand_primary = '{to[0]}', brand_secondary = '{to[1]}' "
        f"WHERE brand_primary = '{frm[0]}' AND brand_secondary = '{frm[1]}'"
    )


def upgrade() -> None:
    _move(OLD, NEW)


def downgrade() -> None:
    _move(NEW, OLD)
