"""User accounts.

`is_admin` is the bootstrap administrator marker the setup wizard sets. Capability-scoped
RBAC replaces it as the authority for permissions; this flag stays as the "there is at
least one administrator" guarantee.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, Index, Integer, String, func, text
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
