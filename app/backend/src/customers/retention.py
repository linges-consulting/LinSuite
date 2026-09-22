"""When a client's record may be destroyed (ADR-0001 §2, pre-flight D3/D4).

`expiry` is the rule, and the only place it is computed. Everything else here is a writer
that feeds it: `record_clinical_entry` (Phases 8 and 9 call it when a form submission or
session note is inserted — nothing in #7 does), `on_dob_changed` (the one DOB writer in
`customers/routes.py`), and `recompute_all` (a retention-profile or timezone switch).
Appointment completion is deliberately none of these: a booking is not an entry in the chart.

`retention_expires_at` semantics, which the purge job's predicate
(`IS NULL OR < now()` → not held / expired) relies on:

* `NULL` — not held (`general_business`, or no clinical entry yet).
* an instant — held until the end of that local day in the business's zone.
* `'infinity'` (`datetime.max` through asyncpg) — held, expiry unknown: there is a chart but
  no date of birth, and "assume adult" would purge a minor's chart up to 18 years early.

**Where a reading is ambiguous, retention takes the later date.** An early purge is
irreversible; a late one is not. Concretely: a 29 Feb anniversary in a common year is 1 Mar
(the calendar has no 29 Feb, and 28 Feb would be the earlier reading — pre-flight D4's
"Feb 28 … later is safer" contradicts itself and is superseded); the DOB arm is the later of
`dob + 28y` and `(dob + 18y) + 10y`, which differ only for a 29 Feb birthday; "end of day" is
the instant before the next local midnight, which is right even in zones whose clocks fall
back at midnight (23:xx twice); and a timezone change never shortens a hold (`later`).

**Locking.** Every writer reads the business's profile and zone `FOR SHARE`, and a profile
switch takes that row `FOR UPDATE` before its bulk recompute, so a writer never stores an
expiry computed from a profile that is being switched underneath it. Every writer takes the
business row before the customer row (the DOB PATCH included), so a writer and a switch queue
on the business row and never deadlock.
"""

import uuid
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from core.models import Business
from customers.models import Customer
from scheduling.clock import localize

REGULATED_HEALTH = "regulated_health"
GENERAL_BUSINESS = "general_business"
PROFILES = (REGULATED_HEALTH, GENERAL_BUSINESS)

# What asyncpg reads `timestamptz 'infinity'` as, and writes it from. Naive on purpose: that
# is asyncpg's sentinel, and an aware `datetime.max` would overflow on `.astimezone()`.
INFINITY = datetime.max

# Configuration, not a constant, per ADR-0001 — but one number per deployment until a
# business asks for another, and each confirms it with its own college or counsel.
YEARS_AFTER_LAST_ENTRY = 10
AGE_OF_MAJORITY = 18
YEARS_AFTER_BIRTH = AGE_OF_MAJORITY + YEARS_AFTER_LAST_ENTRY


def _years_after(day: date, years: int) -> date:
    """The same day `years` on; 29 Feb in a common year is 1 Mar — the later reading."""
    try:
        return day.replace(year=day.year + years)
    except ValueError:
        return date(day.year + years, 3, 1)


def _end_of_local_day(day: date, zone: str) -> datetime:
    """The last microsecond of `day` in `zone`, in UTC: the next local midnight minus 1 µs.

    Not `localize(23:59:59.999999)`: where clocks fall back at midnight (Santiago, Beirut,
    Cairo, Asunción) 23:xx happens twice and `localize`'s fold=0 picks the first, an hour
    early. Local midnight itself is never ambiguous there, and in a midnight spring-forward
    gap fold=0 resolves to the transition instant, which is also the right boundary.
    `scheduling.clock.localize` keeps fold=0 on purpose — scheduling relies on it."""
    return localize(datetime.combine(day + timedelta(days=1), time.min), zone) - timedelta(
        microseconds=1
    )


