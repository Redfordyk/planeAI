#!/usr/bin/env bash
# scripts/backup/backup_postgres.sh — daily full pg_dump of the Plane
# database (which includes our ``ai_*`` tables and the pgvector
# embeddings). Custom format (``-Fc``) so pg_restore has full
# flexibility — selective object restore, parallel restore, ddl-only,
# data-only — and the file is already compressed.
#
# What gets backed up:
#   - All tables in the ``plane`` database, including ``ai_document_chunk``
#     (with embeddings, content, and content_hash), ``ai_workspace_config``
#     (encrypted keys at rest stay encrypted in the dump — they're
#     encrypted at the application layer via FIELD_ENCRYPTION_KEY),
#     ``ai_usage_log``, ``ai_agent_action_log``.
#   - DDL for indexes, including HNSW (``ai_chunk_hnsw_idx``). The
#     HNSW index is re-created by pg_restore at the end of the load,
#     so the restored DB has a working ANN index without us doing
#     anything special.
#
# Output: ``${BACKUP_LOCAL_DIR}/postgres/plane-<UTC_TIMESTAMP>.dump``
# then mirrored to ``s3://<bucket>/planeai-backups/postgres/<Y>/<M>/<D>/``.
#
# Exit codes:
#   0 — dump + offsite mirror succeeded.
#   1 — dump failed (Postgres unreachable, disk full, permission).
#   2 — dump OK but offsite mirror failed. The local file is kept;
#       cron will retry tomorrow, and operator can manually push the
#       file via ``mc cp`` (see BACKUP.md ¶ "manual offsite push").

set -Eeuo pipefail

BACKUP_SCRIPT_NAME="backup_postgres"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib_common.sh
. "${SCRIPT_DIR}/lib_common.sh"

register_cleanup_trap

LOCAL_DIR="${BACKUP_LOCAL_DIR:-/var/backups/planeai}/postgres"
mkdir -p "$LOCAL_DIR"

TIMESTAMP="$(date -u '+%Y%m%dT%H%M%SZ')"
DUMP_FILE="${LOCAL_DIR}/plane-${TIMESTAMP}.dump"
PARTIAL_FILE="$DUMP_FILE"  # picked up by on_exit_cleanup on failure

pg_export_password
PG_HOST="$(pg_host)"
PG_PORT="$(pg_port)"
PG_USER="$(pg_user)"
PG_DB="$(pg_db)"

log_info "starting pg_dump host=${PG_HOST} db=${PG_DB} -> ${DUMP_FILE}"

# --format=custom: best for pg_restore flexibility, built-in zstd-ish
#   compression at level 6 (the default).
# --no-acl --no-owner: the restore happens into a (possibly) different
#   role; ownership is re-applied by the restore script. Avoids
#   "role X does not exist" warnings on a clean target.
# --serializable-deferrable: takes a consistent snapshot without
#   blocking writers. Safe to run on a live DB.
# --no-password: rely on PGPASSWORD only. Without this pg_dump can
#   prompt on TTY, which never returns under cron.
pg_dump \
  --host="$PG_HOST" \
  --port="$PG_PORT" \
  --username="$PG_USER" \
  --dbname="$PG_DB" \
  --format=custom \
  --compress=6 \
  --no-owner \
  --no-acl \
  --serializable-deferrable \
  --no-password \
  --file="$DUMP_FILE"

DUMP_BYTES=$(stat -c '%s' "$DUMP_FILE" 2>/dev/null || stat -f '%z' "$DUMP_FILE")
log_info "pg_dump ok: ${DUMP_BYTES} bytes"

# Sanity check on the dump content: pg_restore --list should print the
# table-of-contents. If the dump is corrupt, this fails BEFORE we
# upload garbage offsite.
if ! pg_restore --list "$DUMP_FILE" >/dev/null 2>&1; then
  log_error "pg_restore --list failed on ${DUMP_FILE} — dump is corrupt"
  exit 1
fi

# We made it through dump + sanity. Clear the "partial" marker so
# trap doesn't delete the file if offsite mirror fails (we want to
# keep it locally for manual recovery).
PARTIAL_FILE=""

# Offsite mirror. mc alias is set up once; ``mc cp --md5`` adds an
# integrity check on upload.
mc_alias_setup
PREFIX="$(offsite_prefix postgres)"
log_info "mirroring to ${PREFIX}/"

if ! mc cp --md5 --quiet "$DUMP_FILE" "${PREFIX}/" >/dev/null; then
  log_error "offsite mirror failed — local file kept at ${DUMP_FILE}"
  exit 2
fi
log_info "offsite mirror ok"

# Local retention. Offsite retention is managed by bucket lifecycle
# rules — see BACKUP.md ¶ "offsite retention".
prune_local_spool "$LOCAL_DIR"

log_info "done"
