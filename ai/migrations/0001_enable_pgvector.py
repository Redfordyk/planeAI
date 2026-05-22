"""Enable pgvector before any model migration in this app.

Django doesn't auto-activate Postgres extensions; if 0002_initial
runs first, the VectorField column type doesn't exist and the table
creation fails. This migration runs first (no `dependencies`) so the
extension is active before 0002 references the `vector` type.

pgvector must be >= 0.8.2 — the bundled binary is provided by the
`pgvector/pgvector:0.8.2-pg15` image we run (see PGVECTOR.md). Older
versions are vulnerable to CVE-2026-3172 during parallel HNSW build.
"""

from django.contrib.postgres.operations import CreateExtension
from django.db import migrations


class Migration(migrations.Migration):

    initial = True
    dependencies: list = []

    operations = [
        # CreateExtension is the Postgres-extension-aware wrapper around
        # `CREATE EXTENSION IF NOT EXISTS vector;`. It's reversible
        # (drops the extension on rollback) and idempotent — Plane's
        # migrator can re-run it without erroring.
        CreateExtension("vector"),
    ]
