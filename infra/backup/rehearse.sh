#!/bin/sh
# The restore rehearsal (#89, tech-stack §10, ADR-0001 rule 9): prove — every PR, and by hand
# against the real offsite repository — that a backup taken by `backup.sh` actually restores.
#
#   1. A throwaway Postgres, migrated to head.
#   2. Seed one customer with one sealed document.
#   3. Back up with the *real* `backup.sh`, inside the *real* backup image, to a local restic
#      repository (BACKUP_TARGET=local — no offsite credentials needed to prove the mechanism).
#   4. Restore into a second, fresh database in the same cluster.
#   5. Compare every table's row count and the document's ciphertext + digest, original vs
#      restored.
#   6. Run `customers.tasks.purge_expired` against the restored copy (ADR-0001 rule 9) — a
#      restore brings shredded keys back, so the documented recovery step is the one tested.
#
# Plain `docker run`/`docker network`, not compose: this needs one throwaway Postgres and the
# backup image, not the frontend, traefik or worker — simplest and most robust for CI.
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
BACKEND_DIR="$REPO_ROOT/app/backend"

RUN_ID="linsuite-rehearsal-$$"
NETWORK="${RUN_ID}-net"
DB_CONTAINER="${RUN_ID}-db"
BACKUP_CONTAINER="${RUN_ID}-backup"
IMAGE="linsuite-backup:${RUN_ID}"
WORKDIR=$(mktemp -d)

# Throwaway credentials for throwaway, network-isolated containers that this script deletes on
# exit — not escrowed, not real secrets.
OWNER_PW="rehearsal-owner"
APP_PW="rehearsal-app"
PURGE_PW="rehearsal-purge"
BACKUP_PW="rehearsal-backup"
RESTIC_PASSWORD="rehearsal-restic-password"

log() { echo "rehearse: $*"; }
fail() {
    log "FAILED: $*"
    exit 1
}

cleanup() {
    status=$?
    # The backup container writes the repo and the dump into the bind mounts as root. On a
    # Linux host (CI) the invoking user cannot delete root-owned files, so empty the mounts
    # from inside the container first; `rm` then fails only on the mount points themselves.
    docker exec "$BACKUP_CONTAINER" rm -rf /restic-repo /restore >/dev/null 2>&1 || true
    docker rm -f "$BACKUP_CONTAINER" >/dev/null 2>&1 || true
    docker rm -f "$DB_CONTAINER" >/dev/null 2>&1 || true
    docker network rm "$NETWORK" >/dev/null 2>&1 || true
    docker image rm "$IMAGE" >/dev/null 2>&1 || true
    rm -rf "$WORKDIR"
    if [ "$status" -eq 0 ]; then
        log "cleaned up, rehearsal OK"
    else
        log "cleaned up after failure (exit $status)"
    fi
}
trap cleanup EXIT INT TERM

docker network create "$NETWORK" >/dev/null

log "starting throwaway Postgres ($DB_CONTAINER)"
docker run -d --name "$DB_CONTAINER" --network "$NETWORK" \
    -e POSTGRES_USER=linsuite -e POSTGRES_PASSWORD="$OWNER_PW" -e POSTGRES_DB=linsuite \
    -e APP_DB_PASSWORD="$APP_PW" -e PURGE_DB_PASSWORD="$PURGE_PW" -e BACKUP_DB_PASSWORD="$BACKUP_PW" \
    -v "$REPO_ROOT/infra/db/init-roles.sh:/docker-entrypoint-initdb.d/10-roles.sh:ro" \
    -p 127.0.0.1::5432 \
    postgres:16-alpine >/dev/null

HOST_PORT=$(docker inspect -f '{{(index (index .NetworkSettings.Ports "5432/tcp") 0).HostPort}}' "$DB_CONTAINER")

log "waiting for Postgres to accept connections"
i=0
until docker exec "$DB_CONTAINER" pg_isready -U linsuite -d linsuite >/dev/null 2>&1; do
    i=$((i + 1))
    [ "$i" -lt 60 ] || fail "Postgres never became ready"
    sleep 1
done

# The three DSNs the app itself uses, plus the plain one pg_dump uses — all pointed at the
# host-published port so `uv run` on the host (migrations, seeding, the compare step, the
# post-restore purge) can reach the same container the backup image talks to over the network.
APP_URL="postgresql+asyncpg://linsuite_app:${APP_PW}@127.0.0.1:${HOST_PORT}/linsuite"
PURGE_URL="postgresql+asyncpg://linsuite_purge:${PURGE_PW}@127.0.0.1:${HOST_PORT}/linsuite"
MIGRATE_URL="postgresql+asyncpg://linsuite:${OWNER_PW}@127.0.0.1:${HOST_PORT}/linsuite"
OWNER_ASYNCPG_URL_ORIG="postgresql://linsuite:${OWNER_PW}@127.0.0.1:${HOST_PORT}/linsuite"
OWNER_ASYNCPG_URL_RESTORED="postgresql://linsuite:${OWNER_PW}@127.0.0.1:${HOST_PORT}/linsuite_restored"

