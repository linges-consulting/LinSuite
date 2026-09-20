"""S2: the availability engine — bookable slots, as a pure function over plain inputs.

No HTTP, no database. Every case is a small scenario against `America/Toronto` unless it says
otherwise, with one staff member working 09:00–12:00 every day of the week and a sixty-minute
service — nine quarter-hour starts, 09:00 through 11:00, when nothing is in the way. Each
test then puts one thing in the way and says what should be left.

The two DST days (tech-stack §19 step 1) are the reason the engine generates in local
wall-clock and converts afterwards: a shift spanning the missing hour loses an hour of slots,
one spanning the repeated hour gains one, and a 09:00 rule starts at 09:00 on both.
"""

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from scheduling.availability import (
    Requirement,
    ResourceSpec,
    ServiceSpec,
    StaffSpec,
    bookable_slots,
)

TORONTO = "America/Toronto"
REGINA = "America/Regina"

SPRING_FORWARD = date(2026, 3, 8)
FALL_BACK = date(2026, 11, 1)
ORDINARY = date(2026, 6, 15)  # a Monday

NINE_TO_NOON = {weekday: [(540, 720)] for weekday in range(7)}
# Long before any scenario, so "past" never gets in the way unless a test wants it to.
LONG_AGO = datetime(2000, 1, 1, tzinfo=UTC)


def local(day: date, hhmm: str, tz: str = TORONTO, fold: int = 0) -> datetime:
    """A local wall-clock moment on `day`, as the UTC instant it names."""
    hour, minute = map(int, hhmm.split(":"))
    return datetime(
        day.year, day.month, day.day, hour, minute, fold=fold, tzinfo=ZoneInfo(tz)
    ).astimezone(UTC)


def wall(moment: datetime, tz: str = TORONTO) -> str:
    return moment.astimezone(ZoneInfo(tz)).strftime("%H:%M")


def compute(
    day: date | list[date] = ORDINARY,
    *,
    tz: str = TORONTO,
    granularity: int = 15,
    service: ServiceSpec | None = None,
    staff: list[StaffSpec] | None = None,
    resources: list[ResourceSpec] = (),
    closures: set[date] = frozenset(),
    staff_busy: dict | None = None,
    resource_busy: dict | None = None,
    now: datetime = LONG_AGO,
    horizon_ends_on: date | None = None,
):
    days = [day] if isinstance(day, date) else day
    return bookable_slots(
        timezone=tz,
        granularity_minutes=granularity,
        service=service or ServiceSpec(duration_minutes=60, staff_ids=("ana",)),
        staff=staff if staff is not None else [StaffSpec(id="ana", hours=NINE_TO_NOON)],
        resources=list(resources),
        closures=set(closures),
        staff_busy=staff_busy or {},
        resource_busy=resource_busy or {},
        dates=days,
        now=now,
        horizon_ends_on=horizon_ends_on,
    )


def starts(day: date = ORDINARY, tz: str = TORONTO, **kwargs) -> list[str]:
    """The local wall-clock start of every slot on one day, in order."""
    return [wall(slot.starts_at, tz) for slot in compute(day, tz=tz, **kwargs)[day]]


NINE_SLOTS = ["09:00", "09:15", "09:30", "09:45", "10:00", "10:15", "10:30", "10:45", "11:00"]


# --- the ordinary day ---------------------------------------------------------------------------


def test_a_free_morning_is_nine_quarter_hour_starts():
    assert starts() == NINE_SLOTS


def test_a_slot_is_the_duration_long_and_in_utc():
    slot = compute()[ORDINARY][0]

    assert slot.starts_at == datetime(2026, 6, 15, 13, 0, tzinfo=UTC)  # 09:00 EDT
    assert slot.ends_at - slot.starts_at == timedelta(minutes=60)
    assert slot.starts_at.tzinfo is UTC
    assert slot.staff_ids == ("ana",)


def test_a_block_that_runs_to_midnight_offers_the_last_hour():
    hours = {weekday: [(1380, 1440)] for weekday in range(7)}  # 23:00–24:00

    assert starts(staff=[StaffSpec(id="ana", hours=hours)]) == ["23:00"]


def test_a_day_nobody_works_has_no_slots_but_is_still_answered():
    hours = {0: [(540, 720)]}  # Mondays only
    tuesday = date(2026, 6, 16)

    result = compute([ORDINARY, tuesday], staff=[StaffSpec(id="ana", hours=hours)])

    assert [wall(s.starts_at) for s in result[ORDINARY]] == NINE_SLOTS
    assert result[tuesday] == []


