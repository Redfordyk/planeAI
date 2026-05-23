#!/usr/bin/env bash
# scripts/backup/restore_postgres.sh — restore a Plane database from a
# pg_dump custom-format file.
#
# Usage:
#   restore_postgres.sh <dump-file>          # restore into env-configured target DB
#   restore_postgres.sh latest               # download latest from offsite first
#
# Required env: BACKUP_PG_HOST/PORT/USER/DB/PASSWORD (or the standard
# POSTGRES_* if the target is Plane's own DB) and, for ``latest``,
# BACKUP_S3_* + BACKUP_LOCAL_DIR.
#
# What this script ASSUMES about the target:
#   - The target Postgres has the ``vector`` extension available
#     (image ``pgvector/pgvector:0.8.2-pg15`` or local install).
#   - The target DB exists and is empty (or you accept that the
#     restore will refuse-conflict on existing objects). Use
#     ``--clean`` carefully on a non-empty DB.
#
# What pg_restore does for us:
#   - Creates schemas, tables, indexes (incl. HNSW), constraints.
#   - Re-creates the ``vector`` extension if it was in the source dump.
#   - Loads data via COPY.
#   - Re-builds HNSW at the end. For 100k chunks this takes ~1-3 min
#     on a 4 vCPU host; the restore script does not need to do
#     anything special — pg_dump captured the CREATE INDEX DDL.

set -Eeuo pipefail

BACKUP_SCRIPT_NAME="restore_postgres"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib_common.sh
. "${SCRIPT_DIR}/lib_common.sh"

if [ $# -lt 1 ]; then
  echo "usage: $0 <dump-file|latest>" >&2
  exit 64
fi

DUMP_ARG="$1"

resolve_latest_dump() {
  # Find the newest planeai-backups/postgres/ object on offsite and
  # download it to a temp file. Returns the local path on stdout.
  mc_alias_setup
  local newest
  newest="$(mc find "offsite/${BACKUP_S3_BUCKET}/planeai-backups/postgres/" \
              --name '*.dump' \
              --print '{time} {key}' 2>/dev/null \
            | sort -r | head -n 1 | awk '{print $2}')"
  if [ -z "$newest" ]; then
    log_error "no dumps found on offsite"
    exit 1
  fi
  local dest
  dest="${BACKUP_LOCAL_DIR:-/var/backups/planeai}/postgres/$(basename "$newest")"
  mkdir -p "$(dirname "$dest")"
  log_info "downloading ${newest} -> ${dest}"
  mc cp --quiet "$newest" "$dest" >/dev/null
  echo "$dest"
}

if [ "$DUMP_ARG" = "latest" ]; then
  DUMP_FILE="$(resolve_latest_dump)"
else
  DUMP_FILE="$DUMP_ARG"
fi

if [ ! -f "$DUMP_FILE" ]; then
  log_error "dump file not found: ${DUMP_FILE}"
  exit 1
fi

pg_export_password
PG_HOST="$(pg_host)"
PG_PORT="$(pg_port)"
PG_USER="$(pg_user)"
PG_DB="$(pg_db)"

log_info "restoring ${DUMP_FILE} -> host=${PG_HOST} db=${PG_DB}"

# Ensure the vector extension is present BEFORE pg_restore — otherwise
# the COPY for ai_document_chunk will fail with "type vector does not
# exist". pg_dump emits ``CREATE EXTENSION vector`` near the top of the
# DDL section, but on some pg_restore paths (selective restores, older
# CE versions) the extension comes after the table definitions.
#
# Idempotent: ``IF NOT EXISTS`` is a no-op when already installed.
psql --no-password \
  --host="$PG_HOST" \
  --port="$PG_PORT" \
  --username="$PG_USER" \
  --dbname="$PG_DB" \
  -v ON_ERROR_STOP=1 \
  -c "CREATE EXTENSION IF NOT EXISTS vector;" >/dev/null

# --jobs 4: parallel table COPY for speed. The HNSW CREATE INDEX is
# still serial inside pg_restore — pgvector's parallel-build path
# fires when the index is created via ``CREATE INDEX`` in psql, but
# the dump format records single-statement DDL.
# --no-owner --no-acl: ownership re-applied separately by deployment
# (Plane creates the ``plane`` role at first migration).
# --exit-on-error: stop on the first error rather than rolling onward
# and producing a half-restored DB.
pg_restore \
  --host="$PG_HOST" \
  --port="$PG_PORT" \
  --username="$PG_USER" \
  --dbname="$PG_DB" \
  --no-owner \
  --no-acl \
  --jobs=4 \
  --exit-on-error \
  --no-password \
  "$DUMP_FILE"

log_info "pg_restore completed"

# Post-restore sanity:
#   - vector extension present;
#   - ai_chunk_hnsw_idx index present;
#   - row count of ai_document_chunk known (informational only).
psql --no-password \
  --host="$PG_HOST" \
  --port="$PG_PORT" \
  --username="$PG_USER" \
  --dbname="$PG_DB" \
  -v ON_ERROR_STOP=1 \
  -c "SELECT 'vector ext: ' || extname FROM pg_extension WHERE extname='vector';" \
  -c "SELECT 'hnsw idx:   ' || indexname FROM pg_indexes WHERE indexname='ai_chunk_hnsw_idx';" \
  -c "SELECT 'chunks:     ' || count(*) FROM ai_document_chunk;"

log_info "done"