def expiry(
    profile: str, date_of_birth: date | None, last_clinical_entry_at: datetime | None, zone: str
) -> datetime | None:
    """`max(last entry + 10y, dob + 28y)` at the end of that local day in `zone`, as a UTC
    instant; `None` when nothing is held; `INFINITY` when held with no DOB to compute from.

    Anything but an explicit `general_business` is treated as regulated: the retain side is
    the recoverable mistake."""
    if last_clinical_entry_at is not None and last_clinical_entry_at.tzinfo is None:
        raise ValueError("a clinical entry is an instant: pass an aware datetime")
    if profile == GENERAL_BUSINESS or last_clinical_entry_at is None:
        return None
    if date_of_birth is None:
        return INFINITY
    entry_day = last_clinical_entry_at.astimezone(ZoneInfo(zone)).date()
    day = max(
        _years_after(entry_day, YEARS_AFTER_LAST_ENTRY),
        _years_after(date_of_birth, YEARS_AFTER_BIRTH),
        _years_after(_years_after(date_of_birth, AGE_OF_MAJORITY), YEARS_AFTER_LAST_ENTRY),
    )
    return _end_of_local_day(day, zone)


def later(old: datetime | None, new: datetime | None) -> datetime | None:
    """The longer of two holds: NULL is no hold, `INFINITY` outlasts any date."""
    if old is None or new is None:
        return new if old is None else old
    if INFINITY in (old, new):
        return INFINITY
    return max(old, new)


async def _rules(db: AsyncSession, *, for_update: bool = False) -> tuple[str, str]:
    """(profile, zone), locked for the rest of the caller's transaction."""
    row = (
        await db.execute(
            select(Business.retention_profile, Business.timezone)
            .where(Business.id == 1)
            .with_for_update(read=not for_update)
        )
    ).one()
    return row.retention_profile, row.timezone


def _recompute(customer: Customer, profile: str, zone: str) -> None:
    customer.retention_expires_at = expiry(
        profile, customer.date_of_birth, customer.last_clinical_entry_at, zone
    )


async def record_clinical_entry(db: AsyncSession, customer_id: uuid.UUID, at: datetime) -> None:
    """A form submission or session note was added to this client's chart at `at`.

    Staged in the caller's transaction (the caller commits, alongside the entry itself).
    `last_clinical_entry_at` only ever moves forward: a back-dated entry never shortens a hold.
    """
    if at.tzinfo is None:
        raise ValueError("a clinical entry is an instant: pass an aware datetime")
    profile, zone = await _rules(db)
    customer = await db.get(Customer, customer_id, with_for_update=True, populate_existing=True)
    if customer is None:
        raise LookupError(f"no customer {customer_id}")
    if customer.last_clinical_entry_at is None or at > customer.last_clinical_entry_at:
        customer.last_clinical_entry_at = at
    _recompute(customer, profile, zone)
    await db.flush()


async def on_dob_changed(db: AsyncSession, customer: Customer) -> None:
    """The one DOB writer (`PATCH /customers/{id}`) calls this after any change, including to
    and from NULL. Staged; the route commits."""
    profile, zone = await _rules(db)
    _recompute(customer, profile, zone)
    await db.flush()


async def recompute_all(db: AsyncSession, *, never_shorten: bool = False) -> dict[str, int]:
    """Every client's expiry, against the business row as the caller has just changed it.
    Call after the change is flushed and before the commit.

    A profile switch recomputes outright — releasing holds is what it is for. A timezone
    change passes `never_shorten=True`: re-reading a past entry's local day in a new zone can
    move it a day earlier, and a zone correction is not a reason for a hold to end sooner,
    so each row keeps the `later` of its old and recomputed value.

    In Python, not SQL, so the rule has one implementation. Only rows whose value moves are
    written, one executemany. Counts are taken after the business row is locked.
    """
    # ponytail: loads (id, dob, entry, expiry) for every client in one request — fine for the
    # thousands a practice has; batch with keyset pagination if a tenant reaches ~10^6.
    profile, zone = await _rules(db, for_update=True)
    rows = (
        await db.execute(
            select(
                Customer.id,
                Customer.date_of_birth,
                Customer.last_clinical_entry_at,
                Customer.retention_expires_at,
            )
        )
    ).all()
    changes, held, released = [], 0, 0
    for row in rows:
        old = row.retention_expires_at
        new = expiry(profile, row.date_of_birth, row.last_clinical_entry_at, zone)
        if never_shorten:
            new = later(old, new)
        held += new is not None
        released += old is not None and new is None
        if new != old:
            changes.append({"id": row.id, "retention_expires_at": new})
    if changes:
        await db.execute(update(Customer), changes)
    return {"changed": len(changes), "held": held, "released": released}