# --- the two DST days -------------------------------------------------------------------------


def test_a_nine_oclock_rule_starts_at_nine_on_both_transition_days():
    for day in (SPRING_FORWARD, FALL_BACK):
        assert starts(day) == NINE_SLOTS


def test_a_shift_across_the_missing_hour_loses_an_hour_of_slots():
    one_to_five = {weekday: [(60, 300)] for weekday in range(7)}
    staff = [StaffSpec(id="ana", hours=one_to_five)]

    ordinary = starts(ORDINARY, staff=staff)
    spring = starts(SPRING_FORWARD, staff=staff)

    assert len(ordinary) == 13
    assert len(spring) == 9  # four quarter-hours fewer
    assert not any(s.startswith("02:") for s in spring)  # 02:xx does not happen that day
    assert spring == [
        "01:00",
        "01:15",
        "01:30",
        "01:45",
        "03:00",
        "03:15",
        "03:30",
        "03:45",
        "04:00",
    ]


def test_a_shift_across_the_repeated_hour_gains_an_hour_on_the_local_grid():
    midnight_to_four = {weekday: [(0, 240)] for weekday in range(7)}
    staff = [StaffSpec(id="ana", hours=midnight_to_four)]

    ordinary = compute(ORDINARY, staff=staff)[ORDINARY]
    fall = compute(FALL_BACK, staff=staff)[FALL_BACK]

    assert len(ordinary) == 13
    assert len(fall) == 17  # 01:00–01:45 happens twice
    instants = [s.starts_at for s in fall]
    assert instants == sorted(instants) and len(set(instants)) == 17
    # Every start is still a quarter-hour on the clock face, both times round the repeated hour.
    assert all(s.starts_at.astimezone(ZoneInfo(TORONTO)).minute % 15 == 0 for s in fall)
    assert [wall(s.starts_at) for s in fall].count("01:30") == 2
    # The two 01:30s are an hour apart in real time.
    first, second = [s.starts_at for s in fall if wall(s.starts_at) == "01:30"]
    assert second - first == timedelta(hours=1)


@pytest.mark.parametrize("day", [SPRING_FORWARD, FALL_BACK, ORDINARY])
def test_saskatchewan_has_no_transition_to_survive(day):
    one_to_five = {weekday: [(60, 300)] for weekday in range(7)}
    slots = compute(day, tz=REGINA, staff=[StaffSpec(id="ana", hours=one_to_five)])[day]

    assert len(slots) == 13
    assert slots[0].starts_at.isoformat().endswith("07:00:00+00:00")  # 01:00 CST, all year


# --- split shifts and buffers ---------------------------------------------------------------------


def test_a_split_shift_is_two_blocks_with_nothing_in_the_gap():
    hours = {weekday: [(540, 720), (780, 1020)] for weekday in range(7)}  # 09–12, 13–17

    found = starts(staff=[StaffSpec(id="ana", hours=hours)])

    assert found[:9] == NINE_SLOTS
    assert found[9:] == [
        "13:00",
        "13:15",
        "13:30",
        "13:45",
        "14:00",
        "14:15",
        "14:30",
        "14:45",
        "15:00",
        "15:15",
        "15:30",
        "15:45",
        "16:00",
    ]
    # Nothing may start in the gap, and nothing may start late enough to run into it.
    assert not any("11:00" < s < "13:00" for s in found)


def test_blocks_that_touch_are_one_shift():
    """09:00–12:00 then 12:00–15:00 is somebody who does not take lunch (`scheduling/models.py`),
    so an appointment may straddle noon."""
    hours = {weekday: [(540, 720), (720, 900)] for weekday in range(7)}

    assert "11:30" in starts(staff=[StaffSpec(id="ana", hours=hours)])


def test_buffers_keep_the_whole_span_inside_the_block():
    service = ServiceSpec(
        duration_minutes=60, buffer_before_minutes=15, buffer_after_minutes=15, staff_ids=("ana",)
    )

    # Earliest: 09:15, so the turnaround before it starts at 09:00. Latest: 10:45, so the
    # turnaround after it ends at 12:00.
    assert starts(service=service) == [
        "09:15",
        "09:30",
        "09:45",
        "10:00",
        "10:15",
        "10:30",
        "10:45",
    ]


