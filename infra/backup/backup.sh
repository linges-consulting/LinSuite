#!/bin/sh
# Nightly (or manual) backup: pg_dump -Fc streamed into `restic backup --stdin`, then a
# healthcheck ping (tech-stack §10, #89). POSIX sh (busybox ash), no bashisms.
#
# "Streamed" is real here, not a figure of speech: pg_dump writes into a named pipe and
# restic reads the other end, so the dump never touches disk. `set -e` alone only sees the
# exit status of the last command in a pipeline, which would hide a failed pg_dump behind a
# restic that happily backs up an empty/truncated stream — busybox ash has no `pipefail`, so
# both sides' exit statuses are captured explicitly below and checked after both finish.
set -eu

log() { echo "backup: $*"; }

ping() {
    # $1: "" for success, "/fail" for failure. A curl failure here must never fail the script
    # a second time over (fail() already calls this once) or mask the real exit status.
    [ -n "${HEALTHCHECK_URL_BACKUP:-}" ] || return 0
    curl -fsS -m 10 --retry 2 "${HEALTHCHECK_URL_BACKUP}${1:-}" >/dev/null 2>&1 || true
}

fail() {
    log "$*"
    ping "/fail"
    exit 1
}

: "${BACKUP_TARGET:?BACKUP_TARGET is not set (b2|s3|sftp|local)}"
: "${RESTIC_REPOSITORY:?RESTIC_REPOSITORY is not set}"
: "${RESTIC_PASSWORD:?RESTIC_PASSWORD is not set}"
: "${DATABASE_URL_BACKUP:?DATABASE_URL_BACKUP is not set}"

# BACKUP_TARGET is a label an operator chose; RESTIC_REPOSITORY is what restic actually
# connects with. They drift independently (a copy-pasted .env, a target switched without
# updating the repository URL), so check they agree before spending a dump on the wrong place.
case "$BACKUP_TARGET" in
    b2) case "$RESTIC_REPOSITORY" in b2:*) ;; *) fail "BACKUP_TARGET=b2 needs a RESTIC_REPOSITORY starting 'b2:'" ;; esac ;;
    s3) case "$RESTIC_REPOSITORY" in s3:*) ;; *) fail "BACKUP_TARGET=s3 needs a RESTIC_REPOSITORY starting 's3:'" ;; esac ;;
    sftp) case "$RESTIC_REPOSITORY" in sftp:*) ;; *) fail "BACKUP_TARGET=sftp needs a RESTIC_REPOSITORY starting 'sftp:'" ;; esac ;;
    local) case "$RESTIC_REPOSITORY" in /*) ;; *) fail "BACKUP_TARGET=local needs an absolute RESTIC_REPOSITORY path" ;; esac ;;
    *) fail "BACKUP_TARGET must be one of b2|s3|sftp|local, got '$BACKUP_TARGET'" ;;
esac

# Only when explicitly allowed (RESTIC_INIT=1): a repository that already has snapshots is
# left alone, and one truly empty is initialised. Anything else — bad credentials, a typo'd
# bucket — is a real error, not a silent second `restic init`.
if [ "${RESTIC_INIT:-0}" = "1" ]; then
    if ! restic snapshots >/dev/null 2>&1; then
        log "RESTIC_INIT=1 and no snapshots reachable: initialising $RESTIC_REPOSITORY"
        restic init || fail "restic init failed"
    fi
fi

fifo="$(mktemp -u)"
mkfifo "$fifo"
trap 'rm -f "$fifo"' EXIT

pg_dump -Fc --dbname "$DATABASE_URL_BACKUP" > "$fifo" &
dump_pid=$!

restic_status=0
restic backup --stdin --stdin-filename linsuite.dump --host linsuite < "$fifo" || restic_status=$?

dump_status=0
wait "$dump_pid" || dump_status=$?

[ "$dump_status" -eq 0 ] || fail "pg_dump exited $dump_status"
[ "$restic_status" -eq 0 ] || fail "restic backup exited $restic_status"

log "snapshot complete"
ping ""
