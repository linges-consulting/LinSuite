"""The first-run setup token's digest.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-18

One row at most. It exists while the instance is unclaimed and is deleted on completion,
so every process — every worker, every restart — agrees on which token is valid without
anyone holding the plaintext.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "setup_token",
        sa.Column("id", sa.Integer(), server_default="1", primary_key=True, autoincrement=False),
        # SHA-256 hex of the token. The token is 256 bits of urandom, so there is nothing
        # for a slow hash to defend against here.
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("id = 1", name="ck_setup_token_single_row"),
    )


def downgrade() -> None:
    op.drop_table("setup_token")
