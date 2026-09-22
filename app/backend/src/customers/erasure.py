"""`POST /api/customers/{customer_id}/erasure`: a client asks to be forgotten (ADR-0001 §3).

The business honours what it lawfully can, at once, and says what it must keep (pre-flight
D5a/D5b). **Split by privilege:** this handler runs as `linsuite_app` and does only what that
role may — in one transaction it records the request, removes contact details, contacts and
notes (always), replaces names and DOB (only when nothing holds them), suppresses the profile,
and audits `customer.erasure_requested`. It then enqueues `customers.tasks.finish_erasure`,
which destroys the document key on the purge role. The purge role is never reached from
here: this module imports the task, never an engine.

**Held** = `retention_expires_at` is non-null and has not passed; `'infinity'` (a chart with
no DOB) is held. A held client keeps name, DOB and visit history — the chart must still
identify its patient — and the answer says until when. The decision is read with the
business row `FOR SHARE` and the customer row `FOR UPDATE`, the order every retention writer
takes them in, so a profile switch or a new clinical entry cannot slip between the decision
and the purge.

A failed enqueue does not fail the request: it is committed, and the nightly
`purge_expired` finishes any request whose client is not held.
"""

import logging
import uuid
from datetime import UTC, date, datetime
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from auth.capabilities import Requires
from auth.session import CurrentUser
from core.access_log import LogAccess
from core.audit import record_event
from core.db import SessionDep
from customers import retention, tasks
from customers.models import ALWAYS_ERASED, ERASED_NAMES, Customer, ErasureRequest

log = logging.getLogger(__name__)

router = APIRouter(prefix="/customers", tags=["customers"])

RETAINED_WHEN_HELD = ("Name", "Date of birth", "Visit history")


def is_held(expires_at: datetime | None, now: datetime) -> bool:
    """The purge trigger's predicate, negated: a hold that exists and has not passed."""
    if expires_at is None:
        return False
    return expires_at == retention.INFINITY or expires_at >= now


def held_until(expires_at: datetime | None, timezone: str) -> date | None:
    """The business-local day the hold ends on; None when there is no hold or no date."""
    if expires_at is None or expires_at == retention.INFINITY:
        return None
    return expires_at.astimezone(ZoneInfo(timezone)).date()


def held_reason(expires_at: datetime, timezone: str) -> str:
    """What the client is told, in plain words."""
    day = held_until(expires_at, timezone)
    if day is None:
        return (
            "Regulated health record with no date of birth on file — retained until one is "
            "recorded and the end of the hold can be worked out"
        )
    return f"Regulated health record — retained until {day.day} {day:%b %Y}, then destroyed"


def retained(*, held: bool) -> list[str]:
    return list(RETAINED_WHEN_HELD) if held else []


class ErasureIn(BaseModel):
    # Why, or how the client asked. For the business's own record; never audited.
    note: Annotated[str | None, Field(max_length=2000)] = None


class ErasureOut(BaseModel):
    """The request, and what was kept and why. `held_until` and `held_reason` derive from the
    DOB, so they are PHI (`core.access_log.PHI_FIELDS`) — this shape leaves only through a
    `LogAccess` route."""

    id: str
    requested_at: datetime
    held: bool
    held_until: date | None
    held_reason: str | None
    retained: list[str]
    purged_at: datetime | None


def erasure_out(
    request: ErasureRequest, held: bool, expires_at: datetime | None, timezone: str
) -> ErasureOut:
    return ErasureOut(
        id=str(request.id),
        requested_at=request.requested_at,
        held=held,
        held_until=held_until(expires_at, timezone) if held else None,
        held_reason=held_reason(expires_at, timezone) if held and expires_at else None,
        retained=retained(held=held),
        purged_at=request.purged_at,
    )


def _instant(value: datetime | None) -> str | None:
    if value is None:
        return None
    return "infinity" if value == retention.INFINITY else value.isoformat()


@router.post(
    "/{customer_id}/erasure",
    status_code=201,
    # `Requires` first: a refusal is not an access. The response names the hold's end date,
    # which discloses the DOB, so it is logged like a profile open.
    dependencies=[
        Depends(Requires("customers.erase")),
        Depends(LogAccess("customer_erasure")),
    ],
)
async def request_erasure(
    customer_id: uuid.UUID, payload: ErasureIn, actor: CurrentUser, db: SessionDep
) -> ErasureOut:
    _, timezone = await retention._rules(db)  # the business row FOR SHARE, and its zone
    customer = await db.get(Customer, customer_id, with_for_update=True, populate_existing=True)
    if customer is None:
        raise HTTPException(status_code=404, detail="No such customer.")
    if customer.suppressed_at is not None:
        raise HTTPException(
            status_code=409, detail="Erasure has already been requested for this client."
        )

    now = datetime.now(UTC)
    expires_at = customer.retention_expires_at
    held = is_held(expires_at, now)
    for field in ALWAYS_ERASED:
        setattr(customer, field, None)
    if not held:
        customer.first_name, customer.last_name = ERASED_NAMES
        # Not through `retention.on_dob_changed`: an unheld client's expiry is NULL or past,
        # and recomputing a nameless tombstone's hold would only invent one.
        customer.date_of_birth = None
    customer.suppressed_at = now
    customer.updated_at = now

    request = ErasureRequest(
        customer_id=customer.id,
        requested_by_user_id=actor.id,
        note=payload.note,
        held_until=expires_at if held else None,
        held_reason=held_reason(expires_at, timezone) if held else None,
    )
    db.add(request)
    await db.flush()
    record_event(
        db,
        "customer.erasure_requested",
        target_type="customer",
        target_id=str(customer.id),
        actor_user_id=actor.id,
        metadata={
            "held": held,
            "held_until": _instant(expires_at) if held else None,
            "request_id": str(request.id),
        },
    )
    await db.commit()

    try:
        tasks.finish_erasure.delay(str(request.id))
    except Exception:
        # Committed already; `purge_expired` sweeps every unfinished, unheld request nightly.
        log.exception("finish_erasure could not be enqueued for request %s", request.id)
    await db.refresh(request)
    return erasure_out(request, held, expires_at, timezone)
