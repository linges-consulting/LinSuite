"""The append-only audit log (ADR-0002).

One writer, so every event has the same shape and nobody invents a second table. Written
inside the caller's transaction, never batched and never handed to Celery: a dropped entry
is precisely the entry that matters.

Identifiers and small facts only. Nothing that was viewed or submitted is copied in here.
"""

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from core.models import AuditEvent


def record_event(
    session: AsyncSession,
    event_type: str,
    *,
    target_type: str,
    target_id: str | None = None,
    actor_user_id: uuid.UUID | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Stage an event. The caller's `commit` is what makes it real — deliberately, so the
    record and the thing it records land together or not at all."""
    session.add(
        AuditEvent(
            event_type=event_type,
            target_type=target_type,
            target_id=target_id,
            actor_user_id=actor_user_id,
            event_metadata=metadata or {},
        )
    )
