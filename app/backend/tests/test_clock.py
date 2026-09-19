"""S2: the one place a wall-clock rule becomes an instant.

Working hours are stored as minutes since local midnight — no zone, no date — because
"Mondays 09:00–12:00" is a rule about a clock face, not about a moment (CLAUDE.md "Time",
tech-stack §19). `local_blocks_to_instants` is what turns that rule into the UTC instants the
scheduling engine intersects, for one particular local date.

The two days that make this worth a function of its own are the DST transitions. Generating
locally and localizing afterwards makes both correct without a special case: a block spanning
the missing hour is genuinely an hour shorter in real time, and one spanning the repeated
hour is genuinely an hour longer. A rule that happens to start at 09:00 is untouched by
either — which is the whole point of storing it as 09:00 rather than as 13:00 UTC.
"""

from datetime import date

import pytest

from scheduling.clock import local_blocks_to_instants

TORONTO = "America/Toronto"
# Saskatchewan keeps CST all year. The same rules, with nothing to go wrong.
REGINA = "America/Regina"

# 2026: clocks go forward on 8 March (02:00 → 03:00) and back on 1 November (02:00 → 01:00).
SPRING_FORWARD = date(2026, 3, 8)
FALL_BACK = date(2026, 11, 1)
ORDINARY = date(2026, 6, 15)


def minutes(pair) -> float:
    start, end = pair
    return (end - start).total_seconds() / 60


def one(blocks, day, tz=TORONTO):
    return local_blocks_to_instants(blocks, day, tz)[0]


# --- the ordinary day -----------------------------------------------------------------------


def test_a_split_shift_becomes_two_instant_pairs():
    morning, afternoon = local_blocks_to_instants([(540, 720), (900, 1080)], ORDINARY, TORONTO)

    assert minutes(morning) == 180
    assert minutes(afternoon) == 180
    # 09:00 EDT is 13:00 UTC on an ordinary June day.
    assert morning[0].isoformat() == "2026-06-15T13:00:00+00:00"


def test_midnight_to_midnight_is_a_whole_day():
    assert minutes(one([(0, 1440)], ORDINARY)) == 1440


# --- spring forward: the hour that does not exist ---------------------------------------------


def test_a_block_spanning_the_missing_hour_is_sixty_minutes_shorter():
    # 01:00–04:00 local. Wall-clock says three hours; only two of them happen.
    assert minutes(one([(60, 240)], SPRING_FORWARD)) == 120
    assert minutes(one([(60, 240)], ORDINARY)) == 180


def test_a_nine_oclock_rule_still_starts_at_nine_across_spring_forward():
    for day in (date(2026, 3, 7), SPRING_FORWARD, date(2026, 3, 9)):
        start, _ = one([(540, 720)], day)
        assert start.astimezone(_zone(TORONTO)).strftime("%H:%M") == "09:00"


# --- fall back: the hour that happens twice ----------------------------------------------------


def test_a_block_spanning_the_repeated_hour_is_sixty_minutes_longer():
    # 00:00–04:00 local. Wall-clock says four hours; five of them happen.
    assert minutes(one([(0, 240)], FALL_BACK)) == 300
    assert minutes(one([(0, 240)], ORDINARY)) == 240


def test_a_nine_oclock_rule_still_starts_at_nine_across_fall_back():
    for day in (date(2026, 10, 31), FALL_BACK, date(2026, 11, 2)):
        start, _ = one([(540, 720)], day)
        assert start.astimezone(_zone(TORONTO)).strftime("%H:%M") == "09:00"


# --- a zone that never moves --------------------------------------------------------------------


@pytest.mark.parametrize("day", [SPRING_FORWARD, FALL_BACK, ORDINARY])
def test_saskatchewan_has_no_transitions_to_survive(day):
    assert minutes(one([(60, 240)], day, REGINA)) == 180
    start, _ = one([(540, 720)], day, REGINA)
    assert start.isoformat().endswith("15:00:00+00:00")  # 09:00 CST, all year


# --- the zone is an argument, not a property of the rule ------------------------------------------


def test_the_same_rule_reads_differently_in_a_different_zone():
    """What makes changing `Business.timezone` safe: the stored block never moves, and the
    instants it produces move with the zone."""
    toronto = one([(540, 720)], ORDINARY, TORONTO)
    vancouver = one([(540, 720)], ORDINARY, "America/Vancouver")

    assert toronto[0].isoformat() == "2026-06-15T13:00:00+00:00"
    assert vancouver[0].isoformat() == "2026-06-15T16:00:00+00:00"


def _zone(name: str):
    from zoneinfo import ZoneInfo

    return ZoneInfo(name)