def test_buffers_keep_the_span_off_an_adjacent_booking():
    service = ServiceSpec(
        duration_minutes=60, buffer_before_minutes=15, buffer_after_minutes=15, staff_ids=("ana",)
    )
    busy = {"ana": [(local(ORDINARY, "10:00"), local(ORDINARY, "10:30"))]}

    # 09:15 would end at 10:15, turnaround to 10:30 — overlapping the booking. Only a start
    # whose turnaround *begins* at 10:30 is clear of it.
    assert starts(service=service, staff_busy=busy) == ["10:45"]


def test_a_booking_without_buffers_blocks_only_itself():
    busy = {"ana": [(local(ORDINARY, "10:00"), local(ORDINARY, "10:30"))]}

    assert starts(staff_busy=busy) == ["09:00", "10:30", "10:45", "11:00"]


# --- spaces and equipment -------------------------------------------------------------------------


def test_a_named_device_that_is_busy_removes_slots_though_the_staff_member_is_free():
    service = ServiceSpec(
        duration_minutes=60,
        staff_ids=("ana",),
        requirements=(Requirement(kind="equipment", resource_id="laser-2"),),
    )
    resources = [
        ResourceSpec(id="laser-1", kind="equipment"),
        ResourceSpec(id="laser-2", kind="equipment"),
    ]
    busy = {"laser-2": [(local(ORDINARY, "10:00"), local(ORDINARY, "11:00"))]}

    assert starts(service=service, resources=resources, resource_busy=busy) == ["09:00", "11:00"]
    # Laser 1 being busy instead changes nothing: the requirement names laser 2.
    other = {"laser-1": busy["laser-2"]}
    assert starts(service=service, resources=resources, resource_busy=other) == NINE_SLOTS


def test_any_space_passes_while_one_room_is_free_and_fails_when_both_are_busy():
    service = ServiceSpec(
        duration_minutes=60, staff_ids=("ana",), requirements=(Requirement(kind="space"),)
    )
    rooms = [ResourceSpec(id="room-1", kind="space"), ResourceSpec(id="room-2", kind="space")]
    all_morning = (local(ORDINARY, "09:00"), local(ORDINARY, "12:00"))
    ten_to_eleven = (local(ORDINARY, "10:00"), local(ORDINARY, "11:00"))

    one_room_free = {"room-1": [all_morning]}
    assert starts(service=service, resources=rooms, resource_busy=one_room_free) == NINE_SLOTS

    both_busy_mid_morning = {"room-1": [all_morning], "room-2": [ten_to_eleven]}
    assert starts(service=service, resources=rooms, resource_busy=both_busy_mid_morning) == [
        "09:00",
        "11:00",
    ]


def test_the_turnaround_occupies_the_room_too():
    service = ServiceSpec(
        duration_minutes=60,
        buffer_before_minutes=0,
        buffer_after_minutes=30,
        staff_ids=("ana",),
        requirements=(Requirement(kind="space"),),
    )
    rooms = [ResourceSpec(id="room-1", kind="space")]
    busy = {"room-1": [(local(ORDINARY, "10:15"), local(ORDINARY, "10:30"))]}

    # 09:00 ends at 10:00 and the room is needed until 10:30 — the booking at 10:15 is in the
    # way. 10:30 is the first start clear of it, and 10:30 + 90 = 12:00 still fits.
    assert starts(service=service, resources=rooms, resource_busy=busy) == ["10:30"]


def test_a_named_resource_the_engine_was_not_given_is_never_treated_as_free():
    """The catalog refuses a deactivated named resource before the engine is reached; this is
    the engine being safe on its own. A requirement nothing can satisfy offers nothing."""
    service = ServiceSpec(
        duration_minutes=60,
        staff_ids=("ana",),
        requirements=(Requirement(kind="equipment", resource_id="laser-9"),),
    )

    assert starts(service=service, resources=[ResourceSpec(id="laser-1", kind="equipment")]) == []
    assert starts(service=service, resources=[]) == []


