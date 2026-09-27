"""Both seeded roles hold `queue.manage` (Phase 7 Task 2, #12).

Revision ID: 0042
Revises: 0041
Create Date: 2026-09-27

`queue.manage` goes to both seeded roles, the same call `0028_form_links.py` already made for
`forms.issue`: adding a walk-in and marking one abandoned is front-desk work, done in Staff
Mode, not an administrative decision the way `schedule.override_availability` is. The
Administrator role is meant to hold every capability the registry has (0022's own docstring);
Staff gets it too because there is no reason a front-desk account should need an escalation
just to take a name at the door.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0042"
down_revision: str | None = "0041"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "INSERT INTO role_capabilities (role_id, capability) "
        "SELECT id, 'queue.manage' FROM roles WHERE name IN ('Administrator', 'Staff') "
        "AND is_system ON CONFLICT DO NOTHING"
    )


def downgrade() -> None:
    op.execute("DELETE FROM role_capabilities WHERE capability = 'queue.manage'")
