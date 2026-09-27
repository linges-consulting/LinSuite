"""S2 + S1: the reminder scheduler (Phase 12 Task 5, #11).

**S2** (`test_reminders_due`, `test_a_..._dst_...`): `reminder_due_at`/`reminders_due` are
pure — no database, no Celery — which is what makes the DST case (#11's own named acceptance
criterion) cheap to pin exactly: the Nov 1 2026 America/Toronto fall-back, the same transition
date `tests/test_clock.py` uses for `scheduling/clock.py`'s own DST tests.

**S1** (`test_the_scheduler_...`): one pass of `send_due_reminders` against a real appointment,
proving the guard table actually stops a second pass from sending twice — the acceptance
criterion's "actual teeth", not the interval math (already proven at S2).
"""

import uuid
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import text

from core.db import session_scope
from notifications.reminders import reminder_due_at, reminders_due, send_due_reminders
from scheduling.clock import localize
from tests.test_appointments import claimed_instance  # noqa: F401 — the autouse fixture
from tests.test_form_links import make_email_ready

TORONTO = "America/Toronto"
# Same transition dates `tests/test_clock.py` pins `scheduling/clock.py` against: clocks in
# Toronto go forward on 8 March 2026 (02:00 -> 03:00) and back on 1 November 2026 (02:00 -> 01:00).
FALL_BACK = date(2026, 11, 1)
SPRING_FORWARD = date(2026, 3, 8)


# --- S2: reminder_due_at / reminders_due --------------------------------------------------


def test_an_ordinary_day_has_no_dst_discrepancy():
    starts_at = localize(datetime(2026, 6, 16, 9, 0), TORONTO)  # an ordinary Tuesday
    due = reminder_due_at(starts_at, 24, TORONTO)
    assert due == starts_at - timedelta(hours=24)


def test_a_24_hour_reminder_respects_the_business_timezone_across_the_fall_back():
    # The appointment is the day of the transition itself; 24 hours before reaches back across
    # it into the day that was still on daylight time.
    starts_at = localize(datetime(FALL_BACK.year, FALL_BACK.month, FALL_BACK.day, 9, 0), TORONTO)
    due = reminder_due_at(starts_at, 24, TORONTO)

    # The intended local-time offset: the same 09:00, one calendar day earlier.
    assert due == localize(datetime(2026, 10, 31, 9, 0), TORONTO)
    # A naive `starts_at - timedelta(hours=24)` lands an hour later on the clock, because the
    # day in between gained an hour when the clocks fell back.
    naive = starts_at - timedelta(hours=24)
    assert due != naive
    assert naive - due == timedelta(hours=1)


def test_a_reminder_respects_the_business_timezone_across_the_spring_forward():
    # The appointment is the day of the transition itself (after the 02:00-03:00 gap); 24
    # hours before reaches back into the day before, which was an hour short in real time.
    starts_at = localize(
        datetime(SPRING_FORWARD.year, SPRING_FORWARD.month, SPRING_FORWARD.day, 9, 0), TORONTO
    )
    due = reminder_due_at(starts_at, 24, TORONTO)

    assert due == localize(datetime(2026, 3, 7, 9, 0), TORONTO)
    naive = starts_at - timedelta(hours=24)
    assert due != naive
    assert due - naive == timedelta(hours=1)


def test_reminders_due_returns_every_offset_whose_threshold_has_passed():
    starts_at = datetime(2026, 6, 16, 12, 0, tzinfo=UTC)

    # More than a day out: neither the day-before nor the two-hour-before mark has arrived.
    far_out = starts_at - timedelta(hours=30)
    assert (
        reminders_due(
            starts_at=starts_at, now=far_out, zone="UTC", intervals=[24, 2], already_sent=[]
        )
        == []
    )

    # Inside the two-hour mark: both thresholds have already passed by now.
    close = starts_at - timedelta(hours=1)
    assert reminders_due(
        starts_at=starts_at, now=close, zone="UTC", intervals=[24, 2], already_sent=[]
    ) == [2, 24]

    # The day-before reminder already went out; only the two-hour one is still owed.
    assert reminders_due(
        starts_at=starts_at, now=close, zone="UTC", intervals=[24, 2], already_sent=[24]
    ) == [2]


# --- S1: send_due_reminders, the guard table's actual teeth --------------------------------

_INSERT_CUSTOMER = text(
    "INSERT INTO customers (first_name, last_name, email) VALUES ('Remi', 'Client', :e) "
    "RETURNING id"
)
_INSERT_SERVICE = text(
    "INSERT INTO services (name, duration_minutes) VALUES ('Direct', 60) RETURNING id"
)
_INSERT_APPOINTMENT = text(
    "INSERT INTO appointments (customer_id, staff_id, service_id, starts_at, ends_at, "
    "duration_minutes, buffer_before_minutes, buffer_after_minutes, price_cents, status, "
    "created_by_user_id) VALUES (:c, :s, :v, :start, :end, 60, 0, 0, 0, 'confirmed', :u) "
    "RETURNING id"
)


async def _seed_appointment_starting_soon(email: str, *, minutes_out: int) -> uuid.UUID:
    """An appointment `minutes_out` minutes from now, for a customer with a real email —
    straight into the tables (the same shape `tests/test_appointments.py::_insert_appointment`
    uses), since `starts_at` has to be relative to `datetime.now()` and the booking API's own
    helpers only offer a fixed future Monday."""
    async with session_scope() as db:
        staff_id, user_id = (await db.execute(text("SELECT id, user_id FROM staff LIMIT 1"))).one()
        customer_id = await db.scalar(_INSERT_CUSTOMER, {"e": email})
        service_id = await db.scalar(_INSERT_SERVICE)
        starts_at = datetime.now(UTC) + timedelta(minutes=minutes_out)
        appointment_id = await db.scalar(
            _INSERT_APPOINTMENT,
            {
                "c": customer_id,
                "s": staff_id,
                "v": service_id,
                "start": starts_at,
                "end": starts_at + timedelta(minutes=60),
                "u": user_id,
            },
        )
        # A fixed single interval, so a test controls exactly which reminder fires rather than
        # reasoning about the `[24, 2]` default's two offsets at once.
        await db.execute(text("UPDATE businesses SET reminder_intervals_hours = '[2]'::jsonb"))
        await db.commit()
    return appointment_id


async def test_the_scheduler_sends_a_due_reminder_once_and_never_a_second_time(sent_emails):
    await make_email_ready()
    await _seed_appointment_starting_soon("remi@x.example", minutes_out=90)

    async with session_scope() as db:
        sent = await send_due_reminders(db)
    assert sent == 1
    assert len(sent_emails) == 1
    assert sent_emails[0].to == "remi@x.example"

    async with session_scope() as db:
        sent_again = await send_due_reminders(db)
    assert sent_again == 0
    assert len(sent_emails) == 1  # unchanged — the guard table refused the second claim


async def test_the_scheduler_ignores_an_appointment_outside_every_interval(sent_emails):
    await make_email_ready()
    # Interval is `[2]` (set by the seed helper); an appointment 5 hours out is not due yet.
    await _seed_appointment_starting_soon("later@x.example", minutes_out=300)

    async with session_scope() as db:
        sent = await send_due_reminders(db)

    assert sent == 0
    assert sent_emails == []
