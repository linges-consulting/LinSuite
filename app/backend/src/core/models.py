"""The business record: this deployment's own identity.

Single-tenant means exactly one row, so `id` is pinned to 1 by a CHECK constraint — the
database, not the application, refuses a second business. Branding columns arrive with the
branding task; only what the setup wizard collects lives here.
"""

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from core.db import Base


class Business(Base):
    __tablename__ = "businesses"
    __table_args__ = (CheckConstraint("id = 1", name="ck_businesses_single_row"),)

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, server_default="1", autoincrement=False
    )
    name: Mapped[str] = mapped_column(String(200))
    # IANA name. Recurring availability is wall-clock against this; see CLAUDE.md "Time".
    timezone: Mapped[str] = mapped_column(String(64))
    # Set once, when the setup wizard completes. Non-null permanently disables setup.
    setup_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
