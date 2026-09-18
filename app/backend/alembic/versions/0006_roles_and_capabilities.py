"""Capability-scoped roles, and the end of `users.is_admin`.

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-18

`is_admin` answered one question with a boolean — "may this account administer?" — and PRD §1
asks a different one: which of a list of functional capabilities does this person hold. The
flag cannot be widened into that, so it is replaced rather than kept beside it. Keeping both
would mean two authorities that can disagree, and every check having to decide which wins.

The capability keys below are written out literally rather than imported from
`auth/capabilities.py`. A migration is a statement about one historical moment; importing a
registry that keeps changing would make this file mean something different on every deploy,
and re-running it a year from now would seed roles nobody reviewed.

The data move is the whole point of the revision: every `is_admin = true` account becomes an
Administrator and everyone else becomes Staff, so no session and no account is left without
an authorisation signal at the instant the column disappears.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The registry as it stood at this revision (`auth/capabilities.py`).
ALL_CAPABILITIES = (
    "admin",
    "roles.manage",
    "users.manage",
    "catalog.manage",
    "schedule.view",
    "schedule.manage",
    "schedule.override_availability",
    "customers.view",
    "customers.manage",
)

# Deliberately not every operational capability: `schedule.override_availability` commits
# somebody else's evening, which tech-stack §9 calls a labour decision rather than a
# scheduling one, and the default role should not make it for them.
STAFF_CAPABILITIES = (
    "schedule.view",
    "schedule.manage",
    "customers.view",
    "customers.manage",
)


def upgrade() -> None:
    op.create_table(
        "roles",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("name", sa.String(64), nullable=False, unique=True),
        sa.Column("description", sa.String(200), server_default="", nullable=False),
        # The two roles seeded below. Read-only through the API: an administrator who could
        # edit `Administrator` could remove their own ability to edit it back, and no second
        # path into this instance exists to undo that.
        sa.Column("is_system", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_table(
        "role_capabilities",
        sa.Column(
            "role_id",
            sa.Uuid(),
            sa.ForeignKey("roles.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        # A key from the code registry, checked at the API boundary. Not an enum: capabilities
        # arrive and retire with deploys, and an enum would tie each to a migration that has
        # to land in the same breath as the code.
        sa.Column("capability", sa.String(64), primary_key=True),
    )

    roles = sa.table(
        "roles",
        sa.column("id", sa.Uuid()),
        sa.column("name", sa.String()),
        sa.column("description", sa.String()),
        sa.column("is_system", sa.Boolean()),
    )
    grants = sa.table(
        "role_capabilities", sa.column("role_id", sa.Uuid()), sa.column("capability", sa.String())
    )

    op.bulk_insert(
        roles,
        [
            {
                "name": "Administrator",
                "description": "Full access, including business settings, roles and accounts.",
                "is_system": True,
            },
            {
                "name": "Staff",
                "description": "Day-to-day work: the calendar and customer records.",
                "is_system": True,
            },
        ],
    )
    # A statement per grant. The set is nine rows at this revision, and a readable INSERT
    # that an operator can follow in `alembic upgrade --sql` output is worth more here than
    # one round trip saved on a table that is written once in the life of the database.
    for role_name, capabilities in (
        ("Administrator", ALL_CAPABILITIES),
        ("Staff", STAFF_CAPABILITIES),
    ):
        for capability in capabilities:
            op.execute(
                grants.insert().from_select(
                    ["role_id", "capability"],
                    sa.select(roles.c.id, sa.literal(capability)).where(roles.c.name == role_name),
                )
            )

    # Nullable first, filled, then tightened — the column cannot be born NOT NULL on a table
    # that already has rows, and the rows are what this revision exists to carry across.
    op.add_column("users", sa.Column("role_id", sa.Uuid(), nullable=True))
    op.execute(
        "UPDATE users SET role_id = (SELECT id FROM roles WHERE name = 'Administrator') "
        "WHERE is_admin"
    )
    op.execute(
        "UPDATE users SET role_id = (SELECT id FROM roles WHERE name = 'Staff') "
        "WHERE role_id IS NULL"
    )
    op.alter_column("users", "role_id", nullable=False)
    op.create_foreign_key(
        "fk_users_role_id", "users", "roles", ["role_id"], ["id"], ondelete="RESTRICT"
    )
    # "Who holds this role" is asked by every delete and by the last-administrator guard.
    op.create_index("ix_users_role_id", "users", ["role_id"])

    op.drop_column("users", "is_admin")


def downgrade() -> None:
    op.add_column(
        "users",
        sa.Column("is_admin", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    # The flag cannot express a custom role, so the honest reconstruction is "held the `admin`
    # capability". Anything finer is lost, which is what dropping a boolean for a set costs
    # in reverse.
    op.execute(
        "UPDATE users SET is_admin = true WHERE role_id IN "
        "(SELECT role_id FROM role_capabilities WHERE capability = 'admin')"
    )
    op.drop_index("ix_users_role_id", table_name="users")
    op.drop_constraint("fk_users_role_id", "users", type_="foreignkey")
    op.drop_column("users", "role_id")
    op.drop_table("role_capabilities")
    op.drop_table("roles")