def test_a_resource_s_bookings_across_the_window_are_applied_to_the_right_days():
    """Free intervals are derived per day from a busy list that spans the whole window."""
    tuesday, wednesday = date(2026, 6, 16), date(2026, 6, 17)
    service = ServiceSpec(
        duration_minutes=60, staff_ids=("ana",), requirements=(Requirement(kind="space"),)
    )
    rooms = [ResourceSpec(id="room-1", kind="space")]
    busy = {
        "room-1": [
            (local(ORDINARY, "10:00"), local(ORDINARY, "11:00")),
            (local(tuesday, "09:00"), local(tuesday, "10:00")),
            (local(tuesday, "11:30"), local(tuesday, "12:00")),
            (local(wednesday, "00:00"), local(wednesday + timedelta(days=1), "00:00")),
        ]
    }

    result = compute(
        [ORDINARY, tuesday, wednesday], service=service, resources=rooms, resource_busy=busy
    )

    assert [wall(s.starts_at) for s in result[ORDINARY]] == ["09:00", "11:00"]
    assert [wall(s.starts_at) for s in result[tuesday]] == ["10:00", "10:15", "10:30"]
    assert result[wednesday] == []


def test_a_requirement_with_no_resource_of_its_kind_offers_nothing():
    service = ServiceSpec(
        duration_minutes=60, staff_ids=("ana",), requirements=(Requirement(kind="space"),)
    )

    assert starts(service=service, resources=[ResourceSpec(id="laser", kind="equipment")]) == []


def test_a_room_and_a_device_are_both_needed():
    service = ServiceSpec(
        duration_minutes=60,
        staff_ids=("ana",),
        requirements=(
            Requirement(kind="space"),
            Requirement(kind="equipment", resource_id="laser"),
        ),
    )
    resources = [
        ResourceSpec(id="room-1", kind="space"),
        ResourceSpec(id="laser", kind="equipment"),
    ]
    busy = {
        "room-1": [(local(ORDINARY, "09:00"), local(ORDINARY, "10:00"))],
        "laser": [(local(ORDINARY, "11:00"), local(ORDINARY, "12:00"))],
    }

    assert starts(service=service, resources=resources, resource_busy=busy) == ["10:00"]


# --- time off, closures, the past, the horizon ---------------------------------------------------


def test_time_off_removes_the_slots_it_covers():
    away = [(local(ORDINARY, "10:00"), local(ORDINARY, "11:00"))]

    assert starts(staff=[StaffSpec(id="ana", hours=NINE_TO_NOON, time_off=away)]) == [
        "09:00",
        "11:00",
    ]


def test_an_all_day_absence_removes_the_day():
    away = [(local(ORDINARY, "00:00"), local(ORDINARY + timedelta(days=1), "00:00"))]

    assert starts(staff=[StaffSpec(id="ana", hours=NINE_TO_NOON, time_off=away)]) == []


def test_a_closure_removes_the_day_and_only_that_day():
    tuesday = date(2026, 6, 16)

    result = compute([ORDINARY, tuesday], closures={ORDINARY})

    assert result[ORDINARY] == []
    assert [wall(s.starts_at) for s in result[tuesday]] == NINE_SLOTS


def test_slots_that_have_already_started_are_dropped():
    now = local(ORDINARY, "10:10")

    assert starts(now=now) == ["10:15", "10:30", "10:45", "11:00"]
    # A slot starting exactly now is still bookable; one a minute before is not.
    assert starts(now=local(ORDINARY, "10:15"))[0] == "10:15"


def test_days_past_the_horizon_are_empty():
    tuesday, wednesday = date(2026, 6, 16), date(2026, 6, 17)

    result = compute([ORDINARY, tuesday, wednesday], horizon_ends_on=tuesday)

    assert len(result[ORDINARY]) == 9
    assert len(result[tuesday]) == 9
    assert result[wednesday] == []


# --- concurrency ---------------------------------------------------------------------------------


def test_one_at_a_time_is_the_default():
    busy = {"ana": [(local(ORDINARY, "10:00"), local(ORDINARY, "11:00"))]}

    assert starts(staff_busy=busy) == ["09:00", "11:00"]


def test_two_chairs_allow_a_second_overlapping_booking_and_refuse_a_third():
    two_chairs = [StaffSpec(id="ana", hours=NINE_TO_NOON, max_concurrent=2)]
    ten_to_eleven = (local(ORDINARY, "10:00"), local(ORDINARY, "11:00"))

    assert starts(staff=two_chairs, staff_busy={"ana": [ten_to_eleven]}) == NINE_SLOTS
    assert starts(staff=two_chairs, staff_busy={"ana": [ten_to_eleven] * 2}) == ["09:00", "11:00"]
    assert starts(staff=two_chairs, staff_busy={"ana": [ten_to_eleven] * 3}) == ["09:00", "11:00"]


