#!/usr/bin/env bash
# Run on the NAS/Backrest host. The VPS publishes only a read-only Restic repository over SFTP.
set -Eeuo pipefail

source_repo="${LINSUITE_SOURCE_REPO:-sftp:linsuite-vps:/}"
destination_repo="${LINSUITE_DESTINATION_REPO:-/backups/linsuite}"
source_password_file="${LINSUITE_SOURCE_PASSWORD_FILE:-/root/.config/linsuite-pull/source-password}"
destination_password_file="${LINSUITE_DESTINATION_PASSWORD_FILE:-/root/.config/linsuite-pull/nas-password}"
max_snapshot_age_hours="${LINSUITE_MAX_SNAPSHOT_AGE_HOURS:-36}"

for path in "$source_password_file" "$destination_password_file"; do
    test -s "$path" || { echo "Missing Restic password file: $path" >&2; exit 1; }
done

test -d "$destination_repo" || { echo "Missing NAS repository: $destination_repo" >&2; exit 1; }
exec 9>/run/lock/linsuite-nas-pull.lock
flock -n 9 || { echo 'A LinSuite NAS pull is already running' >&2; exit 1; }

# --no-lock is required because the VPS SFTP account is strictly read-only. The destination
# has exactly one scheduled writer (this unit, serialized by flock); Backrest must not schedule
# forget/prune or other write operations against this repository at the same time.
restic -r "$source_repo" --password-file "$source_password_file" --no-lock snapshots --json |
    python3 -c '
import datetime as dt
import json
import sys
snapshots = json.load(sys.stdin)
if not snapshots:
    sys.exit("VPS repository has no snapshots")
latest = max(dt.datetime.fromisoformat(s["time"].replace("Z", "+00:00")) for s in snapshots)
age = dt.datetime.now(dt.timezone.utc) - latest
limit = dt.timedelta(hours=int(sys.argv[1]))
if age > limit:
    sys.exit(f"Newest VPS snapshot is {age} old; expected no more than {limit}")
print(f"Newest VPS snapshot: {latest.isoformat()}")
' "$max_snapshot_age_hours"

restic -r "$destination_repo" --password-file "$destination_password_file" --no-lock \
    copy --from-repo "$source_repo" --from-password-file "$source_password_file"
restic -r "$destination_repo" --password-file "$destination_password_file" check
echo 'LinSuite NAS pull and repository check completed'
