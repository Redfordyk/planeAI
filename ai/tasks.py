"""Celery task entry points for the AI pipeline.

Bodies are stubbed for TZ 1.4 — the real chunking + embedding +
upsert work lands in TZ 1.5. The shape of the task signatures
(`reindex_source`, `delete_chunks`) is what the signal handlers in
ai.signals call, and it is **stable**: 1.5 only fills the bodies.

All tasks are `acks_late=False` (default) — if a worker dies mid-task,
we want it to NOT replay (the next save will re-enqueue via the
debounce key). For `delete_chunks` that's fine, deletes are
idempotent. For `reindex_source` that's also fine, the next signal
fire enqueues again.
"""

from __future__ import annotations

import logging

from celery import shared_task


logger = logging.getLogger("plane.ai.tasks")


# `source_type` is one of:
#   ai.models.DocumentChunk.SOURCE_WORK_ITEM ("work_item")
#   ai.models.DocumentChunk.SOURCE_COMMENT   ("comment")
#   ai.models.DocumentChunk.SOURCE_PAGE      ("page")


@shared_task(name="ai.reindex_source")
def reindex_source(workspace_id: str, project_id: str | None, source_type: str, source_id: str) -> None:
    """Compute or refresh embeddings for one source object.

    TZ 1.5 implementation: fetch source content from Plane models,
    chunk via tiktoken, embed via OpenAIEmbed, upsert into
    DocumentChunk respecting (source_type, source_id, chunk_index)
    uniqueness, and write an AIUsageLog row.
    """
    logger.info(
        "reindex_source stub: ws=%s project=%s source=%s/%s",
        workspace_id,
        project_id,
        source_type,
        source_id,
    )


@shared_task(name="ai.delete_chunks")
def delete_chunks(source_type: str, source_id: str) -> int:
    """Remove all chunks for a deleted source object.

    Returns the number of rows removed (handy for ad-hoc debugging /
    backfill). Idempotent — second call on the same id returns 0.
    """
    from ai.models import DocumentChunk

    deleted, _ = DocumentChunk.objects.filter(
        source_type=source_type, source_id=source_id
    ).delete()
    logger.info(
        "delete_chunks: source=%s/%s removed=%d", source_type, source_id, deleted
    )
    return deleted
