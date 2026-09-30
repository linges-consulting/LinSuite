#!/bin/sh
# Writes the crontab from BACKUP_SCHEDULE (.env.example), then runs crond in the foreground so
# the container's logs are the cron log — `docker compose logs backup` is the whole story, the
# same shape `beat`/`worker` already have. A manual run (ticket #89 AC1) does not go through
# here at all: `docker compose exec backup backup.sh`, or `docker compose run --rm backup
# backup.sh`, runs the script directly.
set -eu

# A one-off `docker compose run --rm backup backup.sh` should run the requested command.
if [ "$#" -gt 0 ]; then
    exec "$@"
fi

SCHEDULE="${BACKUP_SCHEDULE:-0 2 * * *}"

# BusyBox crond gives jobs a minimal environment. `export -p` writes shell-quoted assignments,
# so values containing spaces (notably BACKUP_SCHEDULE) survive when the cron job sources it.
# A plain `env > file` is not sourceable for those values and silently breaks the nightly job.
export -p > /etc/backup.env

echo "$SCHEDULE . /etc/backup.env; /usr/local/bin/backup.sh" > /etc/crontabs/root

echo "backup: scheduled '$SCHEDULE' UTC (crond -f -l 2)"
exec crond -f -l 2
