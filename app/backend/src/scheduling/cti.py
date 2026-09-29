"""`/api/cti`: the CTI surface built without the telephony behind it (Phase 14, #16).

PRD §3's three pieces, none of them needing a live call: **phone lookup** is a real feature on
its own — staff enter a caller's number and get the same profile summary, classification and
previous-provider history the booking screen's search already knows how to fetch, plus enough
of the customer record to jump straight into booking them again. The **screen-pop panel** on
the frontend is driven by an event-source abstraction, not by this module — nothing here pushes
anything to a browser (CLAUDE.md: no WebSocket layer in v1). What this module *does* provide
that panel is the one thing a fake incoming call needs: `simulate-call`, gated shut behind
`businesses.demo_mode` (off by default) the same structural way `scheduling/queue.py` gates its
whole surface behind `enable_walk_in_queue` — a 404 before anything else runs, so "no simulate
control is reachable anywhere" (the ticket's own acceptance criterion) holds at the API, not
only in whatever the frontend chooses to render. `demo-mode` is a tiny, capability-gated read so
the frontend can decide whether to render that control at all without needing `admin` (the
toggle itself lives on the Admin-Mode-gated settings panel, `settings/notifications_routes.py`;
reading whether it is on does not).

**One capability throughout, `customers.view`** — the same one that already gates customer
search and the booking dialog's picker (decision #2: "no telephony... this is simply search").
Staff Mode is enough; nothing here is administrative.

**Logging.** A lookup that narrows to several candidates is treated exactly like the existing
customer search (ADR-0002 §4: a list render, not a read) — no `LogAccess` anywhere. A lookup
that resolves to exactly one customer discloses that person's profile and appointment history,
which is a PHI access the same way opening their profile from the Clients list is — logged with
`core.access_log.log_each_named`, the same helper `billing/package_liability.py`'s report uses
for "one row per client this response actually names." This module's routes are not
`{customer_id}`-scoped, so `tests/test_access_log.py`'s route-enumeration check (which reads
`/api/customers/{customer_id}...` paths and the `"phi"` tag) does not see them — the logging
here is a deliberate, hand-checked rule (see `tests/test_cti.py`), not a blind spot in that
check.
"""

import random
import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.capabilities import Requires
from auth.models import User
from auth.session import CurrentUser
from core.access_log import log_each_named
from core.db import SessionDep
from core.forms import refuse
from customers.models import Customer
from customers.phone import MIN_PARTIAL_DIGITS, normalize_phone, normalized_phone_column
from customers.routes import CustomerOut, classifications_of, customer_out
from notifications.triggers import load_business
from scheduling.models import Appointment

router = APIRouter(prefix="/cti", tags=["cti"])

Viewer = Annotated[User, Depends(Requires("customers.view"))]

# A screenful of candidates is plenty for a partial-digit search at the front desk — this is
# a caller-id lookup, not the Clients list.
_MAX_RESULTS = 10
# How many distinct previous providers the match carries — the point is "have they been seen
# here before and by whom", not a full visit history (the profile `GET` is that).
_MAX_PROVIDERS = 5


# --- demo mode: the read side of the settings toggle -----------------------------------------


class DemoModeOut(BaseModel):
    enabled: bool


@router.get("/demo-mode")
async def demo_mode(_: Viewer, db: SessionDep) -> DemoModeOut:
    """Whether the settings panel's `demo_mode` toggle is on — so the frontend can decide
    whether to render the simulate control at all, without needing `admin` (the toggle's own
    write path already requires it; reading it back here does not)."""
    business = await load_business(db)
    return DemoModeOut(enabled=business.demo_mode)


# --- the simulated incoming call --------------------------------------------------------------


class SimulatedCallOut(BaseModel):
    call_id: str
    phone: str
    received_at: datetime


