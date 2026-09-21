"""S2: what the booking handlers are willing to call "that time was just taken".

Three database refusals mean the same thing to the person at the desk — the picture they
were looking at is out of date, pick another time — and `_is_slot_taken` is the one place
that decides which. Two are the rules migration 0014 names; the third is a deadlock
(SQLSTATE 40P01), which `book_group` can provoke all by itself: it takes the trigger's
`FOR UPDATE` on each link's staff row in chain order, so two visits booked as (Ana, Ben)
and (Ben, Ana) are a cycle, and Postgres kills one of them. The client already retries a
409; an unmapped 500 would just look like a bug.
"""

from sqlalchemy.exc import DBAPIError

from scheduling.appointments import _is_slot_taken


class _Orig(Exception):
    """The DBAPI error SQLAlchemy wraps: asyncpg's translated error carries `sqlstate` (and
    `pgcode`), and a constraint refusal carries `constraint_name`."""

    def __init__(self, message: str = "boom", *, constraint_name=None, sqlstate=None):
        super().__init__(message)
        self.constraint_name = constraint_name
        self.sqlstate = self.pgcode = sqlstate


def _wrapped(orig: _Orig) -> DBAPIError:
    return DBAPIError("INSERT INTO appointments ...", None, orig)


def test_the_resource_exclusion_constraint_is_slot_taken():
    assert _is_slot_taken(
        _wrapped(_Orig(constraint_name="ex_appointment_resources_no_overlap", sqlstate="23P01"))
    )


def test_the_staff_concurrency_trigger_is_slot_taken():
    assert _is_slot_taken(
        _wrapped(_Orig(constraint_name="tg_appointments_staff_concurrency", sqlstate="23P01"))
    )


def test_a_deadlock_is_slot_taken():
    assert _is_slot_taken(_wrapped(_Orig("deadlock detected", sqlstate="40P01")))


def test_any_other_refusal_is_not():
    assert not _is_slot_taken(
        _wrapped(_Orig(constraint_name="ck_appointments_span", sqlstate="23514"))
    )
