#!/bin/sh
# Writes the crontab from BACKUP_SCHEDULE (.env.example), then runs crond in the foreground so
# the container's logs are the cron log — `docker compose logs backup` is the whole story, the
# same shape `beat`/`worker` already have. A manual run (ticket #89 AC1) does not go through
# here at all: `docker compose exec backup backup.sh`, or `docker compose run --rm backup
# backup.sh`, runs the script directly.
set -eu

SCHEDULE="${BACKUP_SCHEDULE:-0 2 * * *}"

# Every var backup.sh reads must survive into cron's own process, which does not inherit the
# container's environment the way an interactive shell does — so freeze it into /etc/environment
# and have the crontab line source it. (`env` here, not `printenv`, so a value containing a
# newline cannot inject a second crontab line — none of ours do, but the redirection form makes
# that a property of the mechanism rather than of today's variable list.)
env > /etc/environment

echo "$SCHEDULE . /etc/environment; /usr/local/bin/backup.sh" > /etc/crontabs/root

echo "backup: scheduled '$SCHEDULE' UTC (crond -f -l 2)"
exec crond -f -l 2
