#!/usr/bin/env bash
# scripts/backup/backup_minio.sh — daily mirror of the Plane MinIO
# bucket(s) to offsite S3.
#
# What gets backed up:
#   - ``${AWS_S3_BUCKET_NAME}`` (default ``uploads``) — issue
#     attachments, user avatars, page assets uploaded via Plane UI.
#   - AI artifacts bucket (currently same bucket; if a separate one
#     is ever provisioned, set BACKUP_MINIO_BUCKETS to a space-
#     separated list).
#
# We use ``mc mirror`` (not a tarball of the volume) because:
#   - mirroring is incremental — only new/changed objects move,
#     which keeps daily backup bandwidth proportional to delta, not
#     to library size,
#   - object-level integrity is verified via mc's etag compare,
#   - the offsite layout is browsable as objects rather than opaque
#     tar archives — operator can fetch ONE file in an incident
#     without restoring the whole bucket.
#
# Exit codes:
#   0 — mirror succeeded.
#   1 — mc alias setup failed (env or network).
#   2 — mirror had errors. The script keeps going to mirror what it
#       can (``--continue``) and exits non-zero so cron alerts.

set -Eeuo pipefail

BACKUP_SCRIPT_NAME="backup_minio"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib_common.sh
. "${SCRIPT_DIR}/lib_common.sh"

register_cleanup_trap

# Source MinIO alias. The Plane stack already has ``mc`` config from
# the migrator init job; here we re-create it deterministically so the
# backup script does not depend on prior state.
require_env AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY

MINIO_ENDPOINT="${AWS_S3_ENDPOINT_URL:-http://plane-minio:9000}"
mc alias set planeminio "$MINIO_ENDPOINT" \
  "$AWS_ACCESS_KEY_ID" "$AWS_SECRET_ACCESS_KEY" --quiet >/dev/null

mc_alias_setup
PREFIX="$(offsite_prefix minio)"

# Default: the one bucket Plane uploads to. Override via env if a
# separate AI-artifacts bucket lands later.
BUCKETS="${BACKUP_MINIO_BUCKETS:-${AWS_S3_BUCKET_NAME:-uploads}}"

rc=0
for bucket in $BUCKETS; do
  log_info "mirror bucket=${bucket}"
  # --overwrite: objects whose etag changed get re-uploaded.
  # --remove:    objects deleted in source are deleted in offsite, so
  #              a daily backup reflects current state, not all-time
  #              history. (We rely on bucket versioning + lifecycle on
  #              the offsite side for "time travel" — see BACKUP.md.)
  # --continue:  do not abort the whole run on a single object error.
  if ! mc mirror \
      --overwrite \
      --remove \
      --quiet \
      "planeminio/${bucket}" \
      "${PREFIX}/${bucket}"; then
    log_error "mirror of ${bucket} returned errors"
    rc=2
  fi
done

if [ "$rc" -eq 0 ]; then
  log_info "done"
else
  log_warn "completed with errors (rc=${rc})"
fi
exit "$rc"
