"""The availability engine: which starts can actually be booked (tech-stack §19).

A pure function over plain inputs — no session, no clock of its own, no HTTP — which is what
lets `tests/test_availability.py` pin the two DST days, split shifts, buffers, rooms, devices
and concurrency at seam S2 as a table of small scenarios. `scheduling/slots.py` is the loader
that reads the database into these inputs and serves the result.

**Order of operations, and why it is this order.**

1. Each staff member's working blocks are generated for each *local* date in business-local
   wall-clock and converted with `clock.local_blocks_to_instants` — never generated in UTC.
   That is what makes the spring-forward day an hour shorter and the fall-back day an hour
   longer without a special case (`clock.py`).
2. Closure dates are removed whole. Time off and busy intervals are subtracted — busy ones
   only where `max_concurrent` of them already overlap (§20: the stylist running two chairs).
3. Every required space or device must be free for the whole span, turnaround included: the
   room is occupied while it is being turned over. Each resource's free intervals for the day
   are derived once (the day minus its bookings) and the span has to sit inside one of them,
   exactly as with a staff member's. An "any" requirement is satisfied by any one active
   resource of its kind — which one is chosen at booking time. A named resource this function
   was not given is a requirement nothing satisfies, never one that is waived.
4. `buffer_before + duration + buffer_after` is slid across what remains, with the *start* on
   the granularity grid measured from **local midnight** — so a fifteen-minute grid is
   :00/:15/:30/:45 on the clock face even across a transition, and a forty-five-minute one
   does not drift. The whole buffered span has to sit inside one working block.
5. Starts before `now` are dropped.

**Half-open everywhere.** An interval `[start, end)` and a slot that ends exactly when a
booking begins do not overlap, which is the same convention as the `tstzrange` and
`int4range` exclusion constraints in `scheduling/models.py`.

**A wall-clock time the day does not have is not a slot start.** 02:30 on the spring-forward
day resolves (through `clock.localize`) to an instant for the purpose of a *block boundary*,
but it is not offered as a start: the grid is walked with both `fold` values and any grid
time whose round trip does not land back on itself is skipped. On the fall-back day that is
what offers 01:30 twice, an hour apart, both on the grid.

**Busy intervals are inputs, not queries.** Appointments arrive with Task 15; until then the
loader passes empty sets. What an appointment contributes — its own buffers included — is
that ticket's decision, and this function does not need to know.
"""

from collections.abc import Hashable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from datetime import date as Date
from zoneinfo import ZoneInfo

from scheduling.clock import local_blocks_to_instants, localize

# The same 1440 `scheduling/models.py` declares. Spelled out here rather than imported so
# this module stays free of the ORM: it is what lets the S2 tests import it with nothing
# else loaded, and what keeps "pure function" a statement about the imports too.
MINUTES_IN_DAY = 24 * 60

Id = Hashable
Interval = tuple[datetime, datetime]


@dataclass(frozen=True)
class Requirement:
    """`resource_id` None is "any active resource of this kind"."""

    kind: str
    resource_id: Id | None = None


@dataclass(frozen=True)
class ServiceSpec:
    duration_minutes: int
    buffer_before_minutes: int = 0
    buffer_after_minutes: int = 0
    # The *active* eligible staff — what `CatalogServiceOut.staff_ids` already is.
    staff_ids: Sequence[Id] = ()
    requirements: Sequence[Requirement] = ()


@dataclass(frozen=True)
class StaffSpec:
    id: Id
    # ISO weekday (Monday = 0) → `(start_minute, end_minute)` blocks since local midnight.
    hours: Mapping[int, Sequence[tuple[int, int]]]
    time_off: Sequence[Interval] = ()
    max_concurrent: int = 1


@dataclass(frozen=True)
class ResourceSpec:
    """An *active* resource. The loader leaves deactivated ones out; a named requirement on
    one is refused before this function is reached (`CatalogServiceOut.bookable`)."""

    id: Id
    kind: str


@dataclass(frozen=True)
class Slot:
    starts_at: datetime
    # The appointment the client sees — the duration, without the turnaround either side.
    ends_at: datetime
    staff_ids: tuple[Id, ...]


