"""DRF views for the AI add-on (TZ 1.8 and onward)."""

from __future__ import annotations

from asgiref.sync import sync_to_async
from django.apps import apps
from django.db.models import Count
from django.http import StreamingHttpResponse
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from ai.agent_loop import run_agent
from ai.models import DocumentChunk, WorkspaceAIConfig
from ai.prompts import SEARCH_SYSTEM, build_search_messages
from ai.search import build_context, retrieve, source_ids
from ai.streaming import claude_sse, sse_response_headers
from ai.transcribe import transcribe_audio
from ai.usage import tokens_used_this_month


def _user_can_use_ai(user, workspace_id) -> tuple[bool, str | None, WorkspaceAIConfig | None, int]:
    """Combined gate: workspace member + AI enabled + budget OK.

    Returns ``(ok, error_message, cfg, http_status)``. cfg is non-None
    when ok=True.
    """
    if not _is_workspace_member(user, workspace_id):
        return False, "not a workspace member", None, status.HTTP_403_FORBIDDEN
    cfg = (
        WorkspaceAIConfig.objects.filter(workspace_id=workspace_id, enabled=True)
        .only("anthropic_key", "openai_key", "chat_model", "embed_model", "monthly_token_budget")
        .first()
    )
    if cfg is None:
        return False, "AI disabled for this workspace", None, status.HTTP_403_FORBIDDEN
    used = tokens_used_this_month(workspace_id)
    if used >= cfg.monthly_token_budget:
        return False, "Monthly AI budget exceeded", None, status.HTTP_429_TOO_MANY_REQUESTS
    return True, None, cfg, 200


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


class SearchView(APIView):
    """``POST /api/ai/workspaces/<workspace_id>/search/``.

    Streams the RAG answer as Server-Sent Events. Request body:

      {"query": "<text>", "top_k": 20}

    Response: ``text/event-stream`` with frames as documented in
    ``ai.streaming.claude_sse``. The view itself is ``async`` because
    StreamingHttpResponse(async_gen, ...) only works that way under
    Django + uvicorn (TZ 0.3 / STREAMING.md).
    """

    permission_classes = [IsAuthenticated]

    async def post(self, request, workspace_id):
        query = (request.data.get("query") or "").strip()
        if not query:
            return Response(
                {"error": "query is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        top_k = int(request.data.get("top_k") or 20)
        user = request.user

        # Pre-flight: workspace member, AI enabled, budget OK. All
        # inline-async-safe via sync_to_async wrappers — keeps the
        # 403/429 path returning a regular DRF Response BEFORE we
        # start the SSE stream (clients distinguish HTTP errors from
        # SSE error frames).
        cfg = await sync_to_async(_load_cfg, thread_sensitive=False)(workspace_id)
        if cfg is None:
            return Response(
                {"error": "AI disabled for this workspace"},
                status=status.HTTP_403_FORBIDDEN,
            )

        is_member = await sync_to_async(_is_workspace_member, thread_sensitive=False)(
            user, workspace_id
        )
        if not is_member:
            return Response(
                {"error": "not a workspace member"},
                status=status.HTTP_403_FORBIDDEN,
            )

        used = await sync_to_async(tokens_used_this_month, thread_sensitive=False)(
            workspace_id
        )
        if used >= cfg.monthly_token_budget:
            return Response(
                {
                    "error": "Monthly AI budget exceeded",
                    "used_tokens": used,
                    "budget_tokens": cfg.monthly_token_budget,
                },
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        # Retrieval (sync ORM + sync OpenAI embed call) — must
        # finish before we start streaming so the `sources` frame
        # can be emitted first.
        chunks = await sync_to_async(retrieve, thread_sensitive=False)(
            workspace_id=workspace_id,
            user=user,
            query=query,
            cfg=cfg,
            top_k=top_k,
        )
        sources = source_ids(chunks)
        context = build_context(chunks)
        messages = build_search_messages(context, query)

        gen = claude_sse(
            cfg=cfg,
            system=SEARCH_SYSTEM,
            messages=messages,
            sources=sources,
            workspace_id=workspace_id,
            user_id=user.id,
        )
        response = StreamingHttpResponse(
            gen, content_type="text/event-stream"
        )
        for header, value in sse_response_headers().items():
            response[header] = value
        return response


def _load_cfg(workspace_id):
    return (
        WorkspaceAIConfig.objects.filter(
            workspace_id=workspace_id, enabled=True
        )
        .only("anthropic_key", "openai_key", "chat_model", "embed_model", "monthly_token_budget")
        .first()
    )


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


# ---------- Voice transcription -------------------------------------------


class TranscribeView(APIView):
    """``POST /api/ai/workspaces/<workspace_id>/transcribe/``.

    Multipart-form upload with field ``audio`` containing the raw
    audio blob (webm/opus from MediaRecorder is fine). Returns
    ``{"text": "..."}``. Uses the workspace's stored OpenAI key.
    """

    permission_classes = [IsAuthenticated]
    # Allow up to 25 MB — Whisper's hard limit.
    parser_classes = [__import__("rest_framework").parsers.MultiPartParser]

    def post(self, request, workspace_id):
        ok, err, cfg, code = _user_can_use_ai(request.user, workspace_id)
        if not ok:
            return Response({"error": err}, status=code)
        if not cfg.openai_key:
            return Response(
                {"error": "transcription requires an OpenAI key in WorkspaceAIConfig"},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        upload = request.FILES.get("audio")
        if upload is None:
            return Response(
                {"error": "field 'audio' is required (multipart upload)"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if upload.size and upload.size > 25 * 1024 * 1024:
            return Response(
                {"error": "audio too large (Whisper limit is 25 MB)"},
                status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            )
        language = request.data.get("language") or "ru"

        try:
            text = transcribe_audio(
                api_key=cfg.openai_key,
                audio_bytes=upload.read(),
                filename=upload.name or "audio.webm",
                language=language,
            )
        except Exception as e:  # noqa: BLE001
            return Response(
                {"error": f"transcription failed: {type(e).__name__}: {e}"},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        return Response({"text": text})


# ---------- Agent (tool-use loop) -----------------------------------------


class AgentExecuteView(APIView):
    """``POST /api/ai/workspaces/<workspace_id>/agent/execute/``.

    Body: ``{"prompt": "..."}``. Runs the tool-use loop
    (``ai.agent_loop.run_agent``) which can call create_project,
    create_issue, list_projects, list_members on the user's behalf.
    Returns the agent's final reply plus an ``actions`` log of every
    tool call (with arguments + result) for the UI to render.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request, workspace_id):
        ok, err, cfg, code = _user_can_use_ai(request.user, workspace_id)
        if not ok:
            return Response({"error": err}, status=code)
        prompt = (request.data.get("prompt") or "").strip()
        if not prompt:
            return Response(
                {"error": "prompt is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not cfg.anthropic_key:
            return Response(
                {"error": "agent requires a chat API key in WorkspaceAIConfig"},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        try:
            result = run_agent(
                user=request.user,
                workspace_id=workspace_id,
                prompt=prompt,
                cfg=cfg,
            )
        except Exception as e:  # noqa: BLE001
            return Response(
                {"error": f"agent failed: {type(e).__name__}: {e}"},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        return Response(result)
