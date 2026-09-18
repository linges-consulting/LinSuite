"""User accounts.

`is_admin` is the bootstrap administrator marker the setup wizard sets. Capability-scoped
RBAC replaces it as the authority for permissions; this flag stays as the "there is at
least one administrator" guarantee.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from core.db import Base


class User(Base):
    __tablename__ = "users"
    # Addresses are case-insensitive in practice: one account per address, whatever the
    # casing. Stored lowercase; the functional index is what actually enforces it.
    __table_args__ = (Index("uq_users_email_lower", text("lower(email)"), unique=True),)

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    email: Mapped[str] = mapped_column(String(320))
    password_hash: Mapped[str] = mapped_column(String(255))
    is_admin: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    # Set for administrator-created accounts and after suspected compromise (tech-stack §14).
    # Login still succeeds, and the session it opens is refused (403) by `CurrentUser` on
    # every endpoint but the three that let it pay the debt: `GET /auth/me`,
    # `POST /auth/password/change` and logout. The gate is in `auth/session.py`, not in each
    # route, so an endpoint written later inherits it without knowing this column exists.
    must_change_password: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    # What `businesses.password_rotation_days` is measured against, when a business has opted
    # into rotation at all. Not null: "never changed" and "changed at account creation" are
    # the same fact here, and a null would make every comparison a special case.
    password_changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # Every token issued before this instant is dead — one column instead of per-user `jti`
    # bookkeeping in Redis, which would have to enumerate sessions nobody is tracking. Set by
    # a completed reset and by a password change; Task 8 reuses it for MFA reset.
    sessions_revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SetupToken(Base):
    """The first-run token, as a SHA-256 hex digest. One row, or none once setup is done.

    In the database rather than in memory so every worker of every restart agrees on which
    token is valid. Only the digest is stored: the plaintext exists in the boot log and in
    the 0600 file, and nowhere else.
    """

    __tablename__ = "setup_token"
    __table_args__ = (CheckConstraint("id = 1", name="ck_setup_token_single_row"),)

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, server_default="1", autoincrement=False
    )
    token_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PasswordResetToken(Base):
    """A reset link, as a SHA-256 hex digest of the token that was emailed.

    Only the digest is stored, for the same reason as `SetupToken`: a database dump, a backup
    or a stray SELECT must not hand anybody a working link. The plaintext exists in the one
    message that was sent and nowhere else.

    Rows are kept after use rather than deleted — `used_at` is what makes a second attempt a
    refusal instead of a lookup miss, and the distinction is worth having in the trail.
    """

    __tablename__ = "password_reset_tokens"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
