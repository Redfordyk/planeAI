"""TZ 5.1 — agent assignment trigger and self-loop guard.

When an ``Issue`` is saved we check two activation conditions:

  1. one of its assignees has an enabled ``AIAgent`` row;
  2. it carries a label named ``ai-agent`` (case-sensitive — Plane's
     Label.name is free-form, so this is a deliberate contract with
     the operator).

If either matches, we enqueue ``ai.run_agent_on_workitem`` exactly
once per save, wrapped in ``transaction.on_commit`` so the Celery
worker never reads pre-commit state.

The self-loop guard (``agent_acting``) is what keeps an agent from
re-triggering itself. The worker (TZ 5.2) MUST wrap every Plane write
in ``with agent_acting(issue_id): ...``. That sets a short-lived
Redis flag which our signal checks before enqueueing. Without this
guard, ``set_labels`` / ``update_description`` etc. would fire the
post_save we listen to, re-enqueue the worker, and burn the token
budget in a tight loop.

Two layers of debounce protect against missed flags:

  - the Redis ``ai:agent_acting:<issue_id>`` key (set by the worker
    via :func:`agent_acting`), TTL = ``AGENT_ACTING_TTL`` seconds;
  - a separate ``ai:agent_pending:<issue_id>`` key (set by us when
    we enqueue) that collapses a rapid burst of saves into one task,
    same idea as ``ai/signals.py`` reindex debounce.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager

from django.core.cache import cache
from django.db import transaction


logger = logging.getLogger("plane.ai.agent_triggers")


# How long the worker's "I am writing" flag lives. Worker should be
# done with its Plane writes well within this window (single agent
# turn ≤ a few seconds even with retries). 60s is a generous ceiling;
# if the worker actually takes longer it deserves the suppressed
# trigger.
AGENT_ACTING_TTL = 60

# Collapses a flurry of saves on the same issue into a single agent
# task. Independent of the existing reindex debounce (TZ 1.4).
AGENT_PENDING_TTL = 10

AGENT_LABEL_NAME = "ai-agent"


# ---------------------------------------------------------------------------
# Self-loop guard
# ---------------------------------------------------------------------------


def _acting_key(issue_id) -> str:
    return f"ai:agent_acting:{issue_id}"


def _pending_key(issue_id) -> str:
    return f"ai:agent_pending:{issue_id}"


def is_agent_acting(issue_id) -> bool:
    """True if the worker has the loop-guard flag held for this issue."""
    return cache.get(_acting_key(issue_id)) is not None


@contextmanager
def agent_acting(issue_id):
    """Context manager the agent worker wraps around its Plane writes.

    Sets a short-lived cache flag so the post_save signal can tell the
    save came from the agent itself and skip re-enqueueing. Cleared on
    context exit (even on exception) so a crashed worker doesn't pin
    the issue out of triggers for the full TTL.

    Cross-process by design (Redis cache), so it works when the signal
    fires in the web process while the worker writes from celery.
    """
    key = _acting_key(issue_id)
    cache.set(key, "1", timeout=AGENT_ACTING_TTL)
    try:
        yield
    finally:
        cache.delete(key)


# ---------------------------------------------------------------------------
# Detection helpers
# ---------------------------------------------------------------------------


def is_assigned_to_agent(issue) -> bool:
    """True if any assignee on the issue has an enabled ``AIAgent`` row.

    Imports are deferred to avoid an app-loading cycle (this module is
    imported by signals.connect, which runs at app-ready time).
    """
    from ai.models import AIAgent

    assignee_ids = list(issue.assignees.values_list("id", flat=True))
    if not assignee_ids:
        return False
    return AIAgent.objects.filter(
        user_id__in=assignee_ids,
        workspace_id=issue.workspace_id,
        enabled=True,
    ).exists()


def has_agent_label(issue) -> bool:
    """True if the issue carries a label called ``ai-agent``.

    Label.name is a free-form string in Plane; we match exactly to
    keep the contract predictable for operators.
    """
    return issue.labels.filter(name=AGENT_LABEL_NAME).exists()


# ---------------------------------------------------------------------------
# Signal receiver
# ---------------------------------------------------------------------------


def _enqueue_agent(issue_id) -> None:
    """Debounced enqueue called from ``transaction.on_commit``."""
    from ai.tasks import run_agent_on_workitem

    key = _pending_key(issue_id)
    if not cache.add(key, "1", timeout=AGENT_PENDING_TTL):
        # A save within the window already enqueued; collapse.
        return
    run_agent_on_workitem.apply_async(
        args=[str(issue_id)],
        countdown=AGENT_PENDING_TTL,
    )


def on_issue_saved_for_agent(sender, instance, **kwargs):
    """post_save receiver wired in ``ai.signals.connect``.

    Order of checks is cost-driven: cheapest gates first so a normal
    save (no agent, no label) costs at most one cache lookup and one
    config check.
    """
    # Cheapest: loop guard. If the agent is the source of this save,
    # bail before touching DB at all.
    if is_agent_acting(instance.id):
        return

    # Soft-deleted or draft issues are not actionable by the agent.
    if getattr(instance, "deleted_at", None) is not None:
        return
    if getattr(instance, "is_draft", False):
        return

    # Workspace-level AI gate (mirrors the reindex signal).
    from ai.models import WorkspaceAIConfig

    if not WorkspaceAIConfig.objects.filter(
        workspace_id=instance.workspace_id, enabled=True
    ).exists():
        return

    if not (is_assigned_to_agent(instance) or has_agent_label(instance)):
        return

    transaction.on_commit(lambda: _enqueue_agent(instance.id))
