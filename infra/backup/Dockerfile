# Same Postgres major as the `db` service (infra/compose.yaml) — pg_dump and pg_restore only
# talk to a server within one major version of themselves. Bump both together.
FROM postgres:16-alpine

RUN apk add --no-cache restic curl

COPY backup.sh /usr/local/bin/backup.sh
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/backup.sh /usr/local/bin/entrypoint.sh

# busybox crond runs in the foreground as PID 1; entrypoint.sh writes the crontab from
# BACKUP_SCHEDULE first. The postgres image already ships busybox (crond is part of it) and a
# non-root `postgres` user we do not need — this container only ever runs pg_dump as a client,
# never a server, so it stays root (crond's own default) rather than fighting the base image's
# ownership for no security gain.
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
