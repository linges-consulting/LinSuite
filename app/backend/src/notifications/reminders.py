"""The reminder scheduler (Phase 12 Task 5, #11): a Celery beat task that scans confirmed
appointments and sends the `reminder` notification once per admin-configured interval.

**Local wall-clock, not a raw duration** (CLAUDE.md "Time"; `scheduling/clock.py`'s own
"generate locally, then convert" rule — the same seam `forms/compliance.py` already uses for
its day-boundary logic, per `m3.md`). `businesses.reminder_intervals_hours` (default
`[24, 2]`) means "24 hours before, reckoned on the clock face" — the day before, at the same
local time — not "the appointment's instant minus a 24-hour `timedelta`". Those two disagree
exactly at a DST boundary: subtracting 24 real hours from an instant the day after a fall-back
lands an hour *later* on the local clock than the wall-clock rule intends, because that
particular day was 25 hours long in real time. `reminder_due_at` computes the intended instant
by reading the appointment's own local wall-clock time and converting back with
`scheduling.clock.localize`, inheriting the DST edge-case handling that module already
resolved rather than re-deriving it. `tests/test_reminders.py` pins this against the Nov 1
2026 America/Toronto fall-back — the same transition date `tests/test_clock.py` uses.

**Never double-sent.** `appointment_reminders` has a unique constraint on
`(appointment_id, offset_hours)`; the guard is `INSERT ... ON CONFLICT DO NOTHING RETURNING`,
so only the run that actually claims a given interval for a given appointment goes on to
dispatch — the database is what prevents the race, not an in-process check first (CLAUDE.md
"Concurrency — enforce in the DB, not in app locks").

**Not one of the five trigger functions.** `notifications/triggers.py` has one function per
*event*; a reminder isn't raised by an event, it becomes due at a moment nothing else
observes, so this module calls that file's shared dispatch surface
(`load_business`/`dispatch`/`appointment_context`) directly instead of pretending there is a
sixth trigger.
"""

import asyncio
from collections.abc import Coroutine, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from core.celery_app import celery_app
from core.config import get_settings
from notifications.models import AppointmentReminder
from notifications.triggers import appointment_context, dispatch, load_business
from scheduling.clock import localize
from scheduling.models import Appointment

# A fall-back never adds more than an hour; padding the scan window by that much means an
# appointment whose *wall-clock* reminder instant lands slightly past the naive
# `now + max(interval)` horizon is still inside the window this query fetches.
_HORIZON_PAD = timedelta(hours=1)


# --- S2: pure interval/DST math ----------------------------------------------------------------


def reminder_due_at(starts_at: datetime, offset_hours: int, zone: str | ZoneInfo) -> datetime:
    """The instant the `offset_hours`-before reminder for an appointment starting at
    `starts_at` should fire — reckoned in `zone`'s wall clock (module docstring), not a raw
    `starts_at - timedelta(hours=offset_hours)`."""
    zone_info = zone if isinstance(zone, ZoneInfo) else ZoneInfo(zone)
    local_naive = starts_at.astimezone(zone_info).replace(tzinfo=None)
    return localize(local_naive - timedelta(hours=offset_hours), zone_info)


def reminders_due(
    *,
    starts_at: datetime,
    now: datetime,
    zone: str,
    intervals: Sequence[int],
    already_sent: Sequence[int],
) -> list[int]:
    """Which of `intervals` (hours-before-appointment) have reached their fire instant and are
    not already in `already_sent` — sorted, so a caller sends in a stable order."""
    sent = set(already_sent)
    return sorted(
        offset
        for offset in intervals
        if offset not in sent and reminder_due_at(starts_at, offset, zone) <= now
    )


# --- the scan ------------------------------------------------------------------------------


async def send_due_reminders(db: AsyncSession) -> int:
    """One scheduler pass, against a caller-supplied session (a test's own, or the task's
    fresh one below). Every confirmed appointment starting soon enough that one of its
    intervals might be due is loaded once; each due-and-unclaimed interval is guarded into
    `appointment_reminders` and, only if that insert actually claimed it, dispatched. Returns
    how many reminders were sent — tests read this back without inspecting Celery at all."""
    business = await load_business(db)
    intervals = business.reminder_intervals_hours or []
    if not intervals:
        return 0
    now = datetime.now(UTC)
    horizon = now + timedelta(hours=max(intervals)) + _HORIZON_PAD
    appointments = (
        await db.scalars(
            select(Appointment).where(
                Appointment.status == "confirmed",
                Appointment.starts_at >= now,
                Appointment.starts_at <= horizon,
            )
        )
    ).all()
    sent_count = 0
    for appointment in appointments:
        already = (
            await db.scalars(
                select(AppointmentReminder.offset_hours).where(
                    AppointmentReminder.appointment_id == appointment.id
                )
            )
        ).all()
        due = reminders_due(
            starts_at=appointment.starts_at,
            now=now,
            zone=business.timezone,
            intervals=intervals,
            already_sent=already,
        )
        for offset in due:
            claimed = await db.scalar(
                insert(AppointmentReminder)
                .values(appointment_id=appointment.id, offset_hours=offset)
                .on_conflict_do_nothing(index_elements=["appointment_id", "offset_hours"])
                .returning(AppointmentReminder.appointment_id)
            )
            if claimed is None:
                continue  # another pass already claimed this interval for this appointment
            await db.commit()
            await dispatch(
                db,
                business,
                "reminder",
                customer_id=appointment.customer_id,
                email=appointment.customer.email,
                phone=appointment.customer.phone,
                context=appointment_context(business, appointment),
            )
            sent_count += 1
    return sent_count


# --- Celery --------------------------------------------------------------------------------


def _run(work: Coroutine[Any, Any, int]) -> int:
    """`asyncio.run`, from a worker (no loop running) or from an eager call made inside a
    request/test that already has one of its own — `notifications/failures.py::_run`'s same
    reasoning, copied rather than imported (this module has no other reason to depend on
    that one)."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(work)
    else:
        with ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, work).result()


async def _send_due_reminders_own_session() -> int:
    # A fresh engine on this call's own event loop, the same shape `notifications/failures.py`
    # and `forms/tasks.py` use: a worker call has no loop of its own, and a pooled connection
    # from a previous one is unusable here.
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as db:
            return await send_due_reminders(db)
    finally:
        await engine.dispose()


@celery_app.task(name="notifications.send_appointment_reminders")
def send_appointment_reminders() -> int:
    return _run(_send_due_reminders_own_session())
