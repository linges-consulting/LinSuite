#!/bin/sh
# Runs once, on first boot of an empty data volume. Creates the two roles from ADR-0001
# with the passwords from .env. Grants are applied by the Alembic baseline migration.
set -eu

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-SQL
    CREATE ROLE linsuite_app LOGIN PASSWORD '$APP_DB_PASSWORD';
    CREATE ROLE linsuite_purge LOGIN PASSWORD '$PURGE_DB_PASSWORD';
SQL
