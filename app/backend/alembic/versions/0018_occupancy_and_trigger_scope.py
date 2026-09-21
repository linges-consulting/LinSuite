"""One definition of "occupying", and a trigger that only asks when the answer can change.

Revision ID: 0018
Revises: 0017
Create Date: 2026-09-21

Two defects in 0017's function, both about *when* the staff-concurrency rule is asked rather
than about the handover arithmetic it asks with (that part is unchanged, and still as narrow:
same non-null `booking_group_id`, exact adjacency, only the two facing buffers).

**Occupying is `status NOT IN ('cancelled', 'no_show')`, everywhere.** 0017 returned early
only for `cancelled` and counted everything `<> 'cancelled'`, while
`scheduling.availability.NOT_OCCUPYING` — the Python side of the very same rule, used by
`waive_handover`, `slots.busy_intervals` and `appointments.recompute_group_periods` — has
always said a no-show occupies nothing. The disagreement had two costs: marking a no-show on
a link of a buffered handover chain raised 23P01 from the status UPDATE itself (the row was
compared against its sibling with the facing buffers back in force, because a no-show was not
a waiver partner), and a no-show went on blocking its staff member's afternoon for a visit
that never happened. With the status check hoisted to the top, the two `<> 'no_show'` clauses
inside the waiver are dead — a no-show never reaches the comparison as `NEW`, and never
matches it as `a` — so they go.

**A row that already holds its span cannot newly violate the limit by keeping it.** 0017's
trigger fired on every UPDATE with no column filter, so `max_concurrent_appointments` being
lowered after two overlapping appointments were legitimately booked turned every later
transition on them — completing one, editing its notes — into an unhandled 500. The limit is
a rule about *taking* time, so the check now runs on INSERT, and on an UPDATE only when the
row's staff member, span or buffers move, or when a non-occupying row becomes occupying.
It still takes `FOR UPDATE` on the staff row whenever it does run, which is what serialises
two simultaneous bookings for one person.

`downgrade` restores 0017's body verbatim; `tests/test_migrations.py` runs the round trip.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STAFF_CONCURRENCY = """
CREATE OR REPLACE FUNCTION appointments_enforce_staff_concurrency() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    allowed integer;
    taken integer;
BEGIN
    IF NEW.status IN ('cancelled', 'no_show') THEN
        RETURN NEW;
    END IF;
    -- Keeping a span one already held is not taking it: only a move, a resize, a change of
    -- buffers or of staff member, or a row that has just become occupying, is re-checked.
    IF TG_OP = 'UPDATE'
       AND OLD.status NOT IN ('cancelled', 'no_show')
       AND NEW.staff_id = OLD.staff_id
       AND NEW.starts_at = OLD.starts_at
       AND NEW.ends_at = OLD.ends_at
       AND NEW.buffer_before_minutes = OLD.buffer_before_minutes
       AND NEW.buffer_after_minutes = OLD.buffer_after_minutes
    THEN
        RETURN NEW;
    END IF;
    SELECT max_concurrent_appointments INTO allowed
      FROM staff WHERE id = NEW.staff_id FOR UPDATE;
    SELECT count(*) INTO taken
      FROM appointments a
     WHERE a.staff_id = NEW.staff_id
       AND a.id <> NEW.id
       AND a.status NOT IN ('cancelled', 'no_show')
       AND tstzrange(
               a.starts_at - make_interval(mins => CASE
                   WHEN a.booking_group_id IS NOT NULL
                        AND a.booking_group_id = NEW.booking_group_id
                        AND NEW.ends_at = a.starts_at
                   THEN 0 ELSE a.buffer_before_minutes END),
               a.ends_at + make_interval(mins => CASE
                   WHEN a.booking_group_id IS NOT NULL
                        AND a.booking_group_id = NEW.booking_group_id
                        AND a.ends_at = NEW.starts_at
                   THEN 0 ELSE a.buffer_after_minutes END),
               '[)'
           )
       &&
           tstzrange(
               NEW.starts_at - make_interval(mins => CASE
                   WHEN a.booking_group_id IS NOT NULL
                        AND a.booking_group_id = NEW.booking_group_id
                        AND a.ends_at = NEW.starts_at
                   THEN 0 ELSE NEW.buffer_before_minutes END),
               NEW.ends_at + make_interval(mins => CASE
                   WHEN a.booking_group_id IS NOT NULL
                        AND a.booking_group_id = NEW.booking_group_id
                        AND NEW.ends_at = a.starts_at
                   THEN 0 ELSE NEW.buffer_after_minutes END),
               '[)'
           );
    IF taken >= allowed THEN
        RAISE EXCEPTION 'staff member % already has % overlapping appointment(s), limit %',
            NEW.staff_id, taken, allowed
            USING ERRCODE = 'exclusion_violation',
                  CONSTRAINT = 'tg_appointments_staff_concurrency',
                  TABLE = 'appointments';
    END IF;
    RETURN NEW;
