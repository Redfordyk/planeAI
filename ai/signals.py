"""Signal handlers that schedule AI re-indexing.

Three defensive layers per CLAUDE.md invariants 1, 4, 5:

  1. ``transaction.on_commit`` — never enqueue from inside the
     transaction. A pre-commit enqueue races the worker against
     uncommitted writes (worker reads stale row or `DoesNotExist`).

  2. Redis ``cache.add`` debounce — a flurry of saves on the same
     issue/comment/page within a 10s window produces ONE task in the
     queue. Costs of dropping the dupe: trivial. Costs of indexing
     5x: 5x OpenAI tokens for content that ends up identical.

  3. ``AIProjectSettings.exclude_from_ai`` and ``WorkspaceAIConfig.enabled``
     gates — private projects and AI-off workspaces never produce
     enqueue calls, so their content never leaves the database.

The "no signals at all" failure mode (e.g. bulk-update bypass) is
addressed separately in TZ 1.6 (backfill / reconcile loop).
"""

from __future__ import annotations

import logging

from django.apps import apps
from django.core.cache import cache
from django.db import transaction
from django.db.models.signals import post_delete, post_save

from ai.models import (
    AIProjectSettings,
    DocumentChunk,
    WorkspaceAIConfig,
)
from ai.tasks import delete_chunks, reindex_source


logger = logging.getLogger("plane.ai.signals")


DEBOUNCE_SECONDS = 10


def _ai_enabled(workspace_id) -> bool:
    if workspace_id is None:
        return False
    return WorkspaceAIConfig.objects.filter(
        workspace_id=workspace_id, enabled=True
    ).exists()


def _project_excluded(project_id) -> bool:
    if project_id is None:
        return False
    return AIProjectSettings.objects.filter(
        project_id=project_id, exclude_from_ai=True
    ).exists()


def _enqueue_reindex(workspace_id, project_id, source_type: str, source_id) -> None:
    """Debounced enqueue. Called from `transaction.on_commit`."""
    key = f"ai:reindex_pending:{source_type}:{source_id}"
    if not cache.add(key, "1", timeout=DEBOUNCE_SECONDS):
        # Another save within the window already enqueued.
        return
    reindex_source.apply_async(
        args=[
            str(workspace_id) if workspace_id else None,
            str(project_id) if project_id else None,
            source_type,
            str(source_id),
        ],
        countdown=DEBOUNCE_SECONDS,
    )


# ---- Issue ----------------------------------------------------------------


def _soft_deleted(instance) -> bool:
    """Plane uses soft-delete: `.delete()` writes `deleted_at=now`
    instead of actually deleting (see apps/api/plane/db/mixins.py).
    A row with `deleted_at IS NOT NULL` is logically gone; we treat it
    as a delete for chunk-cleanup purposes and skip reindex.
    """
    return getattr(instance, "deleted_at", None) is not None


def _on_issue_saved(sender, instance, **kwargs):
    if _soft_deleted(instance):
        # Inline (not .delay) — chunk deletion is a small idempotent
        # SQL DELETE and inline keeps the soft-delete event visible
        # in test suites that don't run a live Celery worker.
        delete_chunks(DocumentChunk.SOURCE_WORK_ITEM, str(instance.id))
        return
    # Draft issues never appear in retrieval; skip indexing too.
    if getattr(instance, "is_draft", False):
        return
    if not _ai_enabled(instance.workspace_id):
        return
    if _project_excluded(instance.project_id):
        return
    transaction.on_commit(
        lambda: _enqueue_reindex(
            instance.workspace_id,
            instance.project_id,
            DocumentChunk.SOURCE_WORK_ITEM,
            instance.id,
        )
    )


def _on_issue_deleted(sender, instance, **kwargs):
    # Fallback for hard-delete paths (admin shell, raw QuerySet.delete
    # with soft=False, etc.). The soft-delete branch above covers the
    # normal flow.
    delete_chunks.delay(DocumentChunk.SOURCE_WORK_ITEM, str(instance.id))


