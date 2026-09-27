"""`GET /api/public/booking/availability` (Phase 6 Task 1, #10): the client-facing counterpart
to `scheduling/slots.py`'s `GET /availability`, over the client's own unauthenticated request.

No capability, no session — mounted under `/public/booking` (`main.py`'s `/api/public/`
prefix), which inherits Traefik's `public`/`public-ratelimit` middleware and `main.py`'s
`Cache-Control: no-store`/`Referrer-Policy: no-referrer`/Origin-check treatment for the whole
prefix (`forms/public.py`'s pattern, mirrored exactly) — no second Traefik router, and no
route-local throttle to duplicate what Traefik already applies: unlike `forms/public.py`'s
token lookup, there is nothing secret here to guess, only a service id and a date range, so
the app-level Redis throttle that guards a guessable token does not apply.

**`relax_advisory` is not reachable from here, structurally.** `scheduling/slots.py`'s
`resolve_availability` is the one function both this route and the staff route call, and it
has no parameter of that name at all — the override/relax switch (`scheduling/availability.
py`'s and `scheduling/appointments.py`'s docstrings) is reached from nowhere but the staff
booking path's diagnose step and its actual override. A client can never see an
out-of-shift, time-off, closure or beyond-horizon slot through this endpoint even when one
exists for staff (`tests/test_booking_public.py`'s adversarial case).

**`bookable_online` is the per-service gate** (`scheduling/services.py`'s `ServiceFields`,
already stored since M1/M2, unused anywhere until this task). A service an administrator has
not opted into online booking is a 404 here — the same generic "No such service." `catalog_
entry` already gives an unknown or inactive one, since there is no reason a client should be
able to tell "exists but is in-person-only" apart from "does not exist". The separate,
*business-wide* `online_booking_enabled` toggle (Phase 6 Task 4) is a later gate on the whole
portal; this route does not check it, per this task's own scope note in `m3.md`."""

import uuid
from datetime import date as Date
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query

from core.db import SessionDep
from scheduling.services import catalog_entry
from scheduling.slots import AvailabilityOut, check_range, resolve_availability

router = APIRouter(prefix="/public/booking", tags=["public"])


@router.get("/availability", response_model=AvailabilityOut)
async def public_availability(
    db: SessionDep,
    service_id: uuid.UUID,
    from_: Annotated[Date, Query(alias="from")],
    to: Date,
    staff_id: uuid.UUID | None = None,
):
    """Bookable slots for `service_id`, business-local `from`..`to` inclusive (at most 31
    days) — the same shape `GET /availability` returns. `staff_id` omitted means "any
    eligible staff member", exactly as it does there (`CatalogServiceOut.staff_ids`, the
    engine's own "any available" mode — not reinvented here)."""
    check_range(from_, to)
    service = await catalog_entry(db, service_id)
    if not service.bookable_online:
        raise HTTPException(status_code=404, detail="No such service.")
    return await resolve_availability(db, service, from_, to, staff_id)
