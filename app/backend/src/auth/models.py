"""User accounts.

`is_admin` is the bootstrap administrator marker the setup wizard sets. Capability-scoped
RBAC replaces it as the authority for permissions; this flag stays as the "there is at
least one administrator" guarantee.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, String, func, text
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
