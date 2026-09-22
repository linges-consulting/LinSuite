"""S2: `customers.retention.expiry` — the rule ADR-0001 exists for, as a table.

`max(last_entry + 10y, dob + 18y + 10y)`, at the *end* of that local day in the business's
zone (so a purge can never run a few hours early), with the pre-flight rulings (D4):
`general_business` holds nothing, no clinical entry holds nothing, an entry with no DOB is
held with unknown expiry (`datetime.max`, stored as `'infinity'`).

**Where a reading is ambiguous, retention takes the later date** (fix round 1 ruling — an
early purge is irreversible, a late one is not): a 29 Feb anniversary in a common year is
1 Mar, the DOB arm is the later of `dob + 28y` and `(dob + 18y) + 10y`, and "end of day" is
the instant before the next local midnight even where 23:xx happens twice.
"""

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

from customers.retention import GENERAL_BUSINESS, INFINITY, REGULATED_HEALTH, expiry, later

TORONTO = "America/Toronto"
REGINA = "America/Regina"


def end_of(day: date, zone: str) -> datetime:
    """23:59:59.999999 on `day`, local to `zone`, as the UTC instant the column stores."""
    return datetime(day.year, day.month, day.day, 23, 59, 59, 999999, tzinfo=ZoneInfo(zone))


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


CASES = [
    # (name, profile, dob, last entry, zone, expected)
    (
        "adult: 10 years after the last entry",
        REGULATED_HEALTH,
        date(1980, 5, 1),
        utc(2026, 6, 15, 16),
        TORONTO,
        end_of(date(2036, 6, 15), TORONTO),
    ),
    (
        "minor: the 28th birthday wins",
        REGULATED_HEALTH,
        date(2018, 3, 14),
        utc(2026, 6, 15, 16),
        TORONTO,
        end_of(date(2046, 3, 14), TORONTO),
    ),
    (
        "a minor's entry so late that 10 years after it beats the 28th birthday",
        REGULATED_HEALTH,
        date(2008, 3, 14),
        utc(2026, 6, 15, 16),
        TORONTO,
        end_of(date(2036, 6, 15), TORONTO),
    ),
    (
        "no clinical entry: nothing to hold",
        REGULATED_HEALTH,
        date(2018, 3, 14),
        None,
        TORONTO,
        None,
    ),
    (
        "no entry and no DOB: still nothing to hold",
        REGULATED_HEALTH,
        None,
        None,
        TORONTO,
        None,
    ),
    (
        "general_business: never held, even a minor with an entry",
        GENERAL_BUSINESS,
        date(2018, 3, 14),
        utc(2026, 6, 15, 16),
        TORONTO,
        None,
    ),
    (
        "general_business: never held, even with no DOB",
        GENERAL_BUSINESS,
        None,
        utc(2026, 6, 15, 16),
        TORONTO,
        None,
    ),
    (
        "entry with no DOB: held, expiry unknown — never 'assume adult'",
        REGULATED_HEALTH,
        None,
        utc(2026, 6, 15, 16),
        TORONTO,
        INFINITY,
    ),
    (
        # dob + 28y = 29 Feb 2028; (dob + 18y) + 10y = 1 Mar 2018 + 10y = 1 Mar 2028: later wins.
        "leap-day DOB 2000: the (dob + 18y) + 10y reading, 1 Mar, is later",
        REGULATED_HEALTH,
        date(2000, 2, 29),
        utc(2010, 6, 15, 16),
        TORONTO,
        end_of(date(2028, 3, 1), TORONTO),
    ),
    (
        "leap-day DOB 2008: 1 Mar 2036, not 29 Feb 2036",
        REGULATED_HEALTH,
        date(2008, 2, 29),
        utc(2010, 6, 15, 16),
        TORONTO,
        end_of(date(2036, 3, 1), TORONTO),
    ),
    (
        "leap-day DOB 2072, 28th birthday in the common century year 2100: 1 Mar",
        REGULATED_HEALTH,
        date(2072, 2, 29),
        utc(2080, 6, 15, 16),
        TORONTO,
        end_of(date(2100, 3, 1), TORONTO),
    ),
    (
        "leap-day entry, 10 years on is a common year: 1 Mar",
        REGULATED_HEALTH,
        date(1980, 5, 1),
        utc(2024, 2, 29, 17),
        TORONTO,
        end_of(date(2034, 3, 1), TORONTO),
    ),
    (
        # 04:30 UTC on 1 Jun is still 31 May in Toronto: the entry's day is the local one.
        "the entry's day is read in the business zone, not in UTC",
        REGULATED_HEALTH,
        date(1980, 5, 1),
        utc(2026, 6, 1, 3, 30),
        TORONTO,
        end_of(date(2036, 5, 31), TORONTO),
    ),
    (
        # 4 Nov 2024 is EST (DST ended 3 Nov 2024); 4 Nov 2034 is still EDT (it ends 5 Nov
        # 2034). End of day is 23:59 on the *expiry* day's offset, not the entry's.
        "DST-adjacent: the offset is the expiry day's, not the entry's",
        REGULATED_HEALTH,
        date(1980, 5, 1),
        utc(2024, 11, 4, 15),
        TORONTO,
        utc(2034, 11, 5, 3, 59, 59, 999999),
    ),
    (
        "no DST in Regina: the same entry ends at 23:59 CST all year",
        REGULATED_HEALTH,
        date(1980, 5, 1),
        utc(2024, 11, 4, 15),
        REGINA,
        utc(2034, 11, 5, 5, 59, 59, 999999),
    ),
    # Clocks fall back at midnight, so 23:00–23:59 happens twice on the expiry day: the end of
    # the day is the *second* 23:59:59.999999 — the instant before the next local midnight.
    # Expected values are hard-coded UTC, one hour after a fold=0 reading of 23:59.
    *[
        (
            f"midnight fall-back in {zone}: the day ends before the next local midnight",
            REGULATED_HEALTH,
            date(1970, 1, 1),
            datetime(day.year - 10, day.month, day.day, 12, tzinfo=ZoneInfo(zone)),
            zone,
            expected,
        )
        for zone, day, expected in [
            ("America/Santiago", date(2026, 4, 4), utc(2026, 4, 5, 3, 59, 59, 999999)),
            ("Asia/Beirut", date(2026, 10, 24), utc(2026, 10, 24, 21, 59, 59, 999999)),
            ("Africa/Cairo", date(2026, 10, 29), utc(2026, 10, 29, 21, 59, 59, 999999)),
            ("America/Asuncion", date(2024, 3, 23), utc(2024, 3, 24, 3, 59, 59, 999999)),
        ]
    ],
]


