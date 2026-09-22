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

**Locking.** Every writer reads the business's profile and zone `FOR SHARE`, and a profile
switch takes that row `FOR UPDATE` before its bulk recompute, so a writer never stores an
expiry computed from a profile that is being switched underneath it. The two can deadlock
(a DOB PATCH has already locked its customer row when it asks for the business row); Postgres
then aborts one of them whole, which is an error for somebody to retry, never a wrong date.
"""

import uuid
from datetime import date, datetime, time
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
YEARS_AFTER_BIRTH = 18 + 10


def _years_after(day: date, years: int) -> date:
    """The same day `years` on; 29 Feb in a common year is 28 Feb (pre-flight D4)."""
    try:
        return day.replace(year=day.year + years)
    except ValueError:
        return day.replace(year=day.year + years, day=28)


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
    )
    return localize(datetime.combine(day, time.max), zone)


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


async def recompute_all(db: AsyncSession) -> dict[str, int]:
    """Every client's expiry, against the business row as the caller has just changed it —
    a profile or timezone switch. Call after the change is flushed and before the commit.

    In Python, not SQL, so the rule has one implementation. Only rows whose value moves are
    written, one executemany. Returns `{"changed": n, "held": m}` for the audit event.
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
    changes, held = [], 0
    for row in rows:
        new = expiry(profile, row.date_of_birth, row.last_clinical_entry_at, zone)
        held += new is not None
        if new != row.retention_expires_at:
            changes.append({"id": row.id, "retention_expires_at": new})
    if changes:
        await db.execute(update(Customer), changes)
    return {"changed": len(changes), "held": held}
