"""DRF views for the AI add-on (TZ 1.8 and onward)."""

from __future__ import annotations

from django.apps import apps
from django.db.models import Count
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from ai.models import DocumentChunk


READY_THRESHOLD = 0.95  # >= 95% indexed counts as "ready" (frontend gate)


class IndexStatusView(APIView):
    """``GET /api/ai/workspaces/<workspace_id>/index-status/``.

    Returns indexing coverage so the frontend can:
      - gate the search UI until coverage crosses READY_THRESHOLD;
      - show a "340/512, 66%" progress indicator during backfill.

    Coverage is per source_type: a workspace might have all issues
    indexed but no pages — we want the frontend to surface that.

    ACL: read-only stat, requires the caller to be an active member
    of the workspace (any role, including GUEST).
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, workspace_id):
        if not _is_workspace_member(request.user, workspace_id):
            return Response(
                {"error": "not a workspace member"},
                status=status.HTTP_403_FORBIDDEN,
            )

        breakdown = _coverage_breakdown(workspace_id)

        total = sum(b["total"] for b in breakdown.values())
        indexed_total = sum(b["indexed"] for b in breakdown.values())
        coverage = (
            round(indexed_total / total, 2)
            if total
            else 1.0
        )
        ready = coverage >= READY_THRESHOLD if total else True

        return Response(
            {
                "workspace_id": str(workspace_id),
                "total": total,
                "indexed": indexed_total,
                "coverage": coverage,
                "ready": ready,
                "by_source": breakdown,
            }
        )


def _is_workspace_member(user, workspace_id) -> bool:
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    WorkspaceMember = apps.get_model("db", "WorkspaceMember")
    return WorkspaceMember.objects.filter(
        member=user,
        workspace_id=workspace_id,
        is_active=True,
        deleted_at__isnull=True,
    ).exists()


def _coverage_breakdown(workspace_id) -> dict[str, dict[str, int]]:
    """Per-source totals vs distinct indexed source_ids.

    `indexed` counts distinct source_ids — multiple chunks per source
    must not inflate the indexed count. `total` is the count of
    indexable rows in the workspace per source_type, mirroring the
    filters used by ai.management.commands.backfill_embeddings:
      - work_item: deleted_at IS NULL AND is_draft = false
      - comment:   deleted_at IS NULL
      - page:      deleted_at IS NULL AND archived_at IS NULL
    """
    Issue = apps.get_model("db", "Issue")
    IssueComment = apps.get_model("db", "IssueComment")
    Page = apps.get_model("db", "Page")

    totals = {
        DocumentChunk.SOURCE_WORK_ITEM: Issue.objects.filter(
            workspace_id=workspace_id, deleted_at__isnull=True, is_draft=False
        ).count(),
        DocumentChunk.SOURCE_COMMENT: IssueComment.objects.filter(
            workspace_id=workspace_id, deleted_at__isnull=True
        ).count(),
        DocumentChunk.SOURCE_PAGE: Page.objects.filter(
            workspace_id=workspace_id,
            deleted_at__isnull=True,
            archived_at__isnull=True,
        ).count(),
    }

    indexed_qs = (
        DocumentChunk.objects.filter(workspace_id=workspace_id)
        .values("source_type")
        .annotate(n_sources=Count("source_id", distinct=True))
    )
    indexed_by_type = {row["source_type"]: row["n_sources"] for row in indexed_qs}

    out: dict[str, dict[str, int]] = {}
    for source_type in (
        DocumentChunk.SOURCE_WORK_ITEM,
        DocumentChunk.SOURCE_COMMENT,
        DocumentChunk.SOURCE_PAGE,
    ):
        t = totals[source_type]
        i = indexed_by_type.get(source_type, 0)
        out[source_type] = {
            "total": t,
            "indexed": i,
            "coverage": round(i / t, 2) if t else 1.0,
        }
    return out
