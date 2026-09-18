"""User accounts and the roles that authorise them.

One role per user, and the role is the only authorisation signal there is — the `is_admin`
flag it replaced could say one thing about a user and nothing at all about what they were
allowed to do (0006 dropped it). `role_id` is NOT NULL for the same reason: an account with
no role would be an account no capability check can answer about, and every such check would
need a special case for it.

The capability strings in `role_capabilities` are keys from the registry in
`auth/capabilities.py`, validated at the API boundary. The database deliberately does not
carry that list as an enum: capabilities are added and retired by deploying code, and an
enum would make every one of those a migration that has to land in lockstep with it.
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
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.db import Base


class Role(Base):
    """A named set of capabilities.

    `is_system` marks the two roles 0006 seeds. They are read-only through the API —
    `Administrator` is the floor the whole instance stands on, and an administrator who
    could edit it could remove their own ability to edit it back.
    """

    __tablename__ = "roles"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    name: Mapped[str] = mapped_column(String(64), unique=True)
    description: Mapped[str] = mapped_column(String(200), server_default="")
    is_system: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    capabilities: Mapped[list["RoleCapability"]] = relationship(
        back_populates="role", cascade="all, delete-orphan", lazy="selectin"
    )

    @property
    def capability_keys(self) -> set[str]:
        return {c.capability for c in self.capabilities}


class RoleCapability(Base):
    """One granted capability. A row per grant rather than an array column, so revoking one
    is a DELETE of the thing being revoked instead of a rewrite of the whole set."""

    __tablename__ = "role_capabilities"

    role_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True
    )
    capability: Mapped[str] = mapped_column(String(64), primary_key=True)

    role: Mapped[Role] = relationship(back_populates="capabilities")


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
    # RESTRICT, not CASCADE: deleting a role must never delete the people holding it. The
    # roles endpoint refuses to delete a role in use, and this is the database saying the
    # same thing to anything that bypasses it.
    role_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("roles.id", ondelete="RESTRICT"))
    # Eager, because every authenticated request asks for it: `CurrentUser` is what capability
    # checks resolve through, so the role travels with the user row rather than costing a
    # second round trip per check. Loading it per request is also what makes a revoked
    # capability take effect on a session that is already open.
    role: Mapped[Role] = relationship(lazy="selectin")
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

    @property
    def capabilities(self) -> set[str]:
        """What this account may do, as of the request that loaded it."""
        return self.role.capability_keys


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
