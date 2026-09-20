"""Working hours, split shifts, time off and closures.

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-19

Three tables and one extension.

`btree_gist` is what lets an exclusion constraint mix an equality column with a range one —
`staff_id WITH =` beside `int4range(...) WITH &&` — because the default GiST opclasses cover
ranges and geometry but not plain scalars. Installed here rather than in the baseline because
this is the first migration that needs it; Task 15's `EXCLUDE` on appointment resources needs
the same extension and `IF NOT EXISTS` makes that a no-op.

**`working_hours` holds no timezone, deliberately.** Minutes since local midnight against
`Business.timezone`, read at query time — see `scheduling/models.py` and CLAUDE.md "Time".
The CHECK constraints are the half of the rule the API cannot enforce when the next writer is
a psql session: inside the day, five-minute steps, and a start before its end.

**The two exclusion constraints** are the same idea applied to a recurring rule and to an
instant. `int4range` and `tstzrange` are both half-open, so blocks that merely touch —
09:00–12:00 then 12:00–15:00 — are allowed, which is somebody who does not take lunch.

Grants are inherited from 0001's ALTER DEFAULT PRIVILEGES; nothing is repeated here.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")

    op.create_table(
        "working_hours",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "staff_id",
            sa.Uuid(),
            sa.ForeignKey("staff.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("weekday", sa.Integer(), nullable=False),
        sa.Column("start_minute", sa.Integer(), nullable=False),
        sa.Column("end_minute", sa.Integer(), nullable=False),
        # ISO weekday, Monday = 0, matching `datetime.weekday()`.
        sa.CheckConstraint("weekday BETWEEN 0 AND 6", name="ck_working_hours_weekday"),
        # 1440 is allowed on the end: a block may run to midnight.
        sa.CheckConstraint(
            "start_minute >= 0 AND end_minute <= 1440 AND start_minute < end_minute",
            name="ck_working_hours_within_the_day",
        ),
        sa.CheckConstraint(
            "start_minute % 5 = 0 AND end_minute % 5 = 0", name="ck_working_hours_five_minute_steps"
        ),
    )
    op.execute(
        "ALTER TABLE working_hours ADD CONSTRAINT ex_working_hours_no_overlap "
        "EXCLUDE USING gist ("
        "staff_id WITH =, weekday WITH =, int4range(start_minute, end_minute) WITH &&)"
    )
    # How Task 14 reads it: one staff member's whole week, in week order.
    op.create_index(
        "ix_working_hours_staff_week", "working_hours", ["staff_id", "weekday", "start_minute"]
    )

    op.create_table(
        "time_off",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "staff_id",
            sa.Uuid(),
            sa.ForeignKey("staff.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("all_day", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("reason", sa.String(200)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("starts_at < ends_at", name="ck_time_off_span"),
    )
    op.execute(
        "ALTER TABLE time_off ADD CONSTRAINT ex_time_off_no_overlap "
        "EXCLUDE USING gist (staff_id WITH =, tstzrange(starts_at, ends_at) WITH &&)"
    )
    op.create_index("ix_time_off_staff_start", "time_off", ["staff_id", "starts_at"])

    op.create_table(
        "closures",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("source", sa.String(16), server_default=sa.text("'manual'"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("source IN ('manual', 'statutory')", name="ck_closures_source"),
        # Named, not `unique=True` on the column: that would leave Postgres to call it
        # `closures_date_key`, a name nothing in this repository says out loud.
        sa.UniqueConstraint("date", name="uq_closures_date"),
    )


def downgrade() -> None:
    op.drop_table("closures")
    op.drop_index("ix_time_off_staff_start", table_name="time_off")
    op.drop_table("time_off")
    op.drop_index("ix_working_hours_staff_week", table_name="working_hours")
    op.drop_table("working_hours")
    # The extension stays: Task 15's constraint needs it, and dropping it here would take
    # that one with it.
