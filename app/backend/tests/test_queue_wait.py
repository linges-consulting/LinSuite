"""S2: `scheduling.queue_wait.estimate_wait` (Phase 7 Task 6, #12) — pure, no database."""

from datetime import timedelta

from scheduling.queue_wait import estimate_wait


def test_nobody_ahead_is_zero_wait():
    assert estimate_wait([], available_staff_count=2) == timedelta(0)


def test_split_across_available_staff():
    # Two 30-minute services ahead, two staff free right now: each staff member could pick up
    # one immediately, so the wait is one service's worth, not two.
    ahead = [timedelta(minutes=30), timedelta(minutes=30)]
    assert estimate_wait(ahead, available_staff_count=2) == timedelta(minutes=30)


def test_several_entries_split_across_a_couple_of_staff():
    ahead = [timedelta(minutes=20), timedelta(minutes=30), timedelta(minutes=40)]
    # 90 minutes of remaining service, split across 3 staff = 30 minutes each.
    assert estimate_wait(ahead, available_staff_count=3) == timedelta(minutes=30)


def test_one_available_staff_member_serves_the_whole_line_in_sequence():
    ahead = [timedelta(minutes=15), timedelta(minutes=15)]
    assert estimate_wait(ahead, available_staff_count=1) == timedelta(minutes=30)


def test_zero_available_staff_does_not_raise_and_floors_to_one():
    """Documented choice (`queue_wait.py`'s own docstring): zero capacity floors to a
    denominator of 1, the same arithmetic as `available_staff_count=1` — never a
    `ZeroDivisionError`, never a misleadingly instant "0"."""
    ahead = [timedelta(minutes=45)]
    assert estimate_wait(ahead, available_staff_count=0) == timedelta(minutes=45)
    assert estimate_wait(ahead, available_staff_count=0) == estimate_wait(
        ahead, available_staff_count=1
    )


def test_negative_available_staff_count_is_treated_the_same_as_zero():
    ahead = [timedelta(minutes=10)]
    assert estimate_wait(ahead, available_staff_count=-3) == timedelta(minutes=10)


def test_zero_available_staff_with_nobody_ahead_is_still_zero():
    assert estimate_wait([], available_staff_count=0) == timedelta(0)
