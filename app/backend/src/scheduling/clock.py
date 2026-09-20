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


def localize(moment: datetime, tz: str | ZoneInfo) -> datetime:
    """A naive local wall-clock datetime → the UTC instant it names in `tz`.

    The primitive both public conversions in this application are built from: the recurring
    matrix below, and `scheduling/time_off.py` turning a `datetime-local` field into an
    instant. One implementation, so the gap and the repeated hour are resolved the same way
    on both paths — two `.replace(tzinfo=...)` calls in two modules is how they would come to
    differ without anybody deciding that they should.

    `moment` must be naive. An aware one is a caller that already has an instant and does not
    need this; the API refuses those at its boundary rather than reinterpreting them.
    """
    zone = tz if isinstance(tz, ZoneInfo) else ZoneInfo(tz)
    return moment.replace(tzinfo=zone).astimezone(UTC)


def local_blocks_to_instants(
    blocks: Iterable[tuple[int, int]], day: Date, tz: str | ZoneInfo
) -> list[tuple[datetime, datetime]]:
    """Turn one local date's `(start_minute, end_minute)` blocks into UTC instants.

    `day` is a date in `tz`, not a UTC one — the rule is being read against a local calendar.
    The result is in the order given; a caller that wants them sorted sorts them.

    Minutes are added to local midnight as a `timedelta` rather than built into a `time`,
    because `end_minute` may be 1440 and there is no 24:00.
    """
    midnight = datetime.combine(day, time.min)
    return [
        (
            localize(midnight + timedelta(minutes=start), tz),
            localize(midnight + timedelta(minutes=end), tz),
        )
        for start, end in blocks
    ]
