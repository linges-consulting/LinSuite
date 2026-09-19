"""Wall-clock rules → instants. The one conversion, in one place.

A `working_hours` row is `(weekday, start_minute, end_minute)` — minutes since *local*
midnight, with no zone and no date on it. That is deliberate (CLAUDE.md "Time", PRD §1,
tech-stack §19): "works Mondays 09:00–12:00" is a rule about a clock face. Stored as a UTC
instant it would silently slide by an hour twice a year, and the first anyone would know is a
client arriving when nobody is there.

So the rule stays local and this function localizes it, for one particular date, in the
business's zone — **generate locally, then convert, never the reverse**. That order is what
makes the two awkward days fall out for free rather than needing a special case:

* **Spring forward.** 02:00–03:00 does not exist. A block spanning it is genuinely an hour
  shorter in real time, which is what the arithmetic below produces. A local time inside the
  gap resolves through the pre-transition offset, landing an hour later on the clock — the
  interpretation that keeps the block's start ordered before its end.
* **Fall back.** 01:00–02:00 happens twice. A block spanning it is genuinely an hour longer.
  An ambiguous local time resolves to its first occurrence (`fold=0`, Python's default),
  which is the one a person writing "01:30" means.

Task 14's availability computation is the caller. Nothing here reads the database or knows
what a staff member is: it is a pure function over integers, which is why its DST behaviour
is pinned by `tests/test_clock.py` at seam S2 rather than through an HTTP round trip.
"""

from collections.abc import Iterable
from datetime import UTC, datetime, time, timedelta
from datetime import date as Date
from zoneinfo import ZoneInfo

# Minutes in a day. `end_minute` may equal it — a block that runs to midnight — so the pair
# below is built from a plain `timedelta` rather than by constructing a `time`, which has no
# 24:00.
MINUTES_IN_DAY = 1440


def local_blocks_to_instants(
    blocks: Iterable[tuple[int, int]], day: Date, tz: str
) -> list[tuple[datetime, datetime]]:
    """Turn one local date's `(start_minute, end_minute)` blocks into UTC instants.

    `day` is a date in `tz`, not a UTC one — the rule is being read against a local calendar.
    The result is in the order given; a caller that wants them sorted sorts them.
    """
    zone = ZoneInfo(tz)
    midnight = datetime.combine(day, time.min)

    def instant(minute: int) -> datetime:
        return (midnight + timedelta(minutes=minute)).replace(tzinfo=zone).astimezone(UTC)

    return [(instant(start), instant(end)) for start, end in blocks]
