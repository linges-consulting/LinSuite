#!/bin/sh
# Runs once, on first boot of an empty data volume. Creates the three roles from ADR-0001
# and M5 (#89) with the passwords from .env. Grants are applied by the Alembic migrations
# (0001 for the first two, 0070 for linsuite_backup).
set -eu

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-SQL
    CREATE ROLE linsuite_app LOGIN PASSWORD '$APP_DB_PASSWORD';
    CREATE ROLE linsuite_purge LOGIN PASSWORD '$PURGE_DB_PASSWORD';
    CREATE ROLE linsuite_backup LOGIN PASSWORD '$BACKUP_DB_PASSWORD';
SQL
