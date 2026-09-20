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
   room is occupied while it is being turned over. An "any" requirement is satisfied by any
   one active resource of its kind being free for the whole span — which one is chosen at
   booking time.
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
from scheduling.models import MINUTES_IN_DAY

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
    by_kind: dict[str, list[Id]] = {}
    for resource in resources:
        by_kind.setdefault(resource.kind, []).append(resource.id)
    before = timedelta(minutes=service.buffer_before_minutes)
    length = timedelta(minutes=service.duration_minutes)
    after = timedelta(minutes=service.buffer_after_minutes)

    def resources_free(span: Interval) -> bool:
        for requirement in service.requirements:
            candidates = (
                [requirement.resource_id]
                if requirement.resource_id is not None
                else by_kind.get(requirement.kind, [])
            )
            if not any(_clear(span, resource_busy.get(r, ())) for r in candidates):
                return False
        return True

    result: dict[Date, list[Slot]] = {}
    for day in dates:
        result[day] = []
        if day in closures or (horizon_ends_on is not None and day > horizon_ends_on):
            continue

        takers: dict[datetime, list[Id]] = {}
        for person in providers:
            free = _merged(local_blocks_to_instants(person.hours.get(day.weekday(), ()), day, zone))
            free = _subtract(free, person.time_off)
            free = _subtract(free, _saturated(staff_busy.get(person.id, ()), person.max_concurrent))
            if not free:
                continue
            for start in _grid(day, granularity_minutes, zone):
                if start < now:
                    continue
                span = (start - before, start + length + after)
                if _within(span, free) and (start in takers or resources_free(span)):
                    takers.setdefault(start, []).append(person.id)

        result[day] = [
            Slot(starts_at=start, ends_at=start + length, staff_ids=tuple(sorted(ids, key=str)))
            for start, ids in sorted(takers.items())
        ]
    return result


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


def _clear(span: Interval, busy: Iterable[Interval]) -> bool:
    start, end = span
    return all(end <= busy_start or start >= busy_end for busy_start, busy_end in busy)