END
$$
"""

# The exact 0017 body, restored on downgrade.
STAFF_CONCURRENCY_PREVIOUS = """
CREATE OR REPLACE FUNCTION appointments_enforce_staff_concurrency() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    allowed integer;
    taken integer;
BEGIN
    IF NEW.status = 'cancelled' THEN
        RETURN NEW;
    END IF;
    SELECT max_concurrent_appointments INTO allowed
      FROM staff WHERE id = NEW.staff_id FOR UPDATE;
    SELECT count(*) INTO taken
      FROM appointments a
     WHERE a.staff_id = NEW.staff_id
       AND a.id <> NEW.id
       AND a.status <> 'cancelled'
       AND tstzrange(
               a.starts_at - make_interval(mins => CASE
                   WHEN a.booking_group_id IS NOT NULL
                        AND a.booking_group_id = NEW.booking_group_id
                        AND a.status <> 'no_show' AND NEW.status <> 'no_show'
                        AND NEW.ends_at = a.starts_at
                   THEN 0 ELSE a.buffer_before_minutes END),
               a.ends_at + make_interval(mins => CASE
                   WHEN a.booking_group_id IS NOT NULL
                        AND a.booking_group_id = NEW.booking_group_id
                        AND a.status <> 'no_show' AND NEW.status <> 'no_show'
                        AND a.ends_at = NEW.starts_at
                   THEN 0 ELSE a.buffer_after_minutes END),
               '[)'
           )
       &&
           tstzrange(
               NEW.starts_at - make_interval(mins => CASE
                   WHEN a.booking_group_id IS NOT NULL
                        AND a.booking_group_id = NEW.booking_group_id
                        AND a.status <> 'no_show' AND NEW.status <> 'no_show'
                        AND a.ends_at = NEW.starts_at
                   THEN 0 ELSE NEW.buffer_before_minutes END),
               NEW.ends_at + make_interval(mins => CASE
                   WHEN a.booking_group_id IS NOT NULL
                        AND a.booking_group_id = NEW.booking_group_id
                        AND a.status <> 'no_show' AND NEW.status <> 'no_show'
                        AND NEW.ends_at = a.starts_at
                   THEN 0 ELSE NEW.buffer_after_minutes END),
               '[)'
           );
    IF taken >= allowed THEN
        RAISE EXCEPTION 'staff member % already has % overlapping appointment(s), limit %',
            NEW.staff_id, taken, allowed
            USING ERRCODE = 'exclusion_violation',
                  CONSTRAINT = 'tg_appointments_staff_concurrency',
                  TABLE = 'appointments';
    END IF;
    RETURN NEW;
END
$$
"""


def upgrade() -> None:
    op.execute(STAFF_CONCURRENCY)


def downgrade() -> None:
    op.execute(STAFF_CONCURRENCY_PREVIOUS)
