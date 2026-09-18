"""Password reset tokens, the forced change, and blanket session revocation.

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-18

`users.sessions_revoked_at` is how a reset kills sessions it cannot enumerate. The `jti`
denylist in Redis ends one named token; nothing there can answer "every token this user
holds", because nobody keeps that list. A single instant does: a session token is refused
when it was issued before it. One column, no bookkeeping, and it survives a Redis flush —
which the denylist does not, and a revocation that a cache eviction undoes is not one.

`businesses.password_rotation_days` is nullable with no default on purpose: null is off, and
off is what NIST recommends without evidence of compromise.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "must_change_password",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )
    op.add_column(
        "users",
        sa.Column(
            "password_changed_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.add_column("users", sa.Column("sessions_revoked_at", sa.DateTime(timezone=True)))
    op.add_column("businesses", sa.Column("password_rotation_days", sa.Integer(), nullable=True))

    op.create_table(
        "password_reset_tokens",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column(
            "user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # The digest only. The token itself is in the one message that was sent.
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        # Non-null is what makes the link single-use; the row is kept, not deleted.
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    # A reset invalidates every other outstanding link for that user, which is this lookup.
    op.create_index("ix_password_reset_tokens_user_id", "password_reset_tokens", ["user_id"])


def downgrade() -> None:
    op.drop_table("password_reset_tokens")
    op.drop_column("businesses", "password_rotation_days")
    op.drop_column("users", "sessions_revoked_at")
    op.drop_column("users", "password_changed_at")
    op.drop_column("users", "must_change_password")
