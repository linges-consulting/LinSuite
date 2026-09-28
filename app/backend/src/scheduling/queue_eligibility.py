"""Walk-in queue start eligibility (Phase 7 Task 4, #12) — S2, pure, no session, no clock of
its own.

Answers one question: can *this* staff member start *this* queue entry's service *right now*?
Task 5 (`POST /queue-entries/{id}/start`) is the caller — it resolves the plain inputs below
off the database (the requested service's duration/buffers, the candidate staff member's shift
and current load, `datetime.now(UTC)`) and calls `can_start` as its eligibility gate before
ever touching `offered_slot`/`assign_resources`.

**Reuses the availability engine, does not reimplement it** (`scheduling/availability.py`):

- The shift/time-off/closure/horizon check is `advisory_breaches` itself, called here with the
  exact same `StaffSpec` the loader (`scheduling/slots.py::compute`) already builds for staff
  booking — same shift matrix, same time-off rows, same closures, same horizon. `override`
  lifts exactly this half, mirroring `authorize_override`'s own effect on `broken_rules`
  elsewhere; *authorizing* an override (the capability/Admin-Mode check) is Task 5's job, not
  this pure function's — `can_start` only asks "if the caller already has one, is this OK?"
- The staff-concurrency check reuses `availability._saturated` directly — the exact function
  `bookable_slots` itself calls to decide when a staff member's chairs are all full (tech-stack
  §20, `max_concurrent_appointments_per_staff`) — rather than a second "count the overlaps"
  written here. This mirrors `scheduling/public.py`'s existing precedent of importing a sibling
  module's underscore-prefixed helper directly (`from scheduling.services import _catalog_out,
  _kinds_in_stock, _roster`) instead of re-deriving it.
- Per CLAUDE.md's advisory/physical split: the shift/time-off/closure/horizon rules are
  *advisory* (a human may set them aside, `override=True`); staff concurrency is *physical*
  (never overridable, checked regardless of `override`) — matching `bookable_slots`'s own
  order of operations exactly.

**Resources are out of scope for this function, deliberately.** #12's acceptance criteria name
staff and booked-appointment overlap specifically ("a walk-in cannot be started if it would run
into a booked appointment") — nothing about a room or device. Task 5's actual `start` endpoint
checks physical resources the same way every other booking path does, through
`scheduling.appointments.assign_resources` against the service's real requirements; duplicating
that here would be a second, narrower implementation of the same check. `ServiceSpec.staff_ids`
and `.requirements` are therefore unused by `can_start` even though `service` accepts a full
`ServiceSpec` (reusing the existing type rather than inventing a three-int one) — only
`duration_minutes`/`buffer_before_minutes`/`buffer_after_minutes` are read.

**Not on a grid.** A walk-in starts now, not on the booking grid's quarter-hour marks the way a
scheduled appointment does — so this checks one specific span (`now` buffered fore and aft by
the service's own turnaround) rather than walking `bookable_slots`'s grid, which is built for
"which of these many starts are offered", a different question from "is this one instant OK".
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date as Date
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from scheduling.availability import Interval, ServiceSpec, StaffSpec, _saturated, advisory_breaches

# The one physical reason `can_start` names on top of `availability.ADVISORY_RULES` — a busy
# staff member at their concurrency limit, never overridable (module docstring).
STAFF_BUSY = "staff_busy"


@dataclass(frozen=True)
class Eligibility:
    """`can_start`'s answer: eligible, or not with why. `reason` is `None` exactly when
    `eligible` is `True` — one of `availability.ADVISORY_RULES` (overridable) or `STAFF_BUSY`
    (never overridable), the same vocabulary `broken_rules`'s callers already switch on."""

    eligible: bool
    reason: str | None = None


def can_start(
    *,
    staff: StaffSpec,
    service: ServiceSpec,
    timezone: str,
    now: datetime,
    staff_busy: Sequence[Interval] = (),
    closures: frozenset[Date] = frozenset(),
    horizon_ends_on: Date | None = None,
    override: bool = False,
) -> Eligibility:
    """Whether `staff` can start `service` for a waiting queue entry at `now`.

    `staff_busy` is this staff member's already-buffered busy spans — exactly what
    `scheduling/slots.py::busy_intervals` already returns for one staff id (own buffers
    included, cancelled/no-show excluded already, `availability.NOT_OCCUPYING`'s own rule) —
    over a window wide enough to include their next appointment, whenever it is; `can_start`
    does no querying of its own, so how far ahead to fetch is the caller's (Task 5's) call.

    Order matches `bookable_slots`'s own: the physical check (staff concurrency) first, since
    it is never overridable either way; the advisory check (shift/time-off/closure/horizon)
    second, lifted when `override=True`.
    """
    turnaround = timedelta(minutes=service.duration_minutes + service.buffer_after_minutes)
    span = (
        now - timedelta(minutes=service.buffer_before_minutes),
        now + turnaround,
    )

    # Physical: a staff member already at their concurrency limit for any part of this span —
    # "free for the entire service duration before their next booked appointment" is exactly
    # "the span touches no saturated region", the same test `bookable_slots` runs per grid
    # start, just for the one span a walk-in start needs rather than a whole day of them.
    saturated = _saturated(staff_busy, staff.max_concurrent)
    if any(sat_start < span[1] and span[0] < sat_end for sat_start, sat_end in saturated):
        return Eligibility(False, STAFF_BUSY)

    # Advisory: the shift/time-off/closure/horizon rules, exactly as staff booking's own
    # `broken_rules` asks them — `override` is this function's caller having already decided
    # an override applies (Task 5 authorizes it the same way `authorize_override` does today);
    # this function only answers what an override would be setting aside.
    zone = ZoneInfo(timezone)
    breaches = advisory_breaches(
        timezone=timezone,
        staff=staff,
        day=now.astimezone(zone).date(),
        span=span,
        closures=closures,
        horizon_ends_on=horizon_ends_on,
    )
    if breaches and not override:
        return Eligibility(False, breaches[0])

    return Eligibility(True, None)
