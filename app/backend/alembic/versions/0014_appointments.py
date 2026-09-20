"""Customers, appointments, and the two rules that make double-booking impossible.

Revision ID: 0014
Revises: 0013
Create Date: 2026-09-20

`customers` is the minimal record Task 15 books for: names, an optional email unique
case-insensitively when present, an optional phone stored as digits.

`appointments` holds one service for one staff member (CLAUDE.md "Domain rules") with the
service's duration, buffers and price **snapshotted** onto it — the contract on the `Service`
model: editing the catalog never rewrites what was booked. `starts_at`/`ends_at` are the span
the client sees, as `timestamptz`; the buffered span is derived where it is compared.

`appointment_resources` is one row per space or device the appointment claims, with `period`
the **buffered** span, and the exclusion constraint tech-stack §15 promises:
`EXCLUDE USING gist (resource_id WITH =, period WITH &&)`. No WHERE clause — cancelling
(Task 18) deletes these rows rather than flagging them, so a cancelled booking frees its
room by ceasing to claim it. `btree_gist` (0011) is what lets the equality column sit beside
the range one.

**Staff overlap is policy, so it is a trigger** (§20): `max_concurrent_appointments` is read
off the staff row, and the count of overlapping non-cancelled appointments over their
buffered spans may not reach it. The trigger takes `FOR UPDATE` on the staff row first, which
is what makes two simultaneous bookings for the same person serialise: under READ COMMITTED
each would otherwise count the other's uncommitted row as absent and both would commit. It
raises with SQLSTATE 23P01 (exclusion_violation) and a constraint name, so the API maps it
exactly as it maps the resource constraint — "that time was just taken".

Grants are inherited from 0001's ALTER DEFAULT PRIVILEGES; nothing is repeated here.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import TSTZRANGE

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STAFF_CONCURRENCY = """
CREATE FUNCTION appointments_enforce_staff_concurrency() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    allowed integer;
    taken integer;
    span tstzrange;
BEGIN
    IF NEW.status = 'cancelled' THEN
        RETURN NEW;
    END IF;
    -- Serialise bookings for one person: the second waits here until the first commits,
    -- and then counts it. Without the lock both would count zero and both would land.
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
# Two statements, two `op.execute` calls: asyncpg refuses more than one command per
# prepared statement, and a function body is one command however many semicolons it holds.
STAFF_CONCURRENCY_TRIGGER = """
CREATE TRIGGER tg_appointments_staff_concurrency
    BEFORE INSERT OR UPDATE ON appointments
    FOR EACH ROW EXECUTE FUNCTION appointments_enforce_staff_concurrency()
"""


def upgrade() -> None:
    op.create_table(
        "customers",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("first_name", sa.String(100), nullable=False),
        sa.Column("last_name", sa.String(100), nullable=False),
        sa.Column("email", sa.String(254)),
        sa.Column("phone", sa.String(32)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index(
        "ux_customers_email",
        "customers",
        [sa.text("lower(email)")],
        unique=True,
        postgresql_where=sa.text("email IS NOT NULL"),
    )

    op.create_table(
        "appointments",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        # No cascades: an appointment must never lose its customer, its provider or its
        # service. None of the three is ever deleted — they are deactivated — and a delete
        # that reached one of them should be refused by these rather than silently unhook
        # history.
        sa.Column("customer_id", sa.Uuid(), sa.ForeignKey("customers.id"), nullable=False),
        sa.Column("staff_id", sa.Uuid(), sa.ForeignKey("staff.id"), nullable=False),
        sa.Column("service_id", sa.Uuid(), sa.ForeignKey("services.id"), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        # The snapshot. Copied from the service at booking, read from here ever after.
        sa.Column("duration_minutes", sa.Integer(), nullable=False),
        sa.Column("buffer_before_minutes", sa.Integer(), nullable=False),
        sa.Column("buffer_after_minutes", sa.Integer(), nullable=False),
        sa.Column("price_cents", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), server_default=sa.text("'confirmed'"), nullable=False),
        # Task 18: linked appointments of one visit share this.
        sa.Column("booking_group_id", sa.Uuid()),
        sa.Column("notes", sa.Text()),
        sa.Column("created_by_user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('confirmed', 'completed', 'cancelled', 'no_show')",
            name="ck_appointments_status",
        ),
        sa.CheckConstraint("starts_at < ends_at", name="ck_appointments_span"),
        sa.CheckConstraint(
            "duration_minutes > 0 AND buffer_before_minutes >= 0 "
            "AND buffer_after_minutes >= 0 AND price_cents >= 0",
            name="ck_appointments_snapshot",
        ),
    )
    # How the calendar and the busy-interval loader read it: one person, a range of days.
    op.create_index("ix_appointments_staff_start", "appointments", ["staff_id", "starts_at"])
    op.create_index("ix_appointments_customer", "appointments", ["customer_id"])
    op.execute(STAFF_CONCURRENCY)
    op.execute(STAFF_CONCURRENCY_TRIGGER)

    op.create_table(
        "appointment_resources",
        sa.Column(
            "appointment_id",
            sa.Uuid(),
            sa.ForeignKey("appointments.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("resource_id", sa.Uuid(), sa.ForeignKey("resources.id"), primary_key=True),
        sa.Column("kind", sa.String(16), nullable=False),
        # The *buffered* span, half-open: the room is occupied while it is being turned over.
        sa.Column("period", TSTZRANGE(), nullable=False),
        sa.CheckConstraint("kind IN ('space', 'equipment')", name="ck_appointment_resources_kind"),
    )
    op.execute(
        "ALTER TABLE appointment_resources ADD CONSTRAINT ex_appointment_resources_no_overlap "
        "EXCLUDE USING gist (resource_id WITH =, period WITH &&)"
    )


def downgrade() -> None:
    op.drop_table("appointment_resources")
    op.execute("DROP TRIGGER tg_appointments_staff_concurrency ON appointments")
    op.execute("DROP FUNCTION appointments_enforce_staff_concurrency()")
    op.drop_index("ix_appointments_customer", table_name="appointments")
    op.drop_index("ix_appointments_staff_start", table_name="appointments")
    op.drop_table("appointments")
    op.drop_index("ux_customers_email", table_name="customers")
    op.drop_table("customers")
