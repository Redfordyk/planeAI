"""Celery task entry points for the AI pipeline."""

from __future__ import annotations

import hashlib
import logging

from celery import shared_task
from django.db import transaction

from ai import providers
from ai.chunking import chunk_text
from ai.loaders import load_source_text
from ai.models import AIUsageLog, DocumentChunk, WorkspaceAIConfig
from ai.usage import record_usage


logger = logging.getLogger("plane.ai.tasks")


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@shared_task(
    name="ai.reindex_source",
    bind=True,
    max_retries=3,
    default_retry_delay=60,
    acks_late=False,
)
def reindex_source(self, workspace_id, project_id, source_type, source_id):
    """Compute or refresh embeddings for one source object.

    Pipeline:

      1. Resolve text via `load_source_text(source_type, source_id)`.
         If `None` (deleted / soft-deleted / archived / empty), drop
         any existing chunks for this source and return.
      2. Hash the text; bail if existing chunks already match the
         hash. Cheap idempotency — saves OpenAI tokens on a re-fire
         of the same content.
      3. Chunk into ~400-token windows with 50-token overlap.
      4. Embed via OpenAI (batched, retried inside OpenAIEmbed).
      5. In one transaction: delete old chunks, bulk_create new ones,
         log usage.

    Retries: any provider failure raises `self.retry(exc=exc)`, which
    delays 60s exponentially (Celery default) up to 3 attempts. The
    embed batch itself has its own internal retry; this Celery-level
    retry only catches outright provider outages.
    """
    if not workspace_id or not source_id:
        return

    loaded = load_source_text(source_type, str(source_id))
    if loaded is None:
        DocumentChunk.objects.filter(
            source_type=source_type, source_id=source_id
        ).delete()
        logger.info(
            "reindex_source: source missing -> dropped chunks (source=%s/%s)",
            source_type,
            source_id,
        )
        return

    text, meta = loaded
    # `meta` carries the canonical workspace_id / project_id derived
    # from the loaded model, in case the signal-time values are stale
    # (rare, but defensive). Prefer loaded values.
    ws_id = meta.get("workspace_id") or workspace_id
    prj_id = meta.get("project_id")  # may be None for pages
    if prj_id is None and project_id is not None and source_type != "page":
        prj_id = project_id

    h = _content_hash(text)
    existing = DocumentChunk.objects.filter(
        source_type=source_type, source_id=source_id
    )
    if existing.exists() and existing.first().content_hash == h:
        logger.debug(
            "reindex_source: content unchanged, skip (source=%s/%s)",
            source_type,
            source_id,
        )
        return

    chunks = chunk_text(text)
    if not chunks:
        existing.delete()
        return

    cfg = (
        WorkspaceAIConfig.objects.filter(workspace_id=ws_id, enabled=True)
        .only("openai_key", "embed_model")
        .first()
    )
    if cfg is None or not cfg.openai_key:
        logger.warning(
            "reindex_source: workspace %s has no enabled AI config or no openai_key",
            ws_id,
        )
        return

    embed_model = cfg.embed_model or providers.EMBED_MODEL
    embed = providers.OpenAIEmbed(api_key=cfg.openai_key, model=embed_model)

    try:
        vectors, tokens = embed.embed([c for c, _ in chunks])
    except Exception as exc:
        logger.warning(
            "reindex_source: provider error, retrying via celery (source=%s/%s): %s",
            source_type,
            source_id,
            type(exc).__name__,
        )
        raise self.retry(exc=exc)

    # Sanity check: one vector per chunk.
    if len(vectors) != len(chunks):
        logger.error(
            "reindex_source: vector/chunk count mismatch %d vs %d (source=%s/%s)",
            len(vectors),
            len(chunks),
            source_type,
            source_id,
        )
        return

    rows = [
        DocumentChunk(
            workspace_id=ws_id,
            project_id=prj_id,
            source_type=source_type,
            source_id=source_id,
            chunk_index=i,
            content=text_part,
            token_count=token_count,
            embedding=vec,
            content_hash=h,
        )
        for i, ((text_part, token_count), vec) in enumerate(zip(chunks, vectors))
    ]

    with transaction.atomic():
        # Two-phase replace inside one transaction keeps retrieval
        # consistent: we never see a mix of old + new chunks.
        existing.delete()
        DocumentChunk.objects.bulk_create(rows)
        record_usage(
            workspace_id=ws_id,
            user_id=None,
            feature=AIUsageLog.FEATURE_EMBED,
            model=embed_model,
            usage={"total_tokens": tokens},
        )

    logger.info(
        "reindex_source: source=%s/%s chunks=%d tokens=%d",
        source_type,
        source_id,
        len(rows),
        tokens,
    )


