"""Baseline: the two database roles and their default privileges (ADR-0001).

Revision ID: 0001
Revises:
Create Date: 2026-09-18

Roles are cluster-level, so they are created only if missing. Passwords are never set
here: the deployment's db init script (infra/db/init-roles.sh) or the operator does that.
Tables created by later migrations inherit their grants from ALTER DEFAULT PRIVILEGES, so
the app role can use every table by default and immutable tables REVOKE selectively.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ROLES = ("linsuite_app", "linsuite_purge")


def upgrade() -> None:
    for role in ROLES:
        op.execute(
            f"""
            DO $$ BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
                    CREATE ROLE {role} LOGIN;
                END IF;
            END $$;
            """
        )
        op.execute(f"GRANT CONNECT ON DATABASE {_current_database()} TO {role}")
        op.execute(f"GRANT USAGE ON SCHEMA public TO {role}")

    # Everything the owner creates from here on is usable by the app role.
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO linsuite_app"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO linsuite_app"
    )
    # The purge role only ever reads and deletes; it never writes rows.
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, DELETE ON TABLES TO linsuite_purge"
    )


def downgrade() -> None:
    dp = "ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON"
    op.execute(f"{dp} TABLES FROM linsuite_app")
    op.execute(f"{dp} SEQUENCES FROM linsuite_app")
    op.execute(f"{dp} TABLES FROM linsuite_purge")
    for role in ROLES:
        op.execute(f"REVOKE ALL ON SCHEMA public FROM {role}")
        op.execute(f"REVOKE CONNECT ON DATABASE {_current_database()} FROM {role}")
    # Roles are deliberately kept: they may own nothing here but dropping a login role
    # is an operator decision, not a migration's.


def _current_database() -> str:
    return op.get_bind().execute(sa.text("SELECT current_database()")).scalar_one()
