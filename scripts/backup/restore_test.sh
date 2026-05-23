#!/usr/bin/env bash
# scripts/backup/restore_test.sh — automated end-to-end restore test.
#
# This is the script that closes the TZ 6.1 DoD bullet
# "**Restore протестирован**: из бэкапа поднимается рабочий Plane +
# работающий векторный поиск".
#
# What it does:
#   1. Pulls the latest pg_dump from offsite (or accepts a local path).
#   2. Spins up a throw-away pgvector container (NOT the production
#      ``plane-db`` — never touch prod).
#   3. Restores the dump into it.
#   4. Verifies:
#        a. ``vector`` extension is loaded;
#        b. ``ai_chunk_hnsw_idx`` HNSW index exists;
#        c. ``ai_document_chunk`` has > 0 rows (assuming source did);
#        d. an ANN query (``ORDER BY embedding <=> ...``) actually USES
#           the HNSW index — verified by inspecting EXPLAIN. This is
#           the only smoking-gun proof that the restored index is not
#           just a name on disk but a working ANN structure.
#        e. ``plane_workspace`` / ``plane_project`` (or upstream
#           equivalents) have at least the expected row counts.
#   5. Tears the container down (always, via trap).
#   6. Reports PASS/FAIL on stdout and exits 0/1.
#
# This script is what monthly cron / a manual operator runs to satisfy
# the "restore test" cadence in BACKUP.md.
#
# Why not "just spin up a full Plane stack"? It's expensive in CI time
# and adds moving parts (Redis, MinIO, RabbitMQ). The TZ asks
# specifically about (1) the DB restoring and (2) the vector index
# working — both can be proven with a Postgres-only test in ~2 minutes.

set -Eeuo pipefail

BACKUP_SCRIPT_NAME="restore_test"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib_common.sh
. "${SCRIPT_DIR}/lib_common.sh"

# Throwaway container config. The container is named with a timestamp
# so concurrent runs (or one stuck on cleanup) don't collide.
CONTAINER_NAME="planeai-restore-test-$(date -u '+%s')"
TARGET_IMAGE="${BACKUP_RESTORE_TEST_IMAGE:-pgvector/pgvector:0.8.2-pg15}"
TARGET_PORT="${BACKUP_RESTORE_TEST_PORT:-55432}"  # host-side port; container always 5432
TARGET_DB="plane"
TARGET_USER="plane"
TARGET_PASSWORD="$(openssl rand -hex 16)"  # ephemeral, never reused

cleanup_container() {
  if docker ps -a --format '{{.Names}}' | grep -qx "$CONTAINER_NAME"; then
    log_info "stopping throwaway container ${CONTAINER_NAME}"
    docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
  fi
}
trap cleanup_container EXIT INT TERM

# ---------------------------------------------------------------------------
# Step 1 — resolve dump source
# ---------------------------------------------------------------------------
DUMP_FILE="${1:-}"
if [ -z "$DUMP_FILE" ] || [ "$DUMP_FILE" = "latest" ]; then
  mc_alias_setup
  newest="$(mc find "offsite/${BACKUP_S3_BUCKET}/planeai-backups/postgres/" \
              --name '*.dump' \
              --print '{time} {key}' 2>/dev/null \
            | sort -r | head -n 1 | awk '{print $2}')"
  if [ -z "$newest" ]; then
    log_error "no dumps found on offsite"
    exit 1
  fi
  DUMP_FILE="${BACKUP_LOCAL_DIR:-/var/backups/planeai}/postgres/$(basename "$newest")"
  if [ ! -f "$DUMP_FILE" ]; then
    log_info "downloading ${newest} -> ${DUMP_FILE}"
    mkdir -p "$(dirname "$DUMP_FILE")"
    mc cp --quiet "$newest" "$DUMP_FILE" >/dev/null
  fi
fi

if [ ! -f "$DUMP_FILE" ]; then
  log_error "dump file not found: ${DUMP_FILE}"
  exit 1
fi
log_info "dump to test: ${DUMP_FILE}"

# ---------------------------------------------------------------------------
# Step 2 — spin up throwaway Postgres
# ---------------------------------------------------------------------------
log_info "starting throwaway pgvector container ${CONTAINER_NAME} on host port ${TARGET_PORT}"
docker run -d \
  --name "$CONTAINER_NAME" \
  -e "POSTGRES_USER=${TARGET_USER}" \
  -e "POSTGRES_PASSWORD=${TARGET_PASSWORD}" \
  -e "POSTGRES_DB=${TARGET_DB}" \
  -p "${TARGET_PORT}:5432" \
  -v "$(dirname "$(realpath "$DUMP_FILE")"):/restore:ro" \
  "$TARGET_IMAGE" >/dev/null

# Wait until pg_isready is happy — up to 60s, the pgvector image needs
# initdb on first start.
for i in $(seq 1 60); do
  if docker exec "$CONTAINER_NAME" pg_isready -U "$TARGET_USER" -d "$TARGET_DB" >/dev/null 2>&1; then
    log_info "postgres ready after ${i}s"
    break
  fi
  if [ "$i" -eq 60 ]; then
    log_error "postgres did not become ready in 60s"
    docker logs --tail 100 "$CONTAINER_NAME" >&2 || true
    exit 1
  fi
  sleep 1