# ---- Orchestrator Event Stream (TZ 8.1) ----------------------------------
#
# Emit a typed Event into Celery whenever an Issue changes. Loop
# protection lives in TWO places: the ``_modified_by_agent`` flag
# (set by EXECUTOR/PLANNER on the instance right before save) is
# carried into the event, and the router drops events with that flag
# set. Plus the orchestrator has its own per-event dedupe + per-issue
# lock.


def _on_issue_orchestrator_event(sender, instance, created, **kwargs):
    if _soft_deleted(instance):
        return
    if getattr(instance, "is_draft", False):
        return
    if not _ai_enabled(instance.workspace_id):
        return
    if _project_excluded(instance.project_id):
        return

    # Classify the event. Order matters — completed beats updated.
    from ai.orchestrator.events import (
        Event,
        ISSUE_CREATED,
        ISSUE_UPDATED,
        ISSUE_COMPLETED,
        ISSUE_BLOCKED,
    )
    state = getattr(instance, "state", None)
    group = (getattr(state, "group", "") or "").lower()
    if group == "completed":
        etype = ISSUE_COMPLETED
    elif group == "blocked":
        etype = ISSUE_BLOCKED
    elif created:
        etype = ISSUE_CREATED
    else:
        etype = ISSUE_UPDATED

    event = Event(
        type=etype,
        workspace_id=str(instance.workspace_id),
        project_id=str(instance.project_id) if instance.project_id else None,
        issue_id=str(instance.id),
        modified_by_agent=bool(getattr(instance, "_modified_by_agent", False)),
    )

    def _enqueue():
        from ai.tasks import orchestrator_handle_event
        try:
            orchestrator_handle_event.apply_async(args=[event.to_dict()], countdown=2)
        except Exception as exc:
            logger.warning("orchestrator enqueue failed for %s: %s", instance.id, exc)

    transaction.on_commit(_enqueue)


# ---- IssueComment ---------------------------------------------------------


def _on_comment_saved(sender, instance, **kwargs):
    if _soft_deleted(instance):
        delete_chunks(DocumentChunk.SOURCE_COMMENT, str(instance.id))
        return
    if not _ai_enabled(instance.workspace_id):
        return
    if _project_excluded(instance.project_id):
        return
    transaction.on_commit(
        lambda: _enqueue_reindex(
            instance.workspace_id,
            instance.project_id,
            DocumentChunk.SOURCE_COMMENT,
            instance.id,
        )
    )


def _on_comment_deleted(sender, instance, **kwargs):
    delete_chunks.delay(DocumentChunk.SOURCE_COMMENT, str(instance.id))


# ---- Page -----------------------------------------------------------------


def _on_page_saved(sender, instance, **kwargs):
    if _soft_deleted(instance):
        delete_chunks(DocumentChunk.SOURCE_PAGE, str(instance.id))
        return
    if getattr(instance, "archived_at", None) is not None:
        delete_chunks(DocumentChunk.SOURCE_PAGE, str(instance.id))
        return
    if not _ai_enabled(instance.workspace_id):
        return
    # Page has no direct project FK (SCHEMA.md §db.Page). Project-level
    # exclusion has to consult ProjectPage; defer to TZ 1.5 indexer
    # (which has to handle multi-project pages anyway). Here we only
    # check the workspace gate.
    transaction.on_commit(
        lambda: _enqueue_reindex(
            instance.workspace_id,
            None,
            DocumentChunk.SOURCE_PAGE,
            instance.id,
        )
    )


def _on_page_deleted(sender, instance, **kwargs):
    delete_chunks.delay(DocumentChunk.SOURCE_PAGE, str(instance.id))


