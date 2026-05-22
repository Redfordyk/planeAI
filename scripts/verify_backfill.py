"""Smoke-verify ai.management.commands.backfill_embeddings.

Install: docker cp scripts/verify_backfill.py \
    plane-ce-api-1:/code/plane/db/management/commands/verify_backfill.py

Run:     docker compose exec api python manage.py verify_backfill

Builds Workspace + 2 Projects (one excluded via AIProjectSettings) +
4 Issues / 2 Comments / 2 Pages, then runs backfill_embeddings in
dry-run mode and asserts the right counts per source type.
"""

from __future__ import annotations

import uuid

from django.core.management import call_command
from django.core.management.base import BaseCommand
from io import StringIO

from ai import tasks
from ai.models import AIProjectSettings, DocumentChunk, WorkspaceAIConfig
from plane.db.models import Issue, IssueComment, Page, Project, User, Workspace


SLUG = f"bf-smoke-{uuid.uuid4().hex[:8]}"


class Recorder:
    def __init__(self):
        self.calls: list[tuple] = []

    def install(self):
        self._orig = tasks.reindex_source.apply_async

        def fake(args=None, kwargs=None, **kw):
            self.calls.append((tuple(args or ()), kw.get("countdown")))

        tasks.reindex_source.apply_async = fake

    def restore(self):
        tasks.reindex_source.apply_async = self._orig


class Command(BaseCommand):
    help = "Smoke-verify backfill_embeddings management command."

    def handle(self, *args, **opts):
        failures: list[str] = []
        cleanup_workspace_ids: list = []
        cleanup_user_ids: list = []

        def check(label, got, want):
            if got != want:
                failures.append(f"FAIL  {label}: got={got!r} want={want!r}")
            else:
                print(f"ok    {label}")

        rec = Recorder()
        rec.install()

        try:
            owner = User.objects.create(
                email=f"bf+{uuid.uuid4().hex[:6]}@example.test",
                username=f"bf-{uuid.uuid4().hex[:6]}",
                first_name="bf-owner",
                is_password_autoset=True,
            )
            cleanup_user_ids.append(owner.id)

            ws = Workspace.objects.create(name="BF WS", slug=SLUG, owner=owner)
            cleanup_workspace_ids.append(ws.id)

            prj_a = Project.objects.create(
                workspace=ws, name="A", identifier="BFA", created_by=owner
            )
            prj_b = Project.objects.create(
                workspace=ws, name="B (excluded)", identifier="BFB", created_by=owner
            )
            AIProjectSettings.objects.create(project=prj_b, exclude_from_ai=True)
            WorkspaceAIConfig.objects.create(workspace=ws, enabled=True)

            # 3 indexable issues in prj_a + 1 in prj_b (excluded)
            issues_a = [
                Issue.objects.create(
                    workspace=ws, project=prj_a, name=f"A#{i}", created_by=owner
                )
                for i in range(3)
            ]
            Issue.objects.create(
                workspace=ws, project=prj_b, name="B#0", created_by=owner
            )
            # 1 draft (skip)
            Issue.objects.create(
                workspace=ws,
                project=prj_a,
                name="draft",
                created_by=owner,
                is_draft=True,
            )
            # 1 comment on first prj_a issue + 1 on excluded
            IssueComment.objects.create(
                workspace=ws,
                project=prj_a,
                issue=issues_a[0],
                actor=owner,
                comment_stripped="ok",
                comment_html="<p>ok</p>",
                created_by=owner,
            )
            IssueComment.objects.create(
                workspace=ws,
                project=prj_b,
                issue=Issue.objects.create(
                    workspace=ws, project=prj_b, name="B#1", created_by=owner
                ),
                actor=owner,
                comment_stripped="excluded",
                comment_html="<p>excluded</p>",
                created_by=owner,
            )
            # 2 pages (workspace-level)
            Page.objects.create(
                workspace=ws,
                name="Page 1",
                description_stripped="hello",
                owned_by=owner,
                created_by=owner,
            )
            Page.objects.create(
                workspace=ws,
                name="Page 2",
                description_stripped="world",
                owned_by=owner,
                created_by=owner,
            )

            # ---- case 1: dry-run -------------------------------------
            rec.calls.clear()
            buf = StringIO()
            call_command(
                "backfill_embeddings",
                workspace=str(ws.id),
                rate=3,
                dry_run=True,
                stdout=buf,
            )
            check("dry-run: 0 apply_async calls", len(rec.calls), 0)
            out = buf.getvalue()
            # Expected counts: 3 work_items (excluded + draft skipped),
            # 1 comment (excluded skipped), 2 pages -> 6 total.
            check("dry-run: stdout contains 'Would enqueue 6'", "Would enqueue 6" in out, True)
            check("dry-run: work_item=3", "work_item=3" in out, True)
            check("dry-run: comment=1", "comment=1" in out, True)
            check("dry-run: page=2", "page=2" in out, True)

            # ---- case 2: real run, capture apply_async ---------------
            rec.calls.clear()
            buf = StringIO()
            call_command(
                "backfill_embeddings",
                workspace=str(ws.id),
                rate=3,
                stdout=buf,
            )
            check("real run: 6 apply_async calls", len(rec.calls), 6)
            # Group by source_type
            by_source: dict[str, int] = {}
            for (args, _countdown) in rec.calls:
                by_source[args[2]] = by_source.get(args[2], 0) + 1
            check("real run: 3 work_item tasks", by_source.get("work_item"), 3)
            check("real run: 1 comment task", by_source.get("comment"), 1)
            check("real run: 2 page tasks", by_source.get("page"), 2)
            # Countdown is monotonic non-decreasing.
            countdowns = [c for _, c in rec.calls]
            check("real run: countdown non-decreasing", countdowns == sorted(countdowns), True)
            # With rate=3 and 6 tasks, last countdown is (6-1)//3 = 1.
            check("real run: last countdown = 1", countdowns[-1], 1)
            # No task points at the excluded project's issues/comments.
            excluded_ids = set(
                str(i.id)
                for i in Issue.objects.filter(workspace=ws, project=prj_b)
            ) | {
                str(c.id)
                for c in IssueComment.objects.filter(workspace=ws, project=prj_b)
            }
            scheduled_ids = {args[3] for args, _ in rec.calls}
            check(
                "real run: no excluded source ids scheduled",
                scheduled_ids & excluded_ids,
                set(),
            )

            # ---- case 3: --source=work_item only ---------------------
            rec.calls.clear()
            call_command(
                "backfill_embeddings",
                workspace=str(ws.id),
                rate=3,
                source="work_item",
                dry_run=True,
                stdout=StringIO(),
            )
            # Same 3 work items.
            check("source=work_item only: 0 apply_async (dry-run)", len(rec.calls), 0)
        finally:
            rec.restore()
            DocumentChunk.objects.filter(workspace_id__in=cleanup_workspace_ids).delete()
            Workspace.objects.filter(id__in=cleanup_workspace_ids).delete()
            User.objects.filter(id__in=cleanup_user_ids).delete()

        if failures:
            for f in failures:
                self.stdout.write(self.style.ERROR(f))
            raise SystemExit(1)
        self.stdout.write(self.style.SUCCESS("\nALL BACKFILL ASSERTIONS PASSED"))
