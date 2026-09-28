"""Wait estimate (Phase 7 Task 6, #12) — S2, pure, no session, no clock of its own.

Answers one question: roughly how long until a waiting queue entry is served — every other
waiting entry's remaining service time ahead of it in the line, divided across however many
staff could pick one up right now. Same "pure function, real caller resolves the inputs off the
database" split `queue_eligibility.can_start` (Task 4) already uses.

**Labelled an estimate everywhere it is surfaced** (#12's own acceptance criterion — wording
matters here, not just the number). This module's own name says so; the field this feeds
(`scheduling/queue.py::QueueEntryOut.estimated_wait_minutes`) is named the same way, never
`wait_minutes` or anything read as a promise.

**Wiring decision, since Task 8's queue display screen does not exist yet** (m3.md leaves this
open for this task to decide): wired into `GET /queue-entries`'s existing per-entry response
now, rather than left fully standalone. The endpoint already returns waiting entries ordered by
`arrived_at` — exactly the ordering "how many are ahead of me" needs — so leaving this
disconnected until Task 8 would mean either a second endpoint duplicating that ordering or Task
8 recomputing it from scratch; wiring it into the list response instead means Task 8 reads
`estimated_wait_minutes` straight off the entry it already fetches. `add`/`abandon`/`start`'s own
single-entry responses (`scheduling/queue.py`) do not carry it — each of those returns exactly
one entry with no view of who else is ahead of it, and computing that context for one row would
mean re-running the same per-request query the list endpoint already does for free.

**"Remaining service duration" is a waiting entry's full requested-service `duration_minutes`,
buffers excluded** — nothing has started, so the whole booked duration is what is left; the
buffers either side are staff turnaround, not time a waiting client is stood in the lobby for,
so they are left out of the estimate a client-facing number should describe.
"""

from collections.abc import Sequence
from datetime import timedelta


def estimate_wait(entries_ahead: Sequence[timedelta], available_staff_count: int) -> timedelta:
    """The sum of everyone ahead's remaining service time, split across however many staff
    could pick the next one up right now.

    `available_staff_count <= 0` — nobody eligible for this entry's service is free right now,
    or nobody is even eligible for it at all — is floored to 1 rather than raising a
    `ZeroDivisionError` or returning something misleadingly instant. Documented choice, not a
    real guarantee that one person is about to become free: with genuinely zero capacity the
    honest estimate is "at least as long as it would take one person to clear everyone ahead",
    which is exactly what flooring to 1 computes, rather than an undefined/infinite answer a
    caller would have to special-case anyway.
    """
    total = sum(entries_ahead, timedelta())
    return total / max(available_staff_count, 1)