# ---- AIProjectSettings ---------------------------------------------------
#
# Right-to-erasure for retroactive privacy flagging. TZ 3.4 prevents
# new chunks from being created for an excluded project, but the
# admin might flag a project AFTER it's already been indexed — e.g.
# the team realises a project they thought was public actually
# contains HR-PRIVATE issues.
#
# Without this signal those existing chunks remain in pgvector and
# keep showing up in search results, defeating the exclusion. The
# signal closes the loop: the moment ``exclude_from_ai`` flips True,
# we delete every DocumentChunk row for that project.
#
# Direction is one-way: ``False → True`` triggers cleanup. The
# reverse (``True → False``, "we declassified this project") does
# NOT auto-reindex — that's an explicit operator action via
# ``backfill_embeddings --workspace <id>`` because re-sending the
# content to OpenAI again is a billable decision, not a side effect
# of a UI toggle.


def _on_project_settings_saved(sender, instance, created, **kwargs):
    if not getattr(instance, "exclude_from_ai", False):
        # Flag is False — either a new "public" row (no-op) or a
        # declassification (see comment above; we do not auto-reindex).
        return
    # Project just became (or stayed) excluded. Purge any chunks
    # tied to it. The query is scoped by project_id, so workspace
    # isolation is preserved automatically.
    def _purge():
        n, _ = DocumentChunk.objects.filter(project_id=instance.project_id).delete()
        logger.info(
            "exclude_from_ai purge: project=%s removed=%d chunks",
            instance.project_id,
            n,
        )

    # Same on_commit guard as reindex — never delete inside the
    # transaction that flipped the flag; if the txn rolls back we'd
    # have orphaned the purge.
    transaction.on_commit(_purge)


# ---- Connect --------------------------------------------------------------


def connect() -> None:
    """Wire signals to the live Plane models. Called from ai.apps.ready().

    We resolve the senders via `apps.get_model(...)` to avoid an import
    cycle: ai gets loaded by Django before db's models are populated;
    `apps.get_model` only works once apps are populated, which is
    exactly when `ready()` fires.
    """
    Issue = apps.get_model("db", "Issue")
    IssueComment = apps.get_model("db", "IssueComment")
    Page = apps.get_model("db", "Page")

    post_save.connect(_on_issue_saved, sender=Issue, dispatch_uid="ai.issue_saved")
    post_delete.connect(_on_issue_deleted, sender=Issue, dispatch_uid="ai.issue_deleted")

    # TZ 5.1 — agent trigger. Separate receiver (different dispatch_uid)
    # so it can be disabled independently of the reindex pipeline.
    from ai.agent_triggers import on_issue_saved_for_agent

    post_save.connect(
        on_issue_saved_for_agent, sender=Issue, dispatch_uid="ai.agent_trigger"
    )

    # TZ 8.1 — orchestrator event stream (post-Plane-save → Celery → router).
    post_save.connect(
        _on_issue_orchestrator_event,
        sender=Issue,
        dispatch_uid="ai.orchestrator_event_stream",
    )

    post_save.connect(
        _on_comment_saved, sender=IssueComment, dispatch_uid="ai.comment_saved"
    )
    post_delete.connect(
        _on_comment_deleted, sender=IssueComment, dispatch_uid="ai.comment_deleted"
    )

    post_save.connect(_on_page_saved, sender=Page, dispatch_uid="ai.page_saved")
    post_delete.connect(_on_page_deleted, sender=Page, dispatch_uid="ai.page_deleted")

    # TZ 6.7 — right-to-erasure on retroactive privacy flag.
    post_save.connect(
        _on_project_settings_saved,
        sender=AIProjectSettings,
        dispatch_uid="ai.project_settings_saved",
    )

    logger.info(
        "ai signals connected: Issue, IssueComment, Page, AIProjectSettings (+ agent trigger)"
    )


# Connection is intentionally inside `connect()`, not via `@receiver`
# at module top level — keeps test setup deterministic and makes
# hot-reload double-binding impossible (explicit `dispatch_uid` per
# receiver above).