@router.post("/simulate-call")
async def simulate_call(_: Viewer, db: SessionDep) -> SimulatedCallOut:
    """A fake incoming call, for the demo. Nothing is persisted (decision #1) — this is a pure
    generator, not an event log. 404 while `demo_mode` is off, the same structural gate
    `scheduling/queue.py::_feature_gate` already uses for a whole surface nobody has opted
    into."""
    business = await load_business(db)
    if not business.demo_mode:
        raise HTTPException(status_code=404, detail="Not found.")

    numbers = list(
        await db.scalars(
            select(Customer.phone).where(
                Customer.suppressed_at.is_(None), Customer.phone.is_not(None)
            )
        )
    )
    # Usually a real customer's number, so the screen-pop panel has something to show off;
    # sometimes an unmatched one, so the "no match" state can be demonstrated too (decision
    # #1). A fixed four-in-five split — a demo knob nobody has ever asked to configure.
    # ponytail: no collision check against `numbers` for the unmatched branch — a random
    # 10-digit draw landing on an existing customer's number is a rounding error for a demo,
    # not a correctness requirement; revisit if that ever actually happens in front of a client.
    if numbers and random.random() < 0.8:  # noqa: S311 — a demo prop, not a security control
        phone = random.choice(numbers)  # noqa: S311
    else:
        phone = str(random.randint(2_000_000_000, 9_999_999_999))  # noqa: S311
    return SimulatedCallOut(call_id=str(uuid.uuid4()), phone=phone, received_at=datetime.now(UTC))


# --- phone lookup: the real feature -----------------------------------------------------------


class PreviousProviderOut(BaseModel):
    staff_id: str
    display_name: str
    colour: str
    service_name: str
    last_visit_at: datetime


class PhoneLookupMatchOut(BaseModel):
    """What a single resolved match carries: the profile summary and classification (already
    non-PHI, the same shape the Clients list sends), who has seen this client before, and —
    being `CustomerOut` itself — everything the booking dialog needs to preselect them for a
    quick-book (decision #2)."""

    customer: CustomerOut
    previous_providers: list[PreviousProviderOut]


class PhoneLookupOut(BaseModel):
    status: Literal["no_match", "candidates", "match"]
    # Populated only when `status == "candidates"` — several people share this prefix.
    candidates: list[CustomerOut]
    # Populated only when `status == "match"` — exactly one customer, resolved.
    match: PhoneLookupMatchOut | None


async def _previous_providers(
    db: AsyncSession, customer_id: uuid.UUID
) -> list[PreviousProviderOut]:
    """Distinct staff from this client's completed visits, most recent first, capped."""
    visits = list(
        await db.scalars(
            select(Appointment)
            .where(Appointment.customer_id == customer_id, Appointment.status == "completed")
            .order_by(Appointment.starts_at.desc())
        )
    )
    seen: dict[uuid.UUID, PreviousProviderOut] = {}
    for visit in visits:
        if visit.staff_id in seen:
            continue
        seen[visit.staff_id] = PreviousProviderOut(
            staff_id=str(visit.staff_id),
            display_name=visit.staff.display_name,
            colour=visit.staff.colour,
            service_name=visit.service.name,
            last_visit_at=visit.starts_at,
        )
        if len(seen) >= _MAX_PROVIDERS:
            break
    return list(seen.values())


@router.get("/lookup")
async def phone_lookup(
    user: CurrentUser,
    request: Request,
    db: SessionDep,
    _: Viewer,
    phone: str,
) -> PhoneLookupOut:
    """Matches on the normalised digits (`customers/phone.py`), by prefix — a receptionist
    reading digits off a caller ID one at a time gets candidates as they type, the same
    partial-match shape `find_customers` already offers by name. Erased clients are excluded,
    exactly as that search already excludes them.

    **One match logs like a profile open; several log nothing, like a search** (module
    docstring, decision #2)."""
    digits = normalize_phone(phone)
    if len(digits) < MIN_PARTIAL_DIGITS:
        raise refuse("phone", f"Enter at least {MIN_PARTIAL_DIGITS} digits.", where="query")

    expr = normalized_phone_column(Customer.phone)
    rows = list(
        await db.scalars(
            select(Customer)
            .where(Customer.suppressed_at.is_(None), expr.startswith(digits, autoescape=True))
            .order_by(Customer.last_name, Customer.first_name, Customer.id)
            .limit(_MAX_RESULTS)
        )
    )
    if not rows:
        return PhoneLookupOut(status="no_match", candidates=[], match=None)

    classifications = await classifications_of(db, [c.id for c in rows])
    if len(rows) > 1:
        return PhoneLookupOut(
            status="candidates",
            candidates=[customer_out(c, classifications[c.id]) for c in rows],
            match=None,
        )

    customer = rows[0]
    providers = await _previous_providers(db, customer.id)
    # Resolved to one person: a PHI access, logged before the response is built (module
    # docstring) — fail closed, the same order `core.access_log._record` uses.
    await log_each_named(db, user, request, [customer.id], "customer_phone_lookup")
    return PhoneLookupOut(
        status="match",
        candidates=[],
        match=PhoneLookupMatchOut(
            customer=customer_out(customer, classifications[customer.id]),
            previous_providers=providers,
        ),
    )
