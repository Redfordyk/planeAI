"""Smoke-verify ai.signals against live Plane models.

Install: docker cp scripts/verify_signals.py \
    plane-ce-api-1:/code/plane/db/management/commands/verify_signals.py

Run:     docker compose exec api python manage.py verify_signals

Each scenario runs in its own atomic block so `transaction.on_commit`
actually fires (on_commit only triggers when the *outermost*
transaction commits — a wrapping outer atomic would suppress the
callbacks). At the end we delete the fixtures explicitly.

Apply_async / delay calls are captured via monkey-patch so we do not
need a live Celery worker.
"""

from __future__ import annotations

import uuid

from django.core.cache import cache
from django.core.management.base import BaseCommand
from django.db import transaction

from ai import tasks
from ai.models import AIProjectSettings, DocumentChunk, WorkspaceAIConfig
from plane.db.models import Issue, IssueComment, Project, User, Workspace


SLUG = f"sig-smoke-{uuid.uuid4().hex[:8]}"


class Recorder:
    """Captures apply_async / delay invocations on ai.tasks.*."""

    def __init__(self):
        self.reindex_calls: list[tuple] = []
        self.delete_calls: list[tuple] = []

    def install(self):
        self._orig_apply = tasks.reindex_source.apply_async
        self._orig_delay = tasks.delete_chunks.delay

        def fake_apply(args=None, kwargs=None, **kw):
            self.reindex_calls.append(tuple(args or ()))

        def fake_delay(*args, **kwargs):
            self.delete_calls.append(tuple(args))

        tasks.reindex_source.apply_async = fake_apply
        tasks.delete_chunks.delay = fake_delay

    def restore(self):
        tasks.reindex_source.apply_async = self._orig_apply
        tasks.delete_chunks.delay = self._orig_delay

    def reset(self):
        self.reindex_calls.clear()
        self.delete_calls.clear()


def _clear_debounce():
    """Wipe per-source debounce keys between cases."""
    # django-redis exposes delete_pattern; standard cache does not. We
    # don't care if it's missing — just no-op.
    if hasattr(cache, "delete_pattern"):
        cache.delete_pattern("ai:reindex_pending:*")


def _make_user(handle: str) -> User:
    return User.objects.create(
        email=f"{handle}+{uuid.uuid4().hex[:6]}@example.test",
        username=f"{handle}-{uuid.uuid4().hex[:6]}",
        first_name=handle,
        is_password_autoset=True,
    )


class Command(BaseCommand):
    help = "Smoke-verify ai.signals on Issue / IssueComment."

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
            # ---- fixtures (each in its own committed transaction) -------
            with transaction.atomic():
                owner = _make_user("owner")
                cleanup_user_ids.append(owner.id)
                ws = Workspace.objects.create(name="Sig WS", slug=SLUG, owner=owner)
                cleanup_workspace_ids.append(ws.id)
                prj = Project.objects.create(
                    workspace=ws, name="Sig PRJ", identifier="SIG", created_by=owner
                )
                WorkspaceAIConfig.objects.create(workspace=ws, enabled=True)

            # ---- case 1: enabled + included -> exactly 1 task -----------
            _clear_debounce()
            rec.reset()
            with transaction.atomic():
                issue = Issue.objects.create(
                    workspace=ws, project=prj, name="A", created_by=owner
                )
                # Inside the atomic block, on_commit hasn't fired yet.
                check("during txn: no task yet", rec.reindex_calls, [])
            # Outer commit: on_commit callbacks fire here.
            check("after commit: 1 task", len(rec.reindex_calls), 1)
            if rec.reindex_calls:
                args = rec.reindex_calls[0]
                check("task args: source_type=work_item", args[2], "work_item")
                check("task args: source_id matches issue", args[3], str(issue.id))

            # ---- case 2: rollback -> no task ----------------------------
            _clear_debounce()
            rec.reset()
            try:
                with transaction.atomic():
                    Issue.objects.create(
                        workspace=ws, project=prj, name="rolled-back", created_by=owner
                    )
                    raise RuntimeError("simulate failure")
            except RuntimeError:
                pass
            check("rollback: 0 tasks", rec.reindex_calls, [])

            # ---- case 3: 5 rapid saves -> 1 task (debounce) -------------
            _clear_debounce()
            rec.reset()
            with transaction.atomic():
                issue3 = Issue.objects.create(
                    workspace=ws, project=prj, name="rapid", created_by=owner
                )
                for i in range(4):
                    issue3.name = f"rapid-{i}"
                    issue3.save(update_fields=["name"])
            check("5 saves -> 1 task (debounce)", len(rec.reindex_calls), 1)

            # ---- case 4: disabled workspace -> no task ------------------
            _clear_debounce()
            rec.reset()
            WorkspaceAIConfig.objects.filter(workspace=ws).update(enabled=False)
            with transaction.atomic():
                Issue.objects.create(
                    workspace=ws, project=prj, name="off", created_by=owner
                )
            check("disabled workspace: 0 tasks", rec.reindex_calls, [])
            WorkspaceAIConfig.objects.filter(workspace=ws).update(enabled=True)

            # ---- case 5: project excluded -> no task --------------------
            _clear_debounce()
            rec.reset()
            AIProjectSettings.objects.create(project=prj, exclude_from_ai=True)
            with transaction.atomic():
                Issue.objects.create(
                    workspace=ws, project=prj, name="excluded", created_by=owner
                )
            check("excluded project: 0 tasks", rec.reindex_calls, [])
            AIProjectSettings.objects.filter(project=prj).delete()

            # ---- case 6: draft issue -> no task -------------------------
            _clear_debounce()
            rec.reset()
            with transaction.atomic():
                Issue.objects.create(
                    workspace=ws,
                    project=prj,
                    name="draft",
                    created_by=owner,
                    is_draft=True,
                )
            check("draft issue: 0 tasks", rec.reindex_calls, [])

            # ---- case 7: IssueComment fires comment task ----------------
            _clear_debounce()
            rec.reset()
            with transaction.atomic():
                base_issue = Issue.objects.create(
                    workspace=ws, project=prj, name="for-comment", created_by=owner
                )
            _clear_debounce()
            rec.reset()
            with transaction.atomic():
                comment = IssueComment.objects.create(
                    workspace=ws,
                    project=prj,
                    issue=base_issue,
                    actor=owner,
                    comment_stripped="hello",
                    comment_html="<p>hello</p>",
                    created_by=owner,
                )
            check("comment save: 1 task", len(rec.reindex_calls), 1)
            if rec.reindex_calls:
                check("comment args: source_type=comment", rec.reindex_calls[0][2], "comment")
                check("comment args: source_id", rec.reindex_calls[0][3], str(comment.id))

            # ---- case 8: delete -> delete_chunks called (sync) ----------
            rec.reset()
            comment.delete()
            check("comment delete: 1 delete_chunks call", len(rec.delete_calls), 1)
            if rec.delete_calls:
                check("delete_chunks[0]: 'comment'", rec.delete_calls[0][0], "comment")

        finally:
            rec.restore()
            # Belt-and-braces cleanup. Cascade delete handles members,
            # projects, issues, comments under the workspace.
            DocumentChunk.objects.filter(workspace_id__in=cleanup_workspace_ids).delete()
            Workspace.objects.filter(id__in=cleanup_workspace_ids).delete()
            User.objects.filter(id__in=cleanup_user_ids).delete()

        if failures:
            for f in failures:
                self.stdout.write(self.style.ERROR(f))
            raise SystemExit(1)

        self.stdout.write(self.style.SUCCESS("\nALL SIGNAL ASSERTIONS PASSED"))
