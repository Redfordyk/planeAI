"""Smoke-verify the index-status DRF endpoint against a live stack.

Install: docker cp scripts/verify_index_status.py \
    plane-ce-api-1:/code/plane/db/management/commands/verify_index_status.py

Run:     docker compose exec api python manage.py verify_index_status

Builds Workspace + 4 Issues (3 indexed) + 2 Comments (1 indexed) +
1 Page (0 indexed), plus a WorkspaceMember row for the test user,
then hits ``GET /api/ai/workspaces/<id>/index-status/`` via Django's
test client.
"""

from __future__ import annotations

import uuid

from django.core.management.base import BaseCommand
from django.test import Client
from django.urls import reverse

from ai.models import DocumentChunk
from plane.db.models import (
    Issue,
    IssueComment,
    Page,
    Project,
    User,
    Workspace,
    WorkspaceMember,
)


SLUG = f"idxst-smoke-{uuid.uuid4().hex[:8]}"


def _seed_chunks(workspace, project, source_type, source_id):
    """Two chunks per source — verifies distinct-source dedupe."""
    for i in range(2):
        DocumentChunk.objects.create(
            workspace=workspace,
            project=project,
            source_type=source_type,
            source_id=source_id,
            chunk_index=i,
            content="x",
            token_count=1,
            embedding=[0.0] * 1536,
            content_hash="h" * 64,
        )


class Command(BaseCommand):
    help = "Smoke-verify GET /api/ai/workspaces/<id>/index-status/."

    def handle(self, *args, **opts):
        failures: list[str] = []
        cleanup_workspace_ids: list = []
        cleanup_user_ids: list = []

        def check(label, got, want):
            if got != want:
                failures.append(f"FAIL  {label}: got={got!r} want={want!r}")
            else:
                print(f"ok    {label}")

        try:
            owner = User.objects.create(
                email=f"idx+{uuid.uuid4().hex[:6]}@example.test",
                username=f"idx-{uuid.uuid4().hex[:6]}",
                first_name="idx-owner",
                is_password_autoset=True,
            )
            cleanup_user_ids.append(owner.id)
            outsider = User.objects.create(
                email=f"out+{uuid.uuid4().hex[:6]}@example.test",
                username=f"out-{uuid.uuid4().hex[:6]}",
                first_name="outsider",
                is_password_autoset=True,
            )
            cleanup_user_ids.append(outsider.id)

            ws = Workspace.objects.create(name="IDX WS", slug=SLUG, owner=owner)
            cleanup_workspace_ids.append(ws.id)
            WorkspaceMember.objects.create(
                workspace=ws, member=owner, role=20, is_active=True
            )
            prj = Project.objects.create(
                workspace=ws, name="IDX", identifier="IDX", created_by=owner
            )

            issues = [
                Issue.objects.create(
                    workspace=ws, project=prj, name=f"I-{i}", created_by=owner
                )
                for i in range(4)
            ]
            # Index 3 of 4 issues:
            for issue in issues[:3]:
                _seed_chunks(ws, prj, "work_item", issue.id)

            # 2 comments; index 1
            base_issue = issues[0]
            comments = [
                IssueComment.objects.create(
                    workspace=ws,
                    project=prj,
                    issue=base_issue,
                    actor=owner,
                    comment_stripped=f"c-{i}",
                    comment_html=f"<p>c-{i}</p>",
                    created_by=owner,
                )
                for i in range(2)
            ]
            _seed_chunks(ws, prj, "comment", comments[0].id)

            # 1 page, none indexed
            Page.objects.create(
                workspace=ws,
                name="P",
                description_stripped="...",
                owned_by=owner,
                created_by=owner,
            )

            # ---- happy path ------------------------------------------
            client = Client()
            client.force_login(owner)
            url = reverse("ai:index-status", kwargs={"workspace_id": ws.id})
            resp = client.get(url)
            check("HTTP 200", resp.status_code, 200)
            body = resp.json()
            check("workspace_id matches", body["workspace_id"], str(ws.id))
            check("total = 4+2+1", body["total"], 7)
            check("indexed = 3+1+0", body["indexed"], 4)
            # coverage = 4/7 ≈ 0.57
            check("coverage = 0.57", body["coverage"], 0.57)
            check("ready=False (below 95%)", body["ready"], False)
            # By-source breakdown
            check(
                "by_source.work_item.indexed=3",
                body["by_source"]["work_item"]["indexed"],
                3,
            )
            check(
                "by_source.work_item.total=4",
                body["by_source"]["work_item"]["total"],
                4,
            )
            check(
                "by_source.comment.indexed=1",
                body["by_source"]["comment"]["indexed"],
                1,
            )
            check(
                "by_source.page.coverage=0.0",
                body["by_source"]["page"]["coverage"],
                0.0,
            )

            # ---- ACL: non-member gets 403 ---------------------------
            outsider_client = Client()
            outsider_client.force_login(outsider)
            resp = outsider_client.get(url)
            check("outsider HTTP 403", resp.status_code, 403)

            # ---- ACL: anonymous gets 401/403 ------------------------
            anon = Client()
            resp = anon.get(url)
            check("anonymous HTTP 401/403", resp.status_code in (401, 403), True)

            # ---- empty workspace: coverage=1.0, ready=True ----------
            empty_owner = User.objects.create(
                email=f"empty+{uuid.uuid4().hex[:6]}@example.test",
                username=f"empty-{uuid.uuid4().hex[:6]}",
                first_name="empty-owner",
                is_password_autoset=True,
            )
            cleanup_user_ids.append(empty_owner.id)
            empty_ws = Workspace.objects.create(
                name="empty", slug=f"empty-{uuid.uuid4().hex[:8]}", owner=empty_owner
            )
            cleanup_workspace_ids.append(empty_ws.id)
            WorkspaceMember.objects.create(
                workspace=empty_ws, member=empty_owner, role=20, is_active=True
            )
            empty_client = Client()
            empty_client.force_login(empty_owner)
            empty_url = reverse(
                "ai:index-status", kwargs={"workspace_id": empty_ws.id}
            )
            resp = empty_client.get(empty_url)
            body = resp.json()
            check("empty workspace: HTTP 200", resp.status_code, 200)
            check("empty: total=0", body["total"], 0)
            check("empty: coverage=1.0", body["coverage"], 1.0)
            check("empty: ready=True", body["ready"], True)

        finally:
            DocumentChunk.objects.filter(workspace_id__in=cleanup_workspace_ids).delete()
            Workspace.objects.filter(id__in=cleanup_workspace_ids).delete()
            User.objects.filter(id__in=cleanup_user_ids).delete()

        if failures:
            for f in failures:
                self.stdout.write(self.style.ERROR(f))
            raise SystemExit(1)
        self.stdout.write(self.style.SUCCESS("\nALL INDEX-STATUS ASSERTIONS PASSED"))
