"""The staff-concurrency trigger learns about the handover waiver (Task 18, fix round 2).

Revision ID: 0017
Revises: 0016
Create Date: 2026-09-21

Two appointments sharing one `booking_group_id`, both occupying (neither cancelled nor
no-show), whose raw spans touch — one's `ends_at` the other's `starts_at` — are a handover:
same client, no turnover between them. Their two *facing* buffers do not count against each
other for this trigger's overlap test; every other buffer, against every other appointment,
still applies in full.

This has to live in the trigger rather than in application code, and it has to be *derived*
rather than stored: `scheduling/appointments.py`'s fix round 1 tried zeroing the sibling's own
`buffer_before_minutes`/`buffer_after_minutes` snapshot, and that broke the snapshot contract
(the row started lying about the service's real buffer) and never got restored when the
sibling was later cancelled, no-showed or moved away. Reading `booking_group_id`, `status`,
`starts_at` and `ends_at` straight off both rows being compared — nothing stored for this at
all — is what makes the waiver self-correcting: the moment a sibling stops being adjacent or
stops occupying, the very next comparison simply does not match, with no code anywhere to
"restore" it. `scheduling.availability.waive_handover` is the same rule, in Python, for the
loader and for `appointments.recompute_group_periods`'s equivalent job on resource periods
(which *does* have to store something, because a GiST exclusion constraint cannot be made
conditional the way a trigger's own procedural check can).

`ex_appointment_resources_no_overlap` needs no equivalent change: it is a declarative
exclusion constraint with no room for this kind of per-pair exception, which is exactly why
the resource side is handled by keeping `appointment_resources.period` itself correct on
every write instead.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STAFF_CONCURRENCY = """
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

# The exact 0014 body, restored on downgrade — no handover exemption, `span` precomputed once.
STAFF_CONCURRENCY_PREVIOUS = """
CREATE OR REPLACE FUNCTION appointments_enforce_staff_concurrency() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    allowed integer;
    taken integer;
    span tstzrange;
BEGIN
    IF NEW.status = 'cancelled' THEN
        RETURN NEW;
    END IF;
    SELECT max_concurrent_appointments INTO allowed
      FROM staff WHERE id = NEW.staff_id FOR UPDATE;
    span := tstzrange(
        NEW.starts_at - make_interval(mins => NEW.buffer_before_minutes),
        NEW.ends_at + make_interval(mins => NEW.buffer_after_minutes),
        '[)'
    );
    SELECT count(*) INTO taken
      FROM appointments a
     WHERE a.staff_id = NEW.staff_id
       AND a.id <> NEW.id
       AND a.status <> 'cancelled'
       AND tstzrange(
               a.starts_at - make_interval(mins => a.buffer_before_minutes),
               a.ends_at + make_interval(mins => a.buffer_after_minutes),
               '[)'
           ) && span;
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