@shared_task(
    name="ai.run_agent_on_workitem",
    bind=True,
    max_retries=2,
    default_retry_delay=30,
    acks_late=False,
)
def run_agent_on_workitem(self, issue_id):
    """Celery entry point for the agent loop (TZ 5.1 trigger → 5.2 body).

    The wrapper is intentionally thin: all logic — RAG retrieval,
    white-listed tool dispatch, scope enforcement, audit logging —
    lives in :mod:`ai.agent_worker`. Keeping the Celery decorator
    here (and the worker import deferred) avoids dragging the agent
    module's import surface into ``ai.signals`` at app-ready time.

    Retries: any handler exception inside ``run_agent_body`` is
    already swallowed into an ``AIAgentActionLog`` row with
    ``status='error'``, so a normal run never raises. We still leave
    ``max_retries=2`` for genuine infrastructure failures (DB blip,
    Anthropic outage) that bubble up before we reach the per-action
    handler.
    """
    from ai.agent_worker import run_agent_body

    result = run_agent_body(issue_id)
    logger.info("run_agent_on_workitem: issue=%s result=%s", issue_id, result)
    return result


@shared_task(
    name="ai.orchestrator_handle_event",
    bind=True,
    max_retries=2,
    default_retry_delay=15,
    acks_late=False,
)
def orchestrator_handle_event(self, event_dict: dict) -> dict:
    """Phase 11.1 — entry point for the multi-agent router.

    Thin Celery wrapper: import the router lazily (so a missing
    module doesn't break signal connection at app-ready time), call
    ``handle_event``, return its summary dict. Errors are caught
    inside ``handle_event`` — this wrapper only retries on genuine
    infra issues (DB blip).
    """
    from ai.orchestrator.router import handle_event
    return handle_event(event_dict)


@shared_task(name="ai.planner_decompose_goal", bind=True, max_retries=1)
def planner_decompose_goal(self, goal_id: str) -> dict:
    """Phase 7.3 — Celery wrapper around PLANNER.decompose_goal.

    Used by the goal-create endpoint when ``async_planning=True`` is
    requested; the synchronous path runs PLANNER inline."""
    from ai.models import ProjectGoal, WorkspaceAIConfig
    from ai.orchestrator import planner
    goal = ProjectGoal.objects.filter(id=goal_id).first()
    if goal is None:
        return {"status": "skipped", "reason": "goal_missing"}
    cfg = WorkspaceAIConfig.objects.filter(
        workspace_id=goal.workspace_id, enabled=True
    ).first()
    if cfg is None or not cfg.anthropic_key:
        return {"status": "skipped", "reason": "no_ai_config"}
    plan, action = planner.decompose_goal(goal=goal, cfg=cfg)
    return {
        "status": "ok",
        "task_count": plan.get("task_count", 0),
        "action_id": str(action.id),
    }


@shared_task(name="ai.delete_chunks")
def delete_chunks(source_type: str, source_id: str) -> int:
    """Remove all chunks for a deleted source object.

    Returns the number of rows removed (handy for ad-hoc debugging /
    backfill). Idempotent — second call on the same id returns 0.
    """
    deleted, _ = DocumentChunk.objects.filter(
        source_type=source_type, source_id=source_id
    ).delete()
    logger.info(
        "delete_chunks: source=%s/%s removed=%d", source_type, source_id, deleted
    )
    return deleted
