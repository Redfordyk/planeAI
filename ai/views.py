"""DRF views for the AI add-on (TZ 1.8 and onward)."""

from __future__ import annotations

from django.apps import apps
from django.db.models import Count
from django.http import StreamingHttpResponse
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from ai.acl import ROLE
from ai.agent_loop import run_agent
from ai.models import (
    AIProjectSettings,
    AIUsageLog,
    DocumentChunk,
    IssueSummary,
    WorkspaceAIConfig,
)
from ai.prompts import (
    SEARCH_SYSTEM,
    SUMMARIZE_SYSTEM,
    build_search_messages,
    build_summarize_messages,
)
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


def _user_is_project_admin(user, workspace_id, project_id) -> bool:
    """True iff `user` is an active project ADMIN, or an active
    workspace ADMIN with any active project membership."""
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    ProjectMember = apps.get_model("db", "ProjectMember")
    WorkspaceMember = apps.get_model("db", "WorkspaceMember")
    if ProjectMember.objects.filter(
        member=user,
        workspace_id=workspace_id,
        project_id=project_id,
        is_active=True,
        deleted_at__isnull=True,
        role=ROLE.ADMIN.value,
    ).exists():
        return True
    is_ws_admin = WorkspaceMember.objects.filter(
        member=user,
        workspace_id=workspace_id,
        is_active=True,
        deleted_at__isnull=True,
        role=ROLE.ADMIN.value,
    ).exists()
    if not is_ws_admin:
        return False
    return ProjectMember.objects.filter(
        member=user,
        workspace_id=workspace_id,
        project_id=project_id,
        is_active=True,
        deleted_at__isnull=True,
    ).exists()


