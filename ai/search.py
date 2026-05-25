"""Retrieval half of the RAG pipeline.

The single rule this module enforces (CLAUDE.md invariant 1): a
retrieval result for ``workspace_id`` must contain ONLY chunks whose
``workspace_id`` matches AND whose ``project_id`` is in the caller's
``allowed_projects(user, workspace_id)``. The ACL list is computed
server-side from the database — NEVER trusted from the request
payload, otherwise we have an IDOR across workspace borders.

Page chunks: ``db.Page`` has no FK to project (SCHEMA.md §db.Page),
so we store them with ``project_id=NULL``. They surface when the
user is a workspace member (any project membership inside the
workspace is enough to satisfy the read-only page access check).
A finer-grained per-project page filter via ``db.ProjectPage`` is
deferred to a later iteration — for retrieval purposes "any
workspace member can see workspace pages" matches Plane's own page
permission model.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from django.db.models import Q
from pgvector.django import CosineDistance

from ai import providers
from ai.acl import allowed_projects
from ai.models import AIUsageLog, DocumentChunk, WorkspaceAIConfig
from ai.usage import record_usage


logger = logging.getLogger("plane.ai.search")


DEFAULT_TOP_K = 20


@dataclass
class RetrievedChunk:
    """A typed wrapper so callers can't accidentally see internal
    DocumentChunk fields (embedding bytes etc.) when serialising."""

    id: str
    workspace_id: str
    project_id: str | None
    source_type: str
    source_id: str
    chunk_index: int
    content: str
    distance: float

    @classmethod
    def from_row(cls, row: DocumentChunk) -> "RetrievedChunk":
        return cls(
            id=str(row.id),
            workspace_id=str(row.workspace_id),
            project_id=str(row.project_id) if row.project_id else None,
            source_type=row.source_type,
            source_id=str(row.source_id),
            chunk_index=row.chunk_index,
            content=row.content,
            distance=float(getattr(row, "_distance", 0.0)),
        )


def retrieve(
    *,
    workspace_id,
    user,
    query: str,
    cfg: WorkspaceAIConfig | None = None,
    top_k: int = DEFAULT_TOP_K,
    record: bool = True,
) -> list[RetrievedChunk]:
    """Return the top-K chunks for `query` filtered by user's ACL.

    Args:
        workspace_id: Workspace UUID. Filter is always applied.
        user: Plane ``db.User`` instance. Anonymous/None returns [].
        query: Search text. Empty string returns [].
        cfg: Optional pre-fetched ``WorkspaceAIConfig`` — saves a query
            when the caller already loaded it (e.g. from
            ``require_ai_budget`` guard via ``request.ai_cfg``).
        top_k: Number of chunks. Caller controls — defaults to 20.
        record: When True (default), writes one ``AIUsageLog`` row for
            the embedding call (feature='embed', model=embed_model).
            Set False for tests / dry-runs.
    """
    if user is None or not getattr(user, "is_authenticated", False):
        return []
    if not query or not query.strip():
        return []

    if cfg is None:
        cfg = (
            WorkspaceAIConfig.objects.filter(
                workspace_id=workspace_id, enabled=True
            )
            .only("openai_key", "embed_model")
            .first()
        )
        if cfg is None or not cfg.openai_key:
            logger.info(
                "retrieve: workspace %s has no enabled AI config", workspace_id
            )
            return []

    allowed = allowed_projects(user, workspace_id)
    if not allowed:
        # No project memberships → no chunks visible. We still allow
        # page chunks (project_id IS NULL) on a workspace where the
        # user is a *workspace* member — checked below via the
        # WorkspaceMember filter. Returning early when allowed is
        # empty would lock workspace members out of pages, which
        # contradicts Plane's page permission model.
        from django.apps import apps as django_apps

        WorkspaceMember = django_apps.get_model("db", "WorkspaceMember")
        if not WorkspaceMember.objects.filter(
            member=user, workspace_id=workspace_id, is_active=True
        ).exists():
            return []

    embed_model = cfg.embed_model or providers.EMBED_MODEL
    embed = providers.OpenAIEmbed(api_key=cfg.openai_key, model=embed_model)
    vectors, tokens = embed.embed([query])
    qvec = vectors[0]

    if record:
        record_usage(
            workspace_id=workspace_id,
            user_id=user.id,
            feature=AIUsageLog.FEATURE_INTENT_SEARCH,
            model=embed_model,
            usage={"total_tokens": tokens},
        )

    # The ACL filter: workspace must match, AND either the chunk's
    # project is in `allowed`, OR the chunk is a page (project_id
    # NULL) — pages live at workspace level.
    rows = list(
        DocumentChunk.objects.filter(workspace_id=workspace_id)
        .filter(Q(project_id__in=allowed) | Q(project_id__isnull=True))
        .annotate(_distance=CosineDistance("embedding", qvec))
        .order_by("_distance")[:top_k]
    )
    return [RetrievedChunk.from_row(r) for r in rows]


def build_context(chunks: list[RetrievedChunk], *, max_chars: int = 12000) -> str:
    """Concatenate chunks for inclusion in a Claude prompt.

    Each chunk is tagged with ``[source_type:source_id]`` so the model
    can cite back. Truncates at ``max_chars`` so we don't blow past a
    100k token context unexpectedly — at ~4 chars/token, 12k chars is
    ~3k tokens, comfortably leaving room for the user query and
    system prompt.
    """
    out: list[str] = []
    total = 0
    for chunk in chunks:
        block = f"[{chunk.source_type}:{chunk.source_id}] {chunk.content}"
        if total + len(block) > max_chars and out:
            break
        out.append(block)
        total += len(block)
    return "\n\n".join(out)


def source_ids(chunks: list[RetrievedChunk]) -> list[dict]:
    """Distinct source pointers (one entry per source_id) preserving
    the order chunks appeared in retrieval. Suitable for "sources"
    sidebar in the UI.

    For SOURCE_PAGE chunks `project_id` is NULL (pages live at workspace
    level, see DocumentChunk model docstring). The UI needs project_id
    to build a deep link — Plane's only page route is
    `/<ws>/projects/<pid>/pages/<page_id>`. We backfill it here via the
    ``db.ProjectPage`` junction (one query batched by source_id).
    """
    seen: set[tuple[str, str]] = set()
    result: list[dict] = []
    page_ids: list[str] = []
    for c in chunks:
        key = (c.source_type, c.source_id)
        if key in seen:
            continue
        seen.add(key)
        result.append(
            {
                "source_type": c.source_type,
                "source_id": c.source_id,
                "project_id": c.project_id,
            }
        )
        if c.source_type == "page" and not c.project_id:
            page_ids.append(c.source_id)

    if page_ids:
        # Resolve first project for each page via ProjectPage join.
        try:
            from django.apps import apps
            ProjectPage = apps.get_model("db", "ProjectPage")
            mapping: dict[str, str] = {}
            for row in ProjectPage.objects.filter(page_id__in=page_ids).values(
                "page_id", "project_id"
            ):
                pid = str(row["page_id"])
                if pid not in mapping:
                    mapping[pid] = str(row["project_id"])
            for entry in result:
                if entry["source_type"] == "page" and not entry["project_id"]:
                    entry["project_id"] = mapping.get(entry["source_id"])
        except Exception:  # noqa: BLE001 — never fail the search response
            pass

    return result