def test_only_where_the_bookings_actually_overlap_counts_against_the_limit():
    two_chairs = [StaffSpec(id="ana", hours=NINE_TO_NOON, max_concurrent=2)]
    busy = {
        "ana": [
            (local(ORDINARY, "10:00"), local(ORDINARY, "10:30")),
            (local(ORDINARY, "10:15"), local(ORDINARY, "11:00")),
        ]
    }

    # Both chairs are taken only 10:15–10:30.
    assert starts(staff=two_chairs, staff_busy=busy) == [
        "09:00",
        "09:15",
        "10:30",
        "10:45",
        "11:00",
    ]


def test_bookings_that_merely_touch_do_not_overlap():
    two_chairs = [StaffSpec(id="ana", hours=NINE_TO_NOON, max_concurrent=2)]
    busy = {
        "ana": [
            (local(ORDINARY, "10:00"), local(ORDINARY, "10:30")),
            (local(ORDINARY, "10:30"), local(ORDINARY, "11:00")),
        ]
    }

    assert starts(staff=two_chairs, staff_busy=busy) == NINE_SLOTS


# --- any available provider ---------------------------------------------------------------------


def test_any_provider_is_the_union_and_each_slot_names_who_can_take_it():
    service = ServiceSpec(duration_minutes=60, staff_ids=("ana", "bo"))
    staff = [
        StaffSpec(id="ana", hours=NINE_TO_NOON),
        StaffSpec(id="bo", hours={weekday: [(600, 780)] for weekday in range(7)}),  # 10–13
    ]

    slots = compute(service=service, staff=staff)[ORDINARY]
    by_start = {wall(s.starts_at): s.staff_ids for s in slots}

    assert list(by_start) == NINE_SLOTS + ["11:15", "11:30", "11:45", "12:00"]
    assert by_start["09:00"] == ("ana",)
    assert by_start["10:00"] == ("ana", "bo")
    assert by_start["11:00"] == ("ana", "bo")
    assert by_start["12:00"] == ("bo",)


def test_staff_not_eligible_for_the_service_are_ignored():
    service = ServiceSpec(duration_minutes=60, staff_ids=("ana",))
    staff = [StaffSpec(id="ana", hours=NINE_TO_NOON), StaffSpec(id="bo", hours=NINE_TO_NOON)]

    assert all(s.staff_ids == ("ana",) for s in compute(service=service, staff=staff)[ORDINARY])


def test_one_provider_s_absence_leaves_the_other_s_slots():
    service = ServiceSpec(duration_minutes=60, staff_ids=("ana", "bo"))
    away = [(local(ORDINARY, "00:00"), local(ORDINARY + timedelta(days=1), "00:00"))]
    staff = [
        StaffSpec(id="ana", hours=NINE_TO_NOON, time_off=away),
        StaffSpec(id="bo", hours=NINE_TO_NOON),
    ]

    slots = compute(service=service, staff=staff)[ORDINARY]

    assert len(slots) == 9 and all(s.staff_ids == ("bo",) for s in slots)


# --- granularity ---------------------------------------------------------------------------------


def test_thirty_minute_granularity_offers_the_hour_and_the_half():
    assert starts(granularity=30) == ["09:00", "09:30", "10:00", "10:30", "11:00"]


def test_the_grid_is_measured_from_local_midnight_not_from_the_shift():
    # A shift starting at 09:10 on a fifteen-minute grid: the first start is 09:15, not 09:10.
    hours = {weekday: [(550, 720)] for weekday in range(7)}

    assert starts(staff=[StaffSpec(id="ana", hours=hours)])[0] == "09:15"


def test_the_grid_stays_on_local_midnight_across_the_repeated_hour():
    """Forty-five minutes does not divide the hour, so a grid anchored anywhere but local
    midnight would drift on the fall-back day. It must not."""
    all_day = {weekday: [(0, 1440)] for weekday in range(7)}
    service = ServiceSpec(duration_minutes=45, staff_ids=("ana",))

    found = starts(
        FALL_BACK, granularity=45, service=service, staff=[StaffSpec(id="ana", hours=all_day)]
    )

    assert found[:6] == ["00:00", "00:45", "01:30", "01:30", "02:15", "03:00"]
