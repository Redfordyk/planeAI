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
from ai.pricing import embed_cost


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
        AIUsageLog.objects.create(
            workspace_id=ws_id,
            user=None,
            feature=AIUsageLog.FEATURE_EMBED,
            model=embed_model,
            input_tokens=tokens,
            output_tokens=0,
            cost_usd=embed_cost(embed_model, tokens),
        )

    logger.info(
        "reindex_source: source=%s/%s chunks=%d tokens=%d",
        source_type,
        source_id,
        len(rows),
        tokens,
    )


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
