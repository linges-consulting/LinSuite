"""S2: `scheduling/queue_eligibility.py::can_start` — the walk-in queue's start-eligibility
gate (Phase 7 Task 4, #12). No HTTP, no database, same style as `test_availability.py`: one
staff member working 09:00–12:00 `America/Toronto` unless a case says otherwise.
"""

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from scheduling.availability import OUTSIDE_SHIFT, ServiceSpec, StaffSpec
from scheduling.queue_eligibility import STAFF_BUSY, can_start

TORONTO = "America/Toronto"
MONDAY = date(2026, 6, 15)


def local(day: date, hhmm: str, tz: str = TORONTO) -> datetime:
    hour, minute = map(int, hhmm.split(":"))
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=ZoneInfo(tz)).astimezone(UTC)


NINE_TO_NOON = {weekday: [(540, 720)] for weekday in range(7)}
TWENTY_MINUTE_SERVICE = ServiceSpec(duration_minutes=20)


def test_free_now_within_shift_is_eligible():
    staff = StaffSpec(id="ana", hours=NINE_TO_NOON)
    result = can_start(
        staff=staff,
        service=TWENTY_MINUTE_SERVICE,
        timezone=TORONTO,
        now=local(MONDAY, "10:00"),
    )
    assert result.eligible
    assert result.reason is None


def test_a_booked_appointment_the_service_would_run_into_is_not_eligible():
    """The literal acceptance criterion: a booking starting in 10 minutes, a 20-minute walk-in
    service — the two overlap, so the walk-in cannot start."""
    staff = StaffSpec(id="ana", hours=NINE_TO_NOON)
    now = local(MONDAY, "10:00")
    booked = (now + timedelta(minutes=10), now + timedelta(minutes=40))
    result = can_start(
        staff=staff,
        service=TWENTY_MINUTE_SERVICE,
        timezone=TORONTO,
        now=now,
        staff_busy=[booked],
    )
    assert not result.eligible
    assert result.reason == STAFF_BUSY


def test_an_appointment_already_in_progress_right_now_is_not_eligible():
    staff = StaffSpec(id="ana", hours=NINE_TO_NOON)
    now = local(MONDAY, "10:00")
    in_progress = (now - timedelta(minutes=5), now + timedelta(minutes=15))
    result = can_start(
        staff=staff,
        service=TWENTY_MINUTE_SERVICE,
        timezone=TORONTO,
        now=now,
        staff_busy=[in_progress],
    )
    assert not result.eligible
    assert result.reason == STAFF_BUSY


def test_a_booking_that_only_touches_the_service_s_end_does_not_block_it():
    """Half-open, same convention as the rest of the engine: a booking starting exactly when
    the walk-in service would end does not overlap it."""
    staff = StaffSpec(id="ana", hours=NINE_TO_NOON)
    now = local(MONDAY, "10:00")
    touching = (now + timedelta(minutes=20), now + timedelta(minutes=50))
    result = can_start(
        staff=staff,
        service=TWENTY_MINUTE_SERVICE,
        timezone=TORONTO,
        now=now,
        staff_busy=[touching],
    )
    assert result.eligible


def test_buffer_after_extends_the_checked_span():
    """A service's own turnaround is part of what has to be clear, not just its duration."""
    staff = StaffSpec(id="ana", hours=NINE_TO_NOON)
    now = local(MONDAY, "10:00")
    service = ServiceSpec(duration_minutes=20, buffer_after_minutes=10)
    booked = (now + timedelta(minutes=25), now + timedelta(minutes=45))
    result = can_start(staff=staff, service=service, timezone=TORONTO, now=now, staff_busy=[booked])
    assert not result.eligible
    assert result.reason == STAFF_BUSY


def test_concurrency_of_two_permits_a_second_booking_the_first_alone_did_not_saturate():
    """Staff concurrency is read, not assumed to be 1 (tech-stack §20, colour-processing)."""
    staff = StaffSpec(id="ana", hours=NINE_TO_NOON, max_concurrent=2)
    now = local(MONDAY, "10:00")
    one_other = (now, now + timedelta(minutes=20))
    result = can_start(
        staff=staff,
        service=TWENTY_MINUTE_SERVICE,
        timezone=TORONTO,
        now=now,
        staff_busy=[one_other],
    )
    assert result.eligible


def test_outside_shift_with_no_override_is_not_eligible():
    staff = StaffSpec(id="ana", hours=NINE_TO_NOON)
    result = can_start(
        staff=staff,
        service=TWENTY_MINUTE_SERVICE,
        timezone=TORONTO,
        now=local(MONDAY, "20:00"),
    )
    assert not result.eligible
    assert result.reason == OUTSIDE_SHIFT


def test_outside_shift_with_an_override_is_eligible():
    staff = StaffSpec(id="ana", hours=NINE_TO_NOON)
    result = can_start(
        staff=staff,
        service=TWENTY_MINUTE_SERVICE,
        timezone=TORONTO,
        now=local(MONDAY, "20:00"),
        override=True,
    )
    assert result.eligible
    assert result.reason is None


def test_override_never_waives_the_physical_concurrency_check():
    """Advisory versus physical (module docstring): an override lifts the shift rule, never a
    saturated staff member."""
    staff = StaffSpec(id="ana", hours=NINE_TO_NOON)
    now = local(MONDAY, "10:00")
    booked = (now, now + timedelta(minutes=20))
    result = can_start(
        staff=staff,
        service=TWENTY_MINUTE_SERVICE,
        timezone=TORONTO,
        now=now,
        staff_busy=[booked],
        override=True,
    )
    assert not result.eligible
    assert result.reason == STAFF_BUSY