class ProjectAISettingsView(APIView):
    """`/api/ai/workspaces/<workspace_id>/projects/<project_id>/ai-settings/`.

    Per-project AI on/off switch. The toggle the frontend renders is
    the *inverse* of `exclude_from_ai`: `ai_enabled=True` ⇔
    `exclude_from_ai=False`. New projects have no row, which means
    `ai_enabled=True` (AI on by default per spec).

    - GET: any active workspace member.
    - PATCH: project admin or workspace admin (with any project role).
    """

    permission_classes = [IsAuthenticated]

    def _project_in_workspace(self, workspace_id, project_id) -> bool:
        Project = apps.get_model("db", "Project")
        return Project.objects.filter(
            id=project_id, workspace_id=workspace_id
        ).exists()

    def get(self, request, workspace_id, project_id):
        if not _is_workspace_member(request.user, workspace_id):
            return Response(
                {"error": "not a workspace member"},
                status=status.HTTP_403_FORBIDDEN,
            )
        if not self._project_in_workspace(workspace_id, project_id):
            return Response(
                {"error": "project not in workspace"},
                status=status.HTTP_404_NOT_FOUND,
            )
        row = AIProjectSettings.objects.filter(project_id=project_id).first()
        exclude = bool(row.exclude_from_ai) if row else False
        return Response(
            {
                "project_id": str(project_id),
                "workspace_id": str(workspace_id),
                "ai_enabled": not exclude,
                "exclude_from_ai": exclude,
            }
        )

    def patch(self, request, workspace_id, project_id):
        if not self._project_in_workspace(workspace_id, project_id):
            return Response(
                {"error": "project not in workspace"},
                status=status.HTTP_404_NOT_FOUND,
            )
        if not _user_is_project_admin(request.user, workspace_id, project_id):
            return Response(
                {"error": "admin role required to change project AI settings"},
                status=status.HTTP_403_FORBIDDEN,
            )

        data = request.data or {}
        if "ai_enabled" in data:
            exclude = not bool(data["ai_enabled"])
        elif "exclude_from_ai" in data:
            exclude = bool(data["exclude_from_ai"])
        else:
            return Response(
                {"error": "missing 'ai_enabled' (or 'exclude_from_ai')"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        row, _ = AIProjectSettings.objects.update_or_create(
            project_id=project_id,
            defaults={"exclude_from_ai": exclude},
        )
        return Response(
            {
                "project_id": str(project_id),
                "workspace_id": str(workspace_id),
                "ai_enabled": not row.exclude_from_ai,
                "exclude_from_ai": row.exclude_from_ai,
            }
        )


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

    def post(self, request, workspace_id):
        """Sync DRF view — even though the response body streams.

        Earlier this method was ``async def`` to take advantage of
        ASGI streaming under uvicorn, but DRF's APIView.dispatch in
        the version pinned by Plane does not await async handlers
        and returns the unhandled coroutine — the WSGI test client
        then fails with ``AssertionError: Expected a Response``. Going
        sync here keeps both the live ASGI stack AND the sync test
        client happy; the SSE generator itself uses an
        ``async_to_sync`` adapter at the bottom of the iterator so
        the OpenAI/Anthropic async clients still work.
        """
        query = (request.data.get("query") or "").strip()
        if not query:
            return Response(
                {"error": "query is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        top_k = int(request.data.get("top_k") or 20)
        user = request.user

        cfg = _load_cfg(workspace_id)
        if cfg is None:
            return Response(
                {"error": "AI disabled for this workspace"},
                status=status.HTTP_403_FORBIDDEN,
            )
        if not _is_workspace_member(user, workspace_id):
            return Response(
                {"error": "not a workspace member"},
                status=status.HTTP_403_FORBIDDEN,
            )
        used = tokens_used_this_month(workspace_id)
        if used >= cfg.monthly_token_budget:
            return Response(
                {
                    "error": "Monthly AI budget exceeded",
                    "used_tokens": used,
                    "budget_tokens": cfg.monthly_token_budget,
                },
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        chunks = retrieve(
            workspace_id=workspace_id, user=user, query=query, cfg=cfg, top_k=top_k,
        )
        sources = source_ids(chunks)
        context = build_context(chunks)
        messages = build_search_messages(context, query)

        async_gen = claude_sse(
            cfg=cfg, system=SEARCH_SYSTEM, messages=messages, sources=sources,
            workspace_id=workspace_id, user_id=user.id,
        )

        # Convert async generator -> sync iterator. Each `next()` call
        # blocks until the next SSE frame is ready; under uvicorn this
        # is run in a threadpool so the event loop keeps spinning.
        def sync_iter():
            from asgiref.sync import async_to_sync

            ait = async_gen.__aiter__()
            anext_call = async_to_sync(ait.__anext__)
            while True:
                try:
                    yield anext_call()
                except StopAsyncIteration:
                    return

        response = StreamingHttpResponse(
            sync_iter(), content_type="text/event-stream"
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


class SummarizeIssueView(APIView):
    """``POST /api/ai/workspaces/<workspace_id>/issues/<issue_id>/summarize/``.

    Streams an AI summary of one work item (title + description +
    every comment) as SSE. First frame is either
    ``{"cached": true, "summary": "...", "updated_at": "...", "model": "..."}``
    or ``{"cached": false}`` followed by ``{"delta": "..."}`` frames
    and a closing ``{"done": true, "usage": {...}}``.

    Pass ``{"force": true}`` in the body to bypass the content-hash
    cache (used by the UI "Regenerate" button).

    ACL: caller must be (a) a workspace member, (b) have read access
    to the project (any ProjectMember role), and (c) the project must
    not be flagged ``exclude_from_ai``. Standard AI budget gate.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request, workspace_id, issue_id):
        import hashlib
        import logging

        logger = logging.getLogger("plane.ai.summarize")

        force = bool((request.data or {}).get("force"))
        user = request.user

        # --- gates ----------------------------------------------------
        cfg = _load_cfg(workspace_id)
        if cfg is None:
            return Response(
                {"error": "AI disabled for this workspace"},
                status=status.HTTP_403_FORBIDDEN,
            )
        if not _is_workspace_member(user, workspace_id):
            return Response(
                {"error": "not a workspace member"},
                status=status.HTTP_403_FORBIDDEN,
            )
        used = tokens_used_this_month(workspace_id)
        if used >= cfg.monthly_token_budget:
            return Response(
                {
                    "error": "Monthly AI budget exceeded",
                    "used_tokens": used,
                    "budget_tokens": cfg.monthly_token_budget,
                },
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        # --- fetch the work item + access check via ProjectMember ----
        Issue = apps.get_model("db", "Issue")
        issue = (
            Issue.objects.filter(
                id=issue_id,
                workspace_id=workspace_id,
                deleted_at__isnull=True,
            )
            .only("id", "name", "description_stripped", "project_id", "workspace_id")
            .first()
        )
        if issue is None:
            return Response(
                {"error": "work item not found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        ProjectMember = apps.get_model("db", "ProjectMember")
        has_access = ProjectMember.objects.filter(
            member=user,
            project_id=issue.project_id,
            workspace_id=workspace_id,
            is_active=True,
            deleted_at__isnull=True,
        ).exists()
        if not has_access:
            return Response(
                {"error": "no access to this work item"},
                status=status.HTTP_403_FORBIDDEN,
            )

        # Project-level AI opt-out (TZ 3.4): respect exclude_from_ai.
        excluded = AIProjectSettings.objects.filter(
            project_id=issue.project_id, exclude_from_ai=True
        ).exists()
        if excluded:
            return Response(
                {"error": "AI is disabled for this project"},
                status=status.HTTP_403_FORBIDDEN,
            )

        # --- gather content ------------------------------------------
        IssueComment = apps.get_model("db", "IssueComment")
        comments_qs = (
            IssueComment.objects.filter(
                issue_id=issue_id,
                workspace_id=workspace_id,
                deleted_at__isnull=True,
            )
            .order_by("created_at")
            .only("id", "comment_stripped", "created_at")
        )
        comments: list[tuple[str, str]] = [
            (str(c.id), c.comment_stripped or "") for c in comments_qs
        ]

        title = issue.name or ""
        description = issue.description_stripped or ""

        # --- cache check (content hash) -------------------------------
        hasher = hashlib.sha256()
        hasher.update(title.encode("utf-8"))
        hasher.update(b"\x00")
        hasher.update(description.encode("utf-8"))
        for cid, text in comments:
            hasher.update(b"\x00")
            hasher.update(cid.encode("utf-8"))
            hasher.update(b"\x00")
            hasher.update((text or "").encode("utf-8"))
        content_hash = hasher.hexdigest()

        if not force:
            cached = (
                IssueSummary.objects.filter(issue_id=issue_id, content_hash=content_hash)
                .only("summary_text", "updated_at", "model_used")
                .first()
            )
            if cached is not None:
                def cached_iter():
                    yield _sse_frame(
                        {
                            "cached": True,
                            "summary": cached.summary_text,
                            "updated_at": cached.updated_at.isoformat(),
                            "model": cached.model_used,
                        }
                    )
                    yield _sse_frame({"done": True, "usage": {"input_tokens": 0, "output_tokens": 0}})

                response = StreamingHttpResponse(
                    cached_iter(), content_type="text/event-stream"
                )
                for header, value in sse_response_headers().items():
                    response[header] = value
                return response

        # Reject summarize requests when there's nothing to summarize.
        if not title and not description and not any(t for _, t in comments):
            return Response(
                {"error": "work item is empty — nothing to summarize"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # --- stream + persist on completion ---------------------------
        messages = build_summarize_messages(title, description, comments)
        # Trim absurdly large payloads to protect the LLM context.
        # 30k chars ~ 8k tokens, well under any chat model's limit.
        MAX_CHARS = 30_000
        if len(messages[0]["content"]) > MAX_CHARS:
            messages[0]["content"] = (
                messages[0]["content"][:MAX_CHARS]
                + "\n\n[…truncated for length…]"
            )

        async_gen = claude_sse(
            cfg=cfg,
            system=SUMMARIZE_SYSTEM,
            messages=messages,
            sources=[],
            workspace_id=workspace_id,
            user_id=user.id,
            feature=AIUsageLog.FEATURE_SUMMARIZE,
            # Modest cap — summary should be 5-8 sentences.
            max_tokens=600,
            temperature=0.2,
        )

        # Buffer the streamed deltas so we can persist the final
        # summary; mirrors SearchView's sync-iter pattern.
        collected_text: list[str] = []
        chosen_model_used: list[str] = [cfg.chat_model]
        final_usage_tokens: dict[str, int] = {"input_tokens": 0, "output_tokens": 0}

        def sync_iter():
            from asgiref.sync import async_to_sync
            import json as _json

            ait = async_gen.__aiter__()
            anext_call = async_to_sync(ait.__anext__)
            yield _sse_frame({"cached": False, "model": chosen_model_used[0]})
            while True:
                try:
                    raw = anext_call()
                except StopAsyncIteration:
                    break
                # raw is a string like "data: {...}\n\n" — parse it
                # back to capture deltas and the done frame.
                try:
                    body_line = raw.split("\n", 1)[0]
                    if body_line.startswith("data:"):
                        payload = _json.loads(body_line[len("data:"):].strip())
                        if "delta" in payload:
                            collected_text.append(payload["delta"])
                        elif "done" in payload and isinstance(payload.get("usage"), dict):
                            final_usage_tokens.update(
                                {
                                    "input_tokens": payload["usage"].get("input_tokens", 0),
                                    "output_tokens": payload["usage"].get("output_tokens", 0),
                                }
                            )
                except Exception:  # noqa: BLE001
                    pass
                yield raw

            # After the stream closes, persist the cache row.
            final_text = "".join(collected_text).strip()
            if final_text:
                try:
                    IssueSummary.objects.update_or_create(
                        issue_id=issue_id,
                        defaults={
                            "workspace_id": workspace_id,
                            "content_hash": content_hash,
                            "summary_text": final_text,
                            "model_used": chosen_model_used[0],
                            "input_tokens": final_usage_tokens["input_tokens"],
                            "output_tokens": final_usage_tokens["output_tokens"],
                        },
                    )
                except Exception:  # noqa: BLE001
                    logger.exception("summarize: failed to persist cache row")

        response = StreamingHttpResponse(
            sync_iter(), content_type="text/event-stream"
        )
        for header, value in sse_response_headers().items():
            response[header] = value
        return response


def _sse_frame(event: dict) -> str:
    """SSE frame formatter — duplicated locally so the view doesn't
    have to import a private helper from ai.streaming."""
    import json as _json

    return f"data: {_json.dumps(event, ensure_ascii=False)}\n\n"


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