# Every env var Settings() requires, fixed and distinct (same shape tests/conftest.py uses) —
# nothing here is escrowed or real, the whole cluster is gone when this script exits.
export DATABASE_URL="$APP_URL"
export DATABASE_URL_PURGE="$PURGE_URL"
export DATABASE_URL_MIGRATE="$MIGRATE_URL"
export REDIS_URL="redis://127.0.0.1:0/0" # never actually dialled by anything this script runs
export JWT_SECRET="rehearsal-jwt-secret-at-least-32-characters-long"
# Exactly 64 hex characters each (Settings rejects anything else) — built rather than
# hand-typed, so nobody has to eyeball-count a wall of repeated digits.
MFA_ENCRYPTION_KEY=$(printf '11%.0s' 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20 21 22 23 24 25 26 27 28 29 30 31 32)
DOCUMENT_MASTER_KEY=$(printf '22%.0s' 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20 21 22 23 24 25 26 27 28 29 30 31 32)
NOTIFICATION_CREDENTIAL_KEY=$(printf '33%.0s' 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20 21 22 23 24 25 26 27 28 29 30 31 32)
export MFA_ENCRYPTION_KEY DOCUMENT_MASTER_KEY NOTIFICATION_CREDENTIAL_KEY
export COOKIE_SECURE=false
export BREACH_CHECK_ENABLED=false
export NOTIFICATION_PROVIDER=console
export PYTHONPATH="$BACKEND_DIR/src"

log "migrating to head"
(cd "$BACKEND_DIR" && uv run alembic upgrade head) || fail "migration failed"

log "seeding a customer and a document"
SEED_OUT=$(cd "$BACKEND_DIR" && uv run python "$SCRIPT_DIR/seed.py") || fail "seeding failed"
CUSTOMER_ID=$(echo "$SEED_OUT" | awk '{print $1}')
DOCUMENT_ID=$(echo "$SEED_OUT" | awk '{print $2}')
[ -n "$CUSTOMER_ID" ] && [ -n "$DOCUMENT_ID" ] || fail "seed.py did not print customer/document ids: $SEED_OUT"
log "seeded customer=$CUSTOMER_ID document=$DOCUMENT_ID"

log "building the backup image (the same one infra/compose.yaml's backup service builds)"
docker build -q -t "$IMAGE" "$SCRIPT_DIR" >/dev/null || fail "image build failed"

log "starting the backup container"
docker run -d --name "$BACKUP_CONTAINER" --network "$NETWORK" \
    -v "$WORKDIR/restic-repo:/restic-repo" \
    -v "$WORKDIR/restore:/restore" \
    --entrypoint sleep "$IMAGE" infinity >/dev/null

log "running backup.sh for real (pg_dump | restic backup --stdin)"
docker exec -e BACKUP_TARGET=local -e RESTIC_REPOSITORY=/restic-repo -e RESTIC_PASSWORD="$RESTIC_PASSWORD" \
    -e RESTIC_INIT=1 -e DATABASE_URL_BACKUP="postgresql://linsuite_backup:${BACKUP_PW}@${DB_CONTAINER}:5432/linsuite" \
    "$BACKUP_CONTAINER" /usr/local/bin/backup.sh || fail "backup.sh exited non-zero"

log "restoring the snapshot into a fresh database (linsuite_restored)"
docker exec "$DB_CONTAINER" createdb -U linsuite -O linsuite linsuite_restored \
    || fail "createdb linsuite_restored failed"

docker exec -e RESTIC_REPOSITORY=/restic-repo -e RESTIC_PASSWORD="$RESTIC_PASSWORD" \
    "$BACKUP_CONTAINER" restic restore latest --target /restore \
    || fail "restic restore failed"

docker exec -e PGPASSWORD="$OWNER_PW" "$BACKUP_CONTAINER" \
    pg_restore -h "$DB_CONTAINER" -U linsuite -d linsuite_restored /restore/linsuite.dump \
    || fail "pg_restore failed"

log "comparing row counts and the document's ciphertext + digest"
(cd "$BACKEND_DIR" && uv run python "$SCRIPT_DIR/compare.py" \
    "$OWNER_ASYNCPG_URL_ORIG" "$OWNER_ASYNCPG_URL_RESTORED" "$DOCUMENT_ID") \
    || fail "restored database does not match the original"

log "running customers.tasks.purge_expired against the restored copy (ADR-0001 rule 9)"
export DATABASE_URL="postgresql+asyncpg://linsuite_app:${APP_PW}@127.0.0.1:${HOST_PORT}/linsuite_restored"
export DATABASE_URL_PURGE="postgresql+asyncpg://linsuite_purge:${PURGE_PW}@127.0.0.1:${HOST_PORT}/linsuite_restored"
(cd "$BACKEND_DIR" && uv run python -c "
from customers.tasks import purge_expired
result = purge_expired.run()
print('purge_expired on restored db:', result)
") || fail "purge_expired failed against the restored database"

log "rehearsal passed: restored database matches, post-restore purge ran cleanly"
