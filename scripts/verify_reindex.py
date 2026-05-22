"""Smoke-verify ai.tasks.reindex_source against live Plane models.

Install: docker cp scripts/verify_reindex.py \
    plane-ce-api-1:/code/plane/db/management/commands/verify_reindex.py

Run:     docker compose exec api python manage.py verify_reindex

Builds Workspace + Project + WorkspaceAIConfig + User + Issue with
real content, monkeypatches OpenAIEmbed.embed to return synthetic
1536-dim vectors, then calls reindex_source directly (not via
Celery) and asserts:

  - Initial reindex creates DocumentChunk rows
  - Each row has 1536-dim embedding
  - One AIUsageLog row of feature=embed
  - Second reindex with unchanged content is a no-op (idempotent)
  - Changing the title rebuilds chunks and writes a new usage log
  - delete_chunks removes all chunks for the source
"""

from __future__ import annotations

import uuid

from django.core.management.base import BaseCommand

from ai import providers
from ai.models import AIUsageLog, DocumentChunk, WorkspaceAIConfig
from ai.tasks import delete_chunks, reindex_source
from plane.db.models import Issue, Project, User, Workspace


SLUG = f"reindex-smoke-{uuid.uuid4().hex[:8]}"


class FakeEmbed:
    """Stand-in for providers.OpenAIEmbed. Returns deterministic
    synthetic vectors so we can assert dimension and counts."""

    calls: list[int] = []  # records batch sizes per embed() call

    def __init__(self, api_key, model="text-embedding-3-small"):
        self.api_key = api_key
        self.model = model

    def embed(self, texts):
        FakeEmbed.calls.append(len(texts))
        # One token per input char, just to have a non-zero count.
        total_tokens = sum(len(t) for t in texts)
        vec_dim = 1536
        # Distinct vectors per text so we can spot accidental sharing.
        vectors = [[float(i % 256) / 256.0] * vec_dim for i, _ in enumerate(texts)]
        return vectors, total_tokens


class Command(BaseCommand):
    help = "Smoke-verify ai.tasks.reindex_source with a fake embedder."

    def handle(self, *args, **opts):
        failures: list[str] = []
        cleanup_workspace_ids: list = []
        cleanup_user_ids: list = []

        def check(label, got, want):
            if got != want:
                failures.append(f"FAIL  {label}: got={got!r} want={want!r}")
            else:
                print(f"ok    {label}")

        # Swap embedder.
        orig_embed_class = providers.OpenAIEmbed
        providers.OpenAIEmbed = FakeEmbed

        try:
            owner = User.objects.create(
                email=f"r+{uuid.uuid4().hex[:6]}@example.test",
                username=f"r-{uuid.uuid4().hex[:6]}",
                first_name="r-owner",
                is_password_autoset=True,
            )
            cleanup_user_ids.append(owner.id)

            ws = Workspace.objects.create(name="R WS", slug=SLUG, owner=owner)
            cleanup_workspace_ids.append(ws.id)

            prj = Project.objects.create(
                workspace=ws, name="R PRJ", identifier="RIDX", created_by=owner
            )
            WorkspaceAIConfig.objects.create(
                workspace=ws,
                enabled=True,
                openai_key="sk-fake-for-smoke",
                embed_model="text-embedding-3-small",
            )

            issue = Issue.objects.create(
                workspace=ws,
                project=prj,
                name="Add SSO",
                description_stripped=("Allow workspace admins to enable SAML SSO."),
                created_by=owner,
            )

            # ---- case 1: initial reindex -----------------------------
            FakeEmbed.calls = []
            n_usage_before = AIUsageLog.objects.count()
            reindex_source.apply(
                args=(str(ws.id), str(prj.id), "work_item", str(issue.id)), throw=True
            )
            chunks = list(
                DocumentChunk.objects.filter(source_type="work_item", source_id=issue.id)
            )
            check("initial: at least 1 chunk", len(chunks) >= 1, True)
            check("initial: embedding dim == 1536", len(chunks[0].embedding), 1536)
            check("initial: 1 embed call", len(FakeEmbed.calls), 1)
            check(
                "initial: 1 AIUsageLog feature=embed",
                AIUsageLog.objects.filter(
                    workspace=ws, feature=AIUsageLog.FEATURE_EMBED
                ).count(),
                n_usage_before + 1,
            )

            # ---- case 2: idempotency (re-run, no content change) -----
            FakeEmbed.calls = []
            n_chunks_before = DocumentChunk.objects.filter(source_id=issue.id).count()
            n_usage_before = AIUsageLog.objects.count()
            reindex_source.apply(
                args=(str(ws.id), str(prj.id), "work_item", str(issue.id)), throw=True
            )
            check("idempotent: 0 embed calls", FakeEmbed.calls, [])
            check(
                "idempotent: chunk count unchanged",
                DocumentChunk.objects.filter(source_id=issue.id).count(),
                n_chunks_before,
            )
            check(
                "idempotent: no new AIUsageLog",
                AIUsageLog.objects.count(),
                n_usage_before,
            )

            # ---- case 3: content change rebuilds ---------------------
            issue.name = "Add SSO (urgent)"
            issue.save(update_fields=["name"])
            FakeEmbed.calls = []
            n_usage_before = AIUsageLog.objects.count()
            reindex_source.apply(
                args=(str(ws.id), str(prj.id), "work_item", str(issue.id)), throw=True
            )
            check(
                "content change: chunks rebuilt (>=1 embed call)",
                len(FakeEmbed.calls) >= 1,
                True,
            )
            check(
                "content change: 1 new AIUsageLog",
                AIUsageLog.objects.count(),
                n_usage_before + 1,
            )
            new_chunks = list(
                DocumentChunk.objects.filter(source_id=issue.id)
            )
            check(
                "content change: hash differs",
                new_chunks[0].content_hash != chunks[0].content_hash,
                True,
            )

            # ---- case 4: delete_chunks removes all -------------------
            removed = delete_chunks(source_type="work_item", source_id=str(issue.id))
            check("delete_chunks: removed > 0", removed >= 1, True)
            check(
                "delete_chunks: row count is 0",
                DocumentChunk.objects.filter(source_id=issue.id).count(),
                0,
            )

            # ---- case 5: missing source -> drops chunks --------------
            ghost_id = uuid.uuid4()
            DocumentChunk.objects.create(
                workspace=ws,
                project=prj,
                source_type="work_item",
                source_id=ghost_id,
                chunk_index=0,
                content="stale",
                token_count=1,
                embedding=[0.0] * 1536,
                content_hash="x" * 64,
            )
            reindex_source.apply(
                args=(str(ws.id), str(prj.id), "work_item", str(ghost_id)), throw=True
            )
            check(
                "missing source: chunks removed",
                DocumentChunk.objects.filter(source_id=ghost_id).count(),
                0,
            )

        finally:
            providers.OpenAIEmbed = orig_embed_class
            DocumentChunk.objects.filter(workspace_id__in=cleanup_workspace_ids).delete()
            AIUsageLog.objects.filter(workspace_id__in=cleanup_workspace_ids).delete()
            Workspace.objects.filter(id__in=cleanup_workspace_ids).delete()
            User.objects.filter(id__in=cleanup_user_ids).delete()

        if failures:
            for f in failures:
                self.stdout.write(self.style.ERROR(f))
            raise SystemExit(1)
        self.stdout.write(self.style.SUCCESS("\nALL REINDEX ASSERTIONS PASSED"))