@pytest.mark.parametrize(
    "profile,dob,entry,zone,expected", [c[1:] for c in CASES], ids=[c[0] for c in CASES]
)
def test_expiry(profile, dob, entry, zone, expected):
    assert expiry(profile, dob, entry, zone) == expected


def test_infinity_is_what_asyncpg_reads_timestamptz_infinity_as():
    assert INFINITY == datetime.max


def test_a_dob_correction_moves_the_date_both_ways():
    """The recompute is the same pure call with the corrected input — no memory of the
    old value, so a correction toward an older DOB can shorten the hold as well as lengthen it."""
    entry = utc(2026, 6, 15, 16)
    as_minor = expiry(REGULATED_HEALTH, date(2018, 3, 14), entry, TORONTO)
    corrected = expiry(REGULATED_HEALTH, date(1986, 3, 14), entry, TORONTO)
    assert as_minor == end_of(date(2046, 3, 14), TORONTO)
    assert corrected == end_of(date(2036, 6, 15), TORONTO)


def test_an_unknown_profile_is_treated_as_regulated():
    """The CHECK refuses a third value, but the function's own default is the retain side:
    only an explicit `general_business` releases a hold."""
    assert expiry("something_else", None, utc(2026, 6, 15, 16), TORONTO) == INFINITY


def test_a_naive_entry_is_refused():
    with pytest.raises(ValueError):
        expiry(REGULATED_HEALTH, date(1980, 5, 1), datetime(2026, 6, 15, 12), TORONTO)


LATE = utc(2036, 6, 17, 3, 59, 59, 999999)
EARLY = utc(2036, 6, 16, 6, 59, 59, 999999)


@pytest.mark.parametrize(
    "old,new,expected",
    [
        (None, LATE, LATE),  # no hold before: take the recomputed one
        (None, None, None),
        (LATE, EARLY, LATE),  # never shorter
        (EARLY, LATE, LATE),  # longer is fine
        (INFINITY, LATE, INFINITY),
        (LATE, INFINITY, INFINITY),
        (LATE, None, LATE),
    ],
)
def test_later_is_the_later_hold(old, new, expected):
    assert later(old, new) == expected
