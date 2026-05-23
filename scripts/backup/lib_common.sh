#!/usr/bin/env bash
# scripts/backup/lib_common.sh — sourced by every backup/restore script.
#
# Centralises:
#   - env var sanity check (PG creds, S3 creds, paths)
#   - logging with timestamps
#   - mc / aws CLI selection (mc preferred — same client as Plane's MinIO)
#   - on-trap cleanup so partial files do not pollute offsite storage
#
# Sourced, not executed. Do not run this file directly.

set -Eeuo pipefail

# ---------------------------------------------------------------------------
# Logging — every line goes to stderr so backup-script stdout stays
# reserved for machine-readable signals (e.g. the dump filename a
# wrapper might capture). All messages are prefixed with the script's
# basename so a multi-script cron run is greppable.
# ---------------------------------------------------------------------------
_log() {
  printf '[%s] [%s] %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "${BACKUP_SCRIPT_NAME:-backup}" "$*" >&2
}

log_info()  { _log "INFO  $*"; }
log_warn()  { _log "WARN  $*"; }
log_error() { _log "ERROR $*"; }

# ---------------------------------------------------------------------------
# Required env. Every script declares which subset it needs via
# require_env "VAR1" "VAR2" ... — fails fast with a clear message.
# ---------------------------------------------------------------------------
require_env() {
  local missing=()
  for v in "$@"; do
    if [ -z "${!v:-}" ]; then
      missing+=("$v")
    fi
  done
  if [ ${#missing[@]} -gt 0 ]; then
    log_error "missing required env vars: ${missing[*]}"
    return 1
  fi
}

# ---------------------------------------------------------------------------
# Postgres host/port/user resolution. Defaults match the docker-compose
# service name ``plane-db`` and the standard image config. Override via
# env when running from a different host (e.g. systemd timer on the
# Docker host calling into the container's exposed port).
# ---------------------------------------------------------------------------
pg_host()     { echo "${BACKUP_PG_HOST:-plane-db}"; }
pg_port()     { echo "${BACKUP_PG_PORT:-5432}"; }
pg_user()     { echo "${POSTGRES_USER:-${BACKUP_PG_USER:-plane}}"; }
pg_db()       { echo "${POSTGRES_DB:-${BACKUP_PG_DB:-plane}}"; }
pg_password() { echo "${POSTGRES_PASSWORD:-${BACKUP_PG_PASSWORD:-plane}}"; }

# Exported PGPASSWORD avoids passing creds on the cmdline (would show
# up in ``ps``). Every script that runs psql/pg_dump/pg_restore should
# call this once near the top.
pg_export_password() {
  PGPASSWORD="$(pg_password)"
  export PGPASSWORD
}

# ---------------------------------------------------------------------------
# Offsite (S3-compatible) config. We use ``mc`` (MinIO client) because:
#   - it's already in the team's mental model (Plane's MinIO uses mc),
#   - it talks to AWS S3, Backblaze B2, Cloudflare R2, Wasabi, and
#     self-hosted MinIO with the same alias/syntax,
#   - the recursive mirror command is one line.
#
# Operator picks the vendor by setting the four env vars in .env
# (see BACKUP.md). No vendor name is hardcoded here.
# ---------------------------------------------------------------------------

# Configure an mc alias once per run. Idempotent: ``mc alias set``
# overwrites silently on rerun. Alias name is fixed (``offsite``) so
# the rest of the scripts use a stable identifier.
mc_alias_setup() {
  require_env BACKUP_S3_ENDPOINT BACKUP_S3_ACCESS_KEY BACKUP_S3_SECRET_KEY BACKUP_S3_BUCKET
  mc alias set offsite \
    "$BACKUP_S3_ENDPOINT" \
    "$BACKUP_S3_ACCESS_KEY" \
    "$BACKUP_S3_SECRET_KEY" \
    --quiet >/dev/null
}

# Offsite path for a given local kind (``postgres`` / ``minio``).
# Layout: s3://<bucket>/planeai-backups/<kind>/<YYYY>/<MM>/<DD>/<file>.
# Date-tiered so a `mc ls offsite/<bucket>/planeai-backups/postgres/2026/07/`
# answers "what dumps do we have for this month?" without a 10k-entry
# listing.
offsite_prefix() {
  local kind="$1" date_dir
  date_dir="$(date -u '+%Y/%m/%d')"
  echo "offsite/${BACKUP_S3_BUCKET}/planeai-backups/${kind}/${date_dir}"
}

# ---------------------------------------------------------------------------
# Retention — apply on the LOCAL spool dir (not on offsite; the offsite
# bucket uses lifecycle rules instead, see BACKUP.md).
#
# Default 14 days of daily dumps locally — covers the case where the
# offsite bucket is temporarily unreachable on backup day, plus a
# margin for restore tests pulling yesterday's dump quickly.
# ---------------------------------------------------------------------------
prune_local_spool() {
  local dir="$1" keep_days="${BACKUP_LOCAL_RETENTION_DAYS:-14}"
  [ -d "$dir" ] || return 0
  log_info "pruning ${dir} (keep ${keep_days}d)"
  find "$dir" -maxdepth 1 -type f -mtime "+${keep_days}" -print -delete >&2 || true
}

# ---------------------------------------------------------------------------
# Trap helper — every script registers a cleanup function that nukes
# partial files on failure. Without this a half-written .dump.gz can
# get mirrored offsite and silently corrupt the restore path.
# ---------------------------------------------------------------------------
on_exit_cleanup() {
  local rc=$?
  if [ "$rc" -ne 0 ] && [ -n "${PARTIAL_FILE:-}" ] && [ -e "$PARTIAL_FILE" ]; then
    log_warn "removing partial output ${PARTIAL_FILE}"
    rm -f "$PARTIAL_FILE"
  fi
  exit "$rc"
}
register_cleanup_trap() {
  trap on_exit_cleanup EXIT INT TERM
}
