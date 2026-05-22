"""Bulk-enqueue ``reindex_source`` for every existing object in a workspace.

Used once when AI is first enabled on a workspace that already has
data (the runtime ingest hooks in TZ 1.4 only catch new writes).

Throttling: each task gets `countdown = n // rate` seconds, where
`n` is the task ordinal. With `--rate 3`, three tasks fire per second
target. This keeps OpenAI request rate well under the 1k req/min
ceiling for typical embedding workloads and avoids saturating the
Celery worker queue on cold start.

Re-runs are safe: ``reindex_source`` short-circuits when the content
hash already matches the stored chunk's hash. So if a backfill
crashes halfway, re-running picks up where it left off without
double-paying for embedding tokens.

DPA caveat (see [GDPR.md](../../../GDPR.md)): running this against
real customer data sends every issue/comment/page text to OpenAI.
On prod, this is gated behind the production backfill checklist
(TZ 6.6). On staging it's fine — synthetic data only.
"""

from __future__ import annotations

import logging
from typing import Iterable

from django.core.management.base import BaseCommand, CommandError
from django.db.models import QuerySet

from ai.models import AIProjectSettings, DocumentChunk, WorkspaceAIConfig
from ai.tasks import reindex_source


logger = logging.getLogger("plane.ai.backfill")


SOURCES = ("work_item", "comment", "page")


class Command(BaseCommand):
    help = "Enqueue ai.reindex_source for every Issue/IssueComment/Page in a workspace."

    def add_arguments(self, parser):
        parser.add_argument(
            "--workspace",
            required=True,
            help="Workspace UUID (the value of db.Workspace.id).",
        )
        parser.add_argument(
            "--rate",
            type=int,
            default=3,
            help="Target enqueue rate per second (tasks get countdown = n // rate).",
        )
        parser.add_argument(
            "--source",
            choices=SOURCES + ("all",),
            default="all",
            help="Limit to a single source type (default: all).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Count what would be enqueued; do not actually enqueue.",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Enqueue even if WorkspaceAIConfig.enabled is False.",
        )

    def handle(self, *args, **opts):
        from django.apps import apps as django_apps

        Issue = django_apps.get_model("db", "Issue")
        IssueComment = django_apps.get_model("db", "IssueComment")
        Page = django_apps.get_model("db", "Page")

        ws_id = opts["workspace"]
        rate = max(1, opts["rate"])
        only_source = opts["source"]
        dry_run = opts["dry_run"]
        force = opts["force"]

        # Workspace gate. The hot path (signals) checks this too, but
        # backfill is operator-driven — surface the misconfiguration
        # loudly instead of silently doing nothing.
        cfg = WorkspaceAIConfig.objects.filter(workspace_id=ws_id).first()
        if cfg is None:
            raise CommandError(
                f"workspace {ws_id} has no WorkspaceAIConfig row; create one (enabled=True) first"
            )
        if not cfg.enabled and not force:
            raise CommandError(
                f"workspace {ws_id} has WorkspaceAIConfig.enabled=False; "
                "pass --force to override (do not on prod)"
            )

        excluded_projects = set(
            AIProjectSettings.objects.filter(exclude_from_ai=True).values_list(
                "project_id", flat=True
            )
        )

        # `n` is the global counter across all source types so the
        # rate limit applies cumulatively, not per-source-batch.
        n = 0
        per_source: dict[str, int] = {s: 0 for s in SOURCES}

        def schedule(source_type: str, project_id, source_id) -> None:
            nonlocal n
            if dry_run:
                per_source[source_type] += 1
                n += 1
                return
            reindex_source.apply_async(
                args=[
                    str(ws_id),
                    str(project_id) if project_id else None,
                    source_type,
                    str(source_id),
                ],
                countdown=n // rate,
            )
            per_source[source_type] += 1
            n += 1
            if n % 200 == 0:
                self.stdout.write(f"  ... enqueued {n} so far")

        if only_source in ("work_item", "all"):
            qs: QuerySet = Issue.objects.filter(
                workspace_id=ws_id, deleted_at__isnull=True, is_draft=False
            ).only("id", "project_id")
            for issue in self._batched(qs):
                if issue.project_id in excluded_projects:
                    continue
                schedule("work_item", issue.project_id, issue.id)

        if only_source in ("comment", "all"):
            qs = IssueComment.objects.filter(
                workspace_id=ws_id, deleted_at__isnull=True
            ).only("id", "project_id")
            for comment in self._batched(qs):
                if comment.project_id in excluded_projects:
                    continue
                schedule("comment", comment.project_id, comment.id)

        if only_source in ("page", "all"):
            qs = Page.objects.filter(
                workspace_id=ws_id,
                deleted_at__isnull=True,
                archived_at__isnull=True,
            ).only("id")
            for page in self._batched(qs):
                # Page has no FK to project (SCHEMA.md). Multi-project
                # exclusion via ProjectPage is deferred to TZ 1.5
                # retrieval; backfill includes all non-archived pages.
                schedule("page", None, page.id)

        verb = "Would enqueue" if dry_run else "Enqueued"
        self.stdout.write(
            self.style.SUCCESS(
                f"{verb} {n} reindex tasks for workspace {ws_id} "
                f"(work_item={per_source['work_item']}, "
                f"comment={per_source['comment']}, "
                f"page={per_source['page']}, "
                f"rate={rate}/s)"
            )
        )

        # Hint: progress is visible via the index-status endpoint
        # (TZ 1.8) — count rows in ai_document_chunk vs work_items
        # in the workspace.
        if not dry_run:
            chunks = DocumentChunk.objects.filter(workspace_id=ws_id).count()
            self.stdout.write(f"current chunks for workspace: {chunks}")

    @staticmethod
    def _batched(qs: QuerySet, chunk_size: int = 200) -> Iterable:
        """Wrap `.iterator(chunk_size=...)` so server-side cursor stays
        small for big workspaces."""
        return qs.iterator(chunk_size=chunk_size)