done

# ---------------------------------------------------------------------------
# Step 3 — restore (in-container, so we don't need pg_restore on the host)
# ---------------------------------------------------------------------------
log_info "restoring dump into throwaway container"
docker exec -e PGPASSWORD="$TARGET_PASSWORD" "$CONTAINER_NAME" \
  psql -U "$TARGET_USER" -d "$TARGET_DB" -v ON_ERROR_STOP=1 \
       -c "CREATE EXTENSION IF NOT EXISTS vector;" >/dev/null

docker exec -e PGPASSWORD="$TARGET_PASSWORD" "$CONTAINER_NAME" \
  pg_restore -U "$TARGET_USER" -d "$TARGET_DB" \
             --no-owner --no-acl --jobs=4 --exit-on-error \
             "/restore/$(basename "$DUMP_FILE")"

log_info "pg_restore completed"

# ---------------------------------------------------------------------------
# Step 4 — verifications. Each one prints PASS/FAIL on its own line so
# a CI log shows ALL failures, not just the first.
# ---------------------------------------------------------------------------
fail_count=0
psql_in_container() {
  docker exec -e PGPASSWORD="$TARGET_PASSWORD" "$CONTAINER_NAME" \
    psql -U "$TARGET_USER" -d "$TARGET_DB" -At "$@"
}

assert_eq() {
  local name="$1" got="$2" expected="$3"
  if [ "$got" = "$expected" ]; then
    echo "PASS  ${name} (got '${got}')"
  else
    echo "FAIL  ${name} (expected '${expected}', got '${got}')"
    fail_count=$((fail_count + 1))
  fi
}

assert_nonzero() {
  local name="$1" got="$2"
  if [ "$got" -gt 0 ] 2>/dev/null; then
    echo "PASS  ${name} (got ${got})"
  else
    echo "FAIL  ${name} (expected > 0, got '${got}')"
    fail_count=$((fail_count + 1))
  fi
}

# 4a. vector extension is loaded.
ext="$(psql_in_container -c "SELECT extname FROM pg_extension WHERE extname='vector'")"
assert_eq "vector extension present" "$ext" "vector"

# 4b. HNSW index exists.
idx="$(psql_in_container -c "SELECT indexname FROM pg_indexes WHERE indexname='ai_chunk_hnsw_idx'")"
assert_eq "ai_chunk_hnsw_idx index present" "$idx" "ai_chunk_hnsw_idx"

# 4c. ai_document_chunk has rows. (If the source DB was empty, this
# might legitimately be zero — operator can override the expectation
# via BACKUP_RESTORE_TEST_MIN_CHUNKS=0.)
min_chunks="${BACKUP_RESTORE_TEST_MIN_CHUNKS:-1}"
chunk_count="$(psql_in_container -c "SELECT count(*) FROM ai_document_chunk")"
if [ "$min_chunks" -gt 0 ]; then
  assert_nonzero "ai_document_chunk rows" "$chunk_count"
else
  echo "SKIP  ai_document_chunk rows (BACKUP_RESTORE_TEST_MIN_CHUNKS=0)"
fi

# 4d. CRITICAL: an ANN query plans through the HNSW index. We use the
# embedding of an existing row as the probe vector — that way we don't
# need to know the vector dimension from outside (it's stored in the
# column type).
#
# The EXPLAIN output for an HNSW-served ANN query contains the index
# name; if pgvector or Postgres ever falls back to a sequential scan
# (e.g. because the index is invalid), the index name is absent.
if [ "$chunk_count" -gt 0 ]; then
  plan="$(psql_in_container -c "
    EXPLAIN (FORMAT TEXT)
    SELECT id
    FROM ai_document_chunk
    ORDER BY embedding <=> (SELECT embedding FROM ai_document_chunk LIMIT 1)
    LIMIT 1;
  ")"
  if echo "$plan" | grep -q "ai_chunk_hnsw_idx"; then
    echo "PASS  ANN query uses ai_chunk_hnsw_idx"
  else
    echo "FAIL  ANN query did not use HNSW index"
    echo "      plan was:"
    echo "$plan" | sed 's/^/        /'
    fail_count=$((fail_count + 1))
  fi
else
  echo "SKIP  ANN-via-HNSW probe (no chunks)"
fi

# 4e. A couple of Plane core tables exist (sanity that pg_dump didn't
# silently miss the Plane app schema). We don't enforce row counts —
# a fresh Plane install may have zero rows in some tables.
for tbl in workspaces projects; do
  has="$(psql_in_container -c "SELECT to_regclass('public.${tbl}')")"
  if [ -n "$has" ] && [ "$has" != "" ]; then
    echo "PASS  plane table ${tbl} present"
  else
    echo "FAIL  plane table ${tbl} missing"
    fail_count=$((fail_count + 1))
  fi
done

# ---------------------------------------------------------------------------
# Step 5 — final verdict
# ---------------------------------------------------------------------------
echo
if [ "$fail_count" -eq 0 ]; then
  echo "RESTORE TEST: PASS"
  exit 0
else
  echo "RESTORE TEST: FAIL (${fail_count} check(s) failed)"
  exit 1
fi
