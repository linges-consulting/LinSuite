"""The Administrator role holds `audit.view` (Task 8, #43).

Revision ID: 0022
Revises: 0021
Create Date: 2026-09-21

A capability is code (`auth/capabilities.py`); a role holding it is data. The system
Administrator role is read-only through the API and is meant to hold every capability the
registry has, so a new capability reaches it here or not at all. The seeded Staff role does
not get it: who opened whose chart is an administrator's question.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "INSERT INTO role_capabilities (role_id, capability) "
        "SELECT id, 'audit.view' FROM roles WHERE name = 'Administrator' AND is_system "
        "ON CONFLICT DO NOTHING"
    )


def downgrade() -> None:
    op.execute("DELETE FROM role_capabilities WHERE capability = 'audit.view'")
