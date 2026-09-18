"""The second factor: enrolment on `users`, recovery codes, and the per-business policy.

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-18

`mfa_required_for_admin` defaults to **true**, which is the product decision (PRD §1,
tech-stack §14) and not a neutral one: every existing instance's administrators are asked to
enrol the next time they sign in. That is the intent — the flag exists so a solo operator can
turn it *off* deliberately, not so a deployment can drift into having no second factor
because nobody switched it on.

`mfa_secret` holds an AES-256-GCM ciphertext (`core/crypto.py`), never a base32 secret. The
column is wide enough for the base64 of a nonce plus a sealed 32-character secret with room
to spare; it is not sized to the plaintext.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("mfa_secret", sa.String(255), nullable=True))
    op.add_column("users", sa.Column("mfa_method", sa.String(16), nullable=True))
    op.add_column("users", sa.Column("mfa_enrolled_at", sa.DateTime(timezone=True), nullable=True))
    op.create_check_constraint(
        "ck_users_mfa_method", "users", "mfa_method IS NULL OR mfa_method IN ('totp', 'email')"
    )

    op.create_table(
        "mfa_recovery_codes",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column(
            "user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("code_hash", sa.String(64), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    # Every read is "the live codes for this user" — counting what is left, and spending one.
    # Unique per user, not globally. A global unique constraint on the digest means two
    # people can never hold the same code — which is a 1-in-2^40 collision that nobody would
    # ever see, except as an INSERT failing during somebody's enrolment for reasons no error
    # message would explain. The uniqueness worth having is "one row per code per account",
    # and this index is also the lookup the spend does.
    op.create_unique_constraint(
        "uq_mfa_recovery_codes_user_hash", "mfa_recovery_codes", ["user_id", "code_hash"]
    )

    op.add_column(
        "businesses",
        sa.Column(
            "mfa_required_for_admin",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
    )
    op.add_column(
        "businesses",
        sa.Column(
            "mfa_email_otp_allowed", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
    )


def downgrade() -> None:
    op.drop_column("businesses", "mfa_email_otp_allowed")
    op.drop_column("businesses", "mfa_required_for_admin")
    op.drop_constraint("uq_mfa_recovery_codes_user_hash", "mfa_recovery_codes", type_="unique")
    op.drop_table("mfa_recovery_codes")
    op.drop_constraint("ck_users_mfa_method", "users", type_="check")
    op.drop_column("users", "mfa_enrolled_at")
    op.drop_column("users", "mfa_method")
    op.drop_column("users", "mfa_secret")