def bookable_slots(
    *,
    timezone: str,
    granularity_minutes: int,
    service: ServiceSpec,
    staff: Iterable[StaffSpec],
    resources: Iterable[ResourceSpec],
    closures: set[Date],
    staff_busy: Mapping[Id, Sequence[Interval]],
    resource_busy: Mapping[Id, Sequence[Interval]],
    dates: Iterable[Date],
    now: datetime,
    horizon_ends_on: Date | None = None,
) -> dict[Date, list[Slot]]:
    """The bookable starts on each of `dates` (business-local), in UTC, in order.

    Every requested date is a key, empty when nothing can be booked — a closure, a day past
    `horizon_ends_on`, a day nobody works. Each slot names every eligible staff member who
    could take it; a caller wanting one person filters `staff` before calling.
    """
    zone = ZoneInfo(timezone)
    providers = [s for s in staff if s.id in set(service.staff_ids)]
    known = {resource.id: resource.kind for resource in resources}
    # Which resources each requirement may be met by. A named one has to be in `resources`
    # (active): naming a resource this function was not told about is *not* "free", it is
    # a requirement nothing can satisfy — the catalog gate refuses that case earlier, and
    # this keeps the engine safe without it.
    candidates = [_candidates(requirement, known) for requirement in service.requirements]
    before = timedelta(minutes=service.buffer_before_minutes)
    length = timedelta(minutes=service.duration_minutes)
    after = timedelta(minutes=service.buffer_after_minutes)

    result: dict[Date, list[Slot]] = {}
    for day in dates:
        result[day] = []
        if day in closures or (horizon_ends_on is not None and day > horizon_ends_on):
            continue

        # Step 3: each resource's free intervals on this local day — the whole day minus what
        # is booked on it — derived once here, the same way a staff member's are below. A
        # buffered span never leaves the day (blocks end at midnight at the latest), so the
        # day window is enough.
        day_window = local_blocks_to_instants([(0, MINUTES_IN_DAY)], day, zone)
        resource_free = {id_: _subtract(day_window, resource_busy.get(id_, ())) for id_ in known}

        def resources_free(span: Interval, free=resource_free) -> bool:
            return all(any(_within(span, free[id_]) for id_ in ids) for ids in candidates)

        grid = [start for start in _grid(day, granularity_minutes, zone) if start >= now]
        takers: dict[datetime, list[Id]] = {}
        for person in providers:
            free = _merged(local_blocks_to_instants(person.hours.get(day.weekday(), ()), day, zone))
            free = _subtract(free, person.time_off)
            free = _subtract(free, _saturated(staff_busy.get(person.id, ()), person.max_concurrent))
            if not free:
                continue
            for start in grid:
                span = (start - before, start + length + after)
                if _within(span, free) and (start in takers or resources_free(span)):
                    takers.setdefault(start, []).append(person.id)

        result[day] = [
            Slot(starts_at=start, ends_at=start + length, staff_ids=tuple(sorted(ids, key=str)))
            for start, ids in sorted(takers.items())
        ]
    return result


def _candidates(requirement: Requirement, known: Mapping[Id, str]) -> list[Id]:
    """The resources that may meet one requirement: the named one if it is known, else every
    known one of the kind. An unknown named resource yields nothing, deliberately."""
    if requirement.resource_id is not None:
        return [requirement.resource_id] if requirement.resource_id in known else []
    return [id_ for id_, kind in known.items() if kind == requirement.kind]


# --- the grid ----------------------------------------------------------------------------------


def _grid(day: Date, step: int, zone: ZoneInfo) -> list[datetime]:
    """The instants of this local day's grid times, in order — each one a wall-clock time the
    day actually has, and the repeated hour's times twice."""
    midnight = datetime.combine(day, time.min)
    instants: set[datetime] = set()
    for minute in range(0, MINUTES_IN_DAY, step):
        local = midnight + timedelta(minutes=minute)
        for fold in (0, 1):
            instant = localize(local.replace(fold=fold), zone)
            if instant.astimezone(zone).replace(tzinfo=None) == local:
                instants.add(instant.astimezone(UTC))
    return sorted(instants)


# --- interval arithmetic, all half-open ----------------------------------------------------------


def _merged(intervals: Iterable[Interval]) -> list[Interval]:
    """Overlapping or touching intervals joined: 09:00–12:00 then 12:00–15:00 is one shift."""
    merged: list[Interval] = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _subtract(free: Sequence[Interval], busy: Iterable[Interval]) -> list[Interval]:
    """What is left of `free` once `busy` is taken out of it."""
    taken = sorted(busy)
    remaining: list[Interval] = []
    for start, end in free:
        cursor = start
        for busy_start, busy_end in taken:
            if busy_end <= cursor or busy_start >= end:
                continue
            if busy_start > cursor:
                remaining.append((cursor, busy_start))
            cursor = max(cursor, busy_end)
            if cursor >= end:
                break
        if cursor < end:
            remaining.append((cursor, end))
    return remaining


def _saturated(intervals: Sequence[Interval], limit: int) -> list[Interval]:
    """Where at least `limit` of `intervals` overlap — the only places a staff member with
    that many chairs is actually unavailable (tech-stack §20)."""
    # An end and a start at the same instant: the end goes first, so touching bookings never
    # count as overlapping. `-1` sorts before `+1`.
    events = sorted(
        [(s, 1) for s, _ in intervals] + [(e, -1) for _, e in intervals], key=lambda e: (e[0], e[1])
    )
    saturated: list[Interval] = []
    depth = 0
    opened: datetime | None = None
    for moment, delta in events:
        depth += delta
        if delta == 1 and depth == limit:
            opened = moment
        elif delta == -1 and depth == limit - 1 and opened is not None:
            if moment > opened:
                saturated.append((opened, moment))
            opened = None
    return saturated


def _within(span: Interval, free: Sequence[Interval]) -> bool:
    start, end = span
    return any(free_start <= start and end <= free_end for free_start, free_end in free)
