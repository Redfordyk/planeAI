# scripts/backup/Dockerfile — image for the planeai-backup sidecar.
#
# Goals:
#   - postgresql-client 15 matching the production server image, so
#     pg_dump version mismatches never silently produce dumps the
#     prod cluster can't restore.
#   - mc (MinIO client) for offsite mirror and source-side MinIO talk.
#   - busybox crond (built into alpine) for the cron loop.
#   - openssl for the random password in restore_test.sh.
#   - bash (postgres-client and our scripts both require it).
#
# Why alpine: keeps the image ~150 MB. We deliberately do NOT bake
# AWS / Backblaze / R2 secrets into the image — those come in via env
# at run time (BACKUP_S3_*). The image is reusable across customers /
# backup destinations.

FROM alpine:3.20

# Pin client to pg15 — matches the postgres:15.7-alpine / pgvector
# 0.8.2-pg15 images Plane uses. pg_dump from a newer client can dump
# a 15 server but emits SQL that pg_restore 15 can't replay; matching
# major + minor avoids that whole class of bug.
RUN apk add --no-cache \
      bash \
      coreutils \
      findutils \
      tzdata \
      openssl \
      ca-certificates \
      postgresql15-client \
      curl \
      docker-cli \
  && curl -sSL https://dl.min.io/client/mc/release/linux-amd64/mc -o /usr/local/bin/mc \
  && chmod +x /usr/local/bin/mc

WORKDIR /opt/backup
COPY *.sh ./
COPY crontab /etc/cron.d/planeai-backup
RUN chmod +x ./*.sh \
 && chmod 0644 /etc/cron.d/planeai-backup

# Default cron loop. The entrypoint can also be overridden for
# one-shot runs (``docker run ... backup_postgres.sh``) without cron.
ENTRYPOINT ["/opt/backup/entrypoint.sh"]
CMD ["cron"]
